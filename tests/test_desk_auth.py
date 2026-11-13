"""Trade Desk login: hashing, sessions, lockout, and the gate in front of every route and the websocket."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dotenv import dotenv_values
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tradingdesk.desk import auth as A
from tradingdesk.sessions import ET, SessionClock
from tradingdesk.settings import SessionsConfig

PW = "correct horse battery"


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture(scope="module")
def pw_hash() -> str:
    return A.hash_password(PW)


# -- passwords -----------------------------------------------------------------------------
def test_hash_is_argon2id_and_verifies(pw_hash):
    assert pw_hash.startswith("$argon2id$") and PW not in pw_hash
    assert A.is_valid_hash(pw_hash)
    assert A.verify_password(pw_hash, PW)
    assert not A.verify_password(pw_hash, PW + "x")
    assert not A.verify_password("", PW)
    assert not A.verify_password("not-a-hash", PW)
    assert not A.verify_password(pw_hash, "x" * (A.MAX_PASSWORD_LEN + 1))


def test_login_required_off_localhost(pw_hash):
    assert A.login_problem("127.0.0.1", "") is None
    assert A.login_problem("0.0.0.0", pw_hash) is None
    assert "no Trade Desk password" in A.login_problem("0.0.0.0", "")
    assert "not an Argon2id hash" in A.login_problem("127.0.0.1", "plaintext")


def test_env_write_round_trips_through_dotenv(tmp_path, pw_hash):
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=PK123\nDESK_PASSWORD_HASH=old\n", encoding="utf-8")
    A.write_env_value(env, A.ENV_KEY, pw_hash)
    vals = dotenv_values(env)
    assert vals["DESK_PASSWORD_HASH"] == pw_hash          # the $s in the hash survive
    assert vals["ALPACA_API_KEY"] == "PK123"
    assert env.read_text(encoding="utf-8").count("DESK_PASSWORD_HASH") == 1


def test_set_password_command(tmp_path):
    env = tmp_path / ".env"
    answers = iter([PW, PW])
    assert A.set_password_interactive(env, prompt=lambda _p: next(answers)) == 0
    stored = dotenv_values(env)[A.ENV_KEY]
    assert A.verify_password(stored, PW) and PW not in env.read_text(encoding="utf-8")

    assert A.set_password_interactive(env, prompt=lambda _p: "short") == 1
    mismatch = iter([PW, PW + "!"])
    assert A.set_password_interactive(env, prompt=lambda _p: next(mismatch)) == 1
    assert dotenv_values(env)[A.ENV_KEY] == stored        # failures change nothing


# -- sessions ------------------------------------------------------------------------------
def test_session_ends_with_the_trading_day():
    expiry = A.end_of_trading_day(SessionClock(SessionsConfig()))   # Ended at 11:30 ET
    thu_10 = datetime(2026, 11, 12, 10, 0, tzinfo=ET)
    assert expiry(thu_10) == datetime(2026, 11, 12, 11, 30, tzinfo=ET)
    wed_13 = datetime(2026, 11, 11, 13, 0, tzinfo=ET)
    assert expiry(wed_13) == datetime(2026, 11, 12, 11, 30, tzinfo=ET)
    fri_13 = datetime(2026, 11, 13, 13, 0, tzinfo=ET)                # next Ended is Monday
    assert expiry(fri_13) == fri_13 + A.MAX_SESSION


def test_session_store_expires_and_revokes():
    clock = Clock(datetime(2026, 11, 12, 15, 0, tzinfo=UTC))
    store = A.SessionStore(lambda now: now + timedelta(hours=1), now=clock)
    token, _ = store.create()
    assert store.valid(token) and not store.valid("forged") and not store.valid(None)
    assert token not in store._sessions                   # only the digest is kept
    clock.at += timedelta(minutes=61)
    assert not store.valid(token)
    t2, _ = store.create()
    store.revoke(t2)
    assert not store.valid(t2)


def test_lockout_after_five_misses():
    clock = Clock(datetime(2026, 11, 12, 15, 0, tzinfo=UTC))
    lim = A.LoginLimiter(now=clock)
    assert [lim.failure("1.2.3.4") for _ in range(5)] == [False] * 4 + [True]
    assert lim.locked_for("1.2.3.4") == timedelta(minutes=15)
    assert lim.locked_for("5.6.7.8") == timedelta(0)      # per IP
    clock.at += timedelta(minutes=15)
    assert lim.locked_for("1.2.3.4") == timedelta(0)


def test_misses_outside_the_window_dont_count():
    clock = Clock(datetime(2026, 11, 12, 15, 0, tzinfo=UTC))
    lim = A.LoginLimiter(now=clock)
    for _ in range(4):
        lim.failure("ip")
    clock.at += timedelta(minutes=16)
    assert not lim.failure("ip")


# -- the gate, end to end ------------------------------------------------------------------
@pytest.fixture
def desk(pw_hash):
    alerts: list[str] = []
    store = A.SessionStore(lambda now: now + timedelta(hours=8))
    auth = A.DeskAuth(pw_hash, store, notify=alerts.append)
    web = FastAPI()

    @web.get("/")
    def _root() -> PlainTextResponse:
        return PlainTextResponse("DESK")

    @web.get("/_nicegui/static/app.js")
    def _asset() -> PlainTextResponse:
        return PlainTextResponse("js")

    @web.websocket("/_nicegui_ws/socket.io/")
    async def _ws(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("approve-channel")
        await ws.close()

    A.install(web, auth)
    client = TestClient(web, base_url="http://testserver", follow_redirects=False)
    client.alerts = alerts
    return client


def login(client, password=PW):
    return client.post("/login", data={"password": password}, headers={"origin": "http://testserver"})


def test_everything_is_gated_until_login(desk):
    r = desk.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert desk.get("/_nicegui/static/app.js").status_code == 401
    assert desk.post("/anything").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with desk.websocket_connect("/_nicegui_ws/socket.io/") as ws:
            ws.receive_text()
    page = desk.get("/login")
    assert page.status_code == 200 and 'type="password"' in page.text


def test_login_opens_the_desk_and_websocket(desk):
    r = login(desk)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert desk.get("/").text == "DESK"
    assert desk.get("/login").status_code == 303              # already logged in
    with desk.websocket_connect("/_nicegui_ws/socket.io/", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_text() == "approve-channel"


def test_cross_site_websocket_refused_even_with_cookie(desk):
    login(desk)
    with pytest.raises(WebSocketDisconnect):
        with desk.websocket_connect("/_nicegui_ws/socket.io/", headers={"origin": "http://evil.test"}) as ws:
            ws.receive_text()


def test_cross_site_login_post_refused(desk):
    r = desk.post("/login", data={"password": PW}, headers={"origin": "http://evil.test"})
    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_wrong_password_then_lockout_alerts(desk):
    for _ in range(4):
        r = login(desk, "nope")
        assert r.status_code == 401 and "Wrong password." in r.text
    r = login(desk, "nope")
    assert "Too many attempts" in r.text
    assert len(desk.alerts) == 1 and "5 wrong passwords" in desk.alerts[0]
    r = login(desk)                                            # right password, still locked
    assert r.status_code == 401 and "Too many attempts" in r.text
    assert desk.get("/").status_code == 303


def test_logout_ends_the_session(desk):
    login(desk)
    assert desk.post("/logout", headers={"origin": "http://testserver"}).status_code == 204
    assert desk.get("/").status_code == 303


def test_stolen_cookie_dies_on_logout(desk):
    login(desk)
    token = desk.cookies.get(A.COOKIE)
    desk.post("/logout", headers={"origin": "http://testserver"})
    desk.cookies.set(A.COOKIE, token)                          # replay the old cookie
    assert desk.get("/").status_code == 303


def test_error_text_is_escaped():
    assert "<script>" not in A.login_page("<script>x</script>")
