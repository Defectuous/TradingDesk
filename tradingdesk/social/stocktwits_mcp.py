"""
StockTwits' official MCP server (https://ai.stocktwits.com), for the Sentiment agent.

Auth is the standard MCP OAuth flow the server advertises: dynamic client
registration, authorization code + PKCE (S256), refresh tokens, public client
(no secret). You log in once in a browser with your StockTwits account
(`python -m tradingdesk stocktwits-login`); the token is saved under data/
(git-ignored) and refreshed automatically after that.

Transport is MCP "streamable HTTP": JSON-RPC POSTs to /mcp, replies as JSON or
server-sent events, session id in the Mcp-Session-Id header.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import requests
from loguru import logger

SERVER = "https://mcp.stocktwits.com"
MCP_URL = f"{SERVER}/mcp"
REDIRECT_PORT = 8977
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPE = "read"
PROTOCOL_VERSION = "2025-06-18"
USER_AGENT = "TradingDesk/0.1 (personal research)"


class StockTwitsAuthError(RuntimeError):
    pass


class StockTwitsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(store: TokenStore, open_browser: bool = True, timeout_sec: float = 300) -> dict[str, Any]:
    """One-time browser login. Registers a client if needed, then runs the PKCE code flow."""
    saved = store.load() or {}
    client_id = saved.get("client_id")
    if not client_id:
        r = requests.post(f"{SERVER}/register", timeout=20, headers={"User-Agent": USER_AGENT}, json={
            "client_name": "TradingDesk (personal)", "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": SCOPE,
        })
        if r.status_code >= 400:
            raise StockTwitsAuthError(f"client registration failed: HTTP {r.status_code} {r.text[:200]}")
        client_id = r.json()["client_id"]
        store.save({"client_id": client_id})          # keep the registration even if this login times out

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h3>TradingDesk: StockTwits connected. You can close this tab.</h3>" if ok else
                              "<h3>TradingDesk: login failed. Check the terminal.</h3>").encode())

        def log_message(self, *args: Any) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    srv.timeout = 1
    url = f"{SERVER}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI, "scope": SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": MCP_URL,
    })
    logger.info("Open this URL to connect StockTwits (it should open by itself):\n{}", url)
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout_sec
    try:
        while not got and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    if "code" not in got:
        raise StockTwitsAuthError(f"no authorization received ({got.get('error', 'timed out')})")
    if got.get("state") != state:
        raise StockTwitsAuthError("state mismatch; login aborted")
    tok = _token_request({"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT_URI,
                          "client_id": client_id, "code_verifier": verifier, "resource": MCP_URL})
    data = {"client_id": client_id, **tok}
    store.save(data)
    return data


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    r = requests.post(f"{SERVER}/token", data=form, timeout=20, headers={"User-Agent": USER_AGENT})
    if r.status_code >= 400:
        raise StockTwitsAuthError(f"token request failed: HTTP {r.status_code} {r.text[:200]}")
    t = r.json()
    return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token", form.get("refresh_token")),
            "expires_at": time.time() + float(t.get("expires_in", 3600)) - 60, "scope": t.get("scope", SCOPE)}


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

class StockTwitsMCP:
    def __init__(self, store: TokenStore, timeout_sec: float = 20) -> None:
        self.store = store
        self.timeout = timeout_sec
        self._lock = threading.Lock()
        self._session: str | None = None
        self._id = 0

    def _token(self) -> str:
        data = self.store.load()
        if not data or not data.get("access_token"):
            raise StockTwitsAuthError("not connected: run `python -m tradingdesk stocktwits-login`")
        if time.time() >= data.get("expires_at", 0):
            if not data.get("refresh_token"):
                raise StockTwitsAuthError("token expired and no refresh token: log in again")
            data = {**data, **_token_request({"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                              "client_id": data["client_id"], "resource": MCP_URL})}
            self.store.save(data)
        return data["access_token"]

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT,
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        r = requests.post(MCP_URL, json=payload, headers=headers, timeout=self.timeout)
        if r.status_code == 401:
            raise StockTwitsAuthError("StockTwits rejected the token (401): log in again")
        if r.status_code == 404 and self._session:          # session expired on the server
            self._session = None
            raise StockTwitsError("session expired")
        if r.status_code == 429:
            raise StockTwitsError(f"rate limited (429), retry-after {r.headers.get('retry-after')}")
        if r.status_code >= 400:
            raise StockTwitsError(f"HTTP {r.status_code}: {r.text[:200]}")
        self._session = r.headers.get("Mcp-Session-Id", self._session)
        if "id" not in payload or r.status_code == 202 or not r.content:
            return None
        if r.headers.get("Content-Type", "").startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == payload["id"]:
                        return msg
            raise StockTwitsError("no reply in event stream")
        return r.json()

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            if self._session is None and method != "initialize":
                self._initialize()
            self._id += 1
            msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if msg is None:
            raise StockTwitsError(f"{method}: empty reply")
        if "error" in msg:
            raise StockTwitsError(f"{method}: {msg['error']}")
        return msg["result"]

    def _initialize(self) -> None:
        self._id += 1
        self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "TradingDesk", "version": "0.1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tools(self) -> list[dict[str, Any]]:
        return self._rpc("tools/list").get("tools", [])

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Returns the tool result: {'content': [...], 'structuredContent': {...}?, 'isError': bool?}."""
        try:
            res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        except StockTwitsError as exc:
            if "session expired" in str(exc):
                res = self._rpc("tools/call", {"name": name, "arguments": arguments})
            else:
                raise
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            raise StockTwitsError(f"{name} failed: {text[:200]}")
        return res
