"""
Trade Desk login: one password, server-side sessions, lockout after repeated misses.

  Password    only an Argon2id hash is kept, in .env as DESK_PASSWORD_HASH. Set it with
              `python -m tradingdesk set-desk-password` (prompted, never echoed or logged).
  Sessions    a random 256-bit token in an HttpOnly cookie. The server keeps only its
              SHA-256, in memory, so a restart logs everyone out. A session ends when
              the trading day does (the next Ended phase), and never lasts over 24 hours.
  Lockout     5 wrong passwords from one IP within 15 minutes locks that IP out for
              15 minutes and sends a Discord alert.
  Gate        an ASGI middleware covers every HTTP request and the websocket the
              buttons use. Only /login (and the favicon) is open. Websocket upgrades
              and POSTs must come from the desk's own origin.

There is no TLS yet: on the LAN the password and cookie travel in plain HTTP.
"""
from __future__ import annotations

import getpass
import hashlib
import html
import os
import secrets
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request                      # module level: route annotations resolve here
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from tradingdesk.sessions import Phase, SessionClock

COOKIE = "td_session"
ENV_KEY = "DESK_PASSWORD_HASH"
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 1024          # cap the input before hashing
MAX_FORM_BYTES = 4096
MAX_SESSION = timedelta(hours=24)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

_hasher = PasswordHasher()       # argon2id, RFC 9106 low-memory profile (64 MiB, t=3, p=4)


# -- passwords -----------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    if not stored_hash or len(password) > MAX_PASSWORD_LEN:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def is_valid_hash(stored_hash: str) -> bool:
    return stored_hash.startswith("$argon2id$") and stored_hash.count("$") == 5


def login_problem(host: str, stored_hash: str) -> str | None:
    """Why the desk must not start, or None. A non-local desk needs a password."""
    if stored_hash and not is_valid_hash(stored_hash):
        return f"{ENV_KEY} in .env is not an Argon2id hash; run `python -m tradingdesk set-desk-password`"
    if host not in LOCAL_HOSTS and not stored_hash:
        return (f"desk.host is {host}, but no Trade Desk password is set. "
                "Run `python -m tradingdesk set-desk-password` (or set desk.host to 127.0.0.1)")
    return None


# -- sessions ------------------------------------------------------------------------------
def end_of_trading_day(clock: SessionClock) -> Callable[[datetime], datetime]:
    """Session expiry: the next time the clock reaches Ended, capped at MAX_SESSION."""
    def expiry(now: datetime) -> datetime:
        at = now
        for _ in range(12):                       # ~2 trading days of phase changes
            phase, at = clock.next_change(at)
            if phase is Phase.ENDED:
                break
        return min(at, now + MAX_SESSION)
    return expiry


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    def __init__(self, expiry: Callable[[datetime], datetime],
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._expiry = expiry
        self._now = now
        self._sessions: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires = self._expiry(now)
        with self._lock:
            self._purge(now)
            self._sessions[_digest(token)] = expires
        return token, expires

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(_digest(token))
            if expires is None:
                return False
            if self._now() >= expires:
                del self._sessions[_digest(token)]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(_digest(token), None)

    def _purge(self, now: datetime) -> None:
        for k in [k for k, exp in self._sessions.items() if now >= exp]:
            del self._sessions[k]


# -- lockout -------------------------------------------------------------------------------
class LoginLimiter:
    def __init__(self, max_failures: int = 5, window: timedelta = timedelta(minutes=15),
                 lockout: timedelta = timedelta(minutes=15),
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self._now = now
        self._fails: dict[str, deque[datetime]] = {}
        self._locked: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def locked_for(self, ip: str) -> timedelta:
        """Time left on this IP's lockout (zero if not locked)."""
        with self._lock:
            until = self._locked.get(ip)
            if until is None:
                return timedelta(0)
            left = until - self._now()
            if left <= timedelta(0):
                del self._locked[ip]
                return timedelta(0)
            return left

    def failure(self, ip: str) -> bool:
        """Record a miss. True if it just locked the IP out."""
        now = self._now()
        with self._lock:
            q = self._fails.setdefault(ip, deque())
            q.append(now)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_failures:
                self._locked[ip] = now + self.lockout
                q.clear()
                return True
            return False

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


# -- the gate ------------------------------------------------------------------------------
class DeskAuth:
    def __init__(self, password_hash: str, store: SessionStore, limiter: LoginLimiter | None = None,
                 notify: Callable[[str], None] | None = None) -> None:
        self.password_hash = password_hash
        self.store = store
        self.limiter = limiter or LoginLimiter()
        self.notify = notify or (lambda _msg: None)

    def attempt(self, ip: str, password: str) -> tuple[tuple[str, datetime] | None, str]:
        """Check a login. Returns ((token, expires) or None, message for the page)."""
        left = self.limiter.locked_for(ip)
        if left:
            logger.warning("desk login refused: {} is locked out", ip)
            return None, f"Too many attempts. Try again in {max(1, round(left.total_seconds() / 60))} min."
        if verify_password(self.password_hash, password):
            self.limiter.success(ip)
            token, expires = self.store.create()
            logger.info("desk login from {} (session until {:%Y-%m-%d %H:%M %Z})", ip, expires)
            return (token, expires), ""
        logger.warning("desk login failed from {}", ip)
        if self.limiter.failure(ip):
            mins = round(self.limiter.lockout.total_seconds() / 60)
            logger.warning("desk login: {} locked out for {} min", ip, mins)
            self.notify(f"Trade Desk: {self.limiter.max_failures} wrong passwords from {ip}; "
                        f"locked out for {mins} min.")
            return None, f"Too many attempts. Try again in {mins} min."
        return None, "Wrong password."


def _cookies(scope: dict[str, Any]) -> dict[str, str]:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(v.decode("latin-1"))
            except Exception:
                return {}
            return {name: m.value for name, m in jar.items()}
    return {}


def _header(scope: dict[str, Any], name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return ""


def same_origin(scope: dict[str, Any]) -> bool:
    """True if Origin (when sent) matches the Host header. Browsers send it on websockets and POSTs."""
    origin = _header(scope, b"origin")
    if not origin:
        return True
    return (urlparse(origin).netloc or "").lower() == _header(scope, b"host").lower()


_OPEN_PATHS = {"/login", "/favicon.ico"}


class AuthMiddleware:
    """Pure ASGI, so it covers websocket upgrades too (BaseHTTPMiddleware doesn't)."""

    def __init__(self, app: Any, auth: DeskAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if kind == "http" and path in _OPEN_PATHS:
            return await self.app(scope, receive, send)

        ok = self.auth.store.valid(_cookies(scope).get(COOKIE))
        if kind == "websocket":
            if ok and same_origin(scope):
                return await self.app(scope, receive, send)
            await send({"type": "websocket.close", "code": 1008})
            return
        if ok and (scope.get("method") in ("GET", "HEAD") or same_origin(scope)):
            return await self.app(scope, receive, send)
        if scope.get("method") == "GET" and not path.startswith("/_nicegui"):
            await _send(send, 303, b"", [(b"location", b"/login")])
        else:
            await _send(send, 401, b"login required", [(b"content-type", b"text/plain")])


async def _send(send: Any, status: int, body: bytes, headers: list[tuple[bytes, bytes]]) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode()),
                                      (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


# -- routes --------------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Desk - log in</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --fg:#1d2330; --muted:#6b7280; --line:#d5d9e0; --accent:#1f6feb; --bad:#c62828; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#11141a; --card:#1a1f28; --fg:#e6e9ef; --muted:#9aa3b2; --line:#2c3341; --accent:#4c8dff; --bad:#ff6b6b; }
  }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
         font: 15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; padding:16px; }
  form { width:100%; max-width:340px; background:var(--card); border:1px solid var(--line); border-radius:10px;
         padding:28px 24px; display:grid; gap:14px; }
  h1 { margin:0; font-size:20px; }
  p { margin:0; color:var(--muted); font-size:13px; }
  label { display:grid; gap:6px; font-size:13px; color:var(--muted); }
  input { font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
  input:focus { outline:2px solid var(--accent); outline-offset:1px; }
  button { font:inherit; font-weight:600; padding:10px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; }
  .err { color:var(--bad); font-size:13px; min-height:1em; }
</style></head>
<body><form method="post" action="/login" autocomplete="on">
  <h1>&#x1F4C8; Trade Desk</h1>
  <p>Paper trading. Every trade still needs your approval.</p>
  <label>Password<input type="password" name="password" autocomplete="current-password" autofocus required></label>
  <div class="err" role="alert">{error}</div>
  <button type="submit">Log in</button>
</form></body></html>"""


def login_page(error: str = "") -> str:
    return _PAGE.replace("{error}", html.escape(error))


def install(web: Any, auth: DeskAuth) -> None:
    """Add the gate and the /login, /logout routes to NiceGUI's FastAPI app (before ui.run)."""
    no_store = {"cache-control": "no-store"}

    @web.get("/login", include_in_schema=False)
    async def _login_form(request: Request) -> Response:
        if auth.store.valid(request.cookies.get(COOKIE)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_page(), headers=no_store)

    @web.post("/login", include_in_schema=False)
    async def _login(request: Request) -> Response:
        if not same_origin(request.scope):
            return HTMLResponse(login_page("Request blocked (wrong origin)."), status_code=403, headers=no_store)
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return HTMLResponse(login_page("Request too large."), status_code=413, headers=no_store)
        password = (parse_qs(body.decode("utf-8", "replace")).get("password") or [""])[0]
        ip = request.client.host if request.client else "?"
        session, msg = auth.attempt(ip, password)
        if not session:
            return HTMLResponse(login_page(msg), status_code=401, headers=no_store)
        token, expires = session
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, token, max_age=max(60, int((expires - datetime.now(UTC)).total_seconds())),
                        httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
        return resp

    @web.post("/logout", include_in_schema=False)
    async def _logout(request: Request) -> Response:
        auth.store.revoke(request.cookies.get(COOKIE))
        resp = Response(status_code=204)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    web.add_middleware(AuthMiddleware, auth=auth)


# -- set-desk-password ---------------------------------------------------------------------
def write_env_value(env_path: Path, key: str, value: str) -> None:
    """Set KEY='value' in .env (replacing an existing KEY= line), keeping the file private."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new = f"{key}='{value}'"
    for i, ln in enumerate(lines):
        if ln.split("=", 1)[0].strip() == key:
            lines[i] = new
            break
    else:
        lines += ["", "# --- Trade Desk login (set with: python -m tradingdesk set-desk-password) ---", new]
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)


def set_password_interactive(env_path: Path, prompt: Callable[[str], str] = getpass.getpass) -> int:
    pw = prompt(f"New Trade Desk password (at least {MIN_PASSWORD_LEN} characters): ")
    if len(pw) < MIN_PASSWORD_LEN:
        logger.error("Too short: use at least {} characters", MIN_PASSWORD_LEN)
        return 1
    if len(pw) > MAX_PASSWORD_LEN:
        logger.error("Too long: {} characters at most", MAX_PASSWORD_LEN)
        return 1
    if prompt("Again: ") != pw:
        logger.error("The two entries don't match; nothing changed")
        return 1
    write_env_value(env_path, ENV_KEY, hash_password(pw))
    logger.success("Saved the password hash to {} as {}. Restart the desk to use it.", env_path, ENV_KEY)
    return 0
