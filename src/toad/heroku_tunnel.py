"""Heroku tunnel client for Toad.

Two-step flow:
  1. `toad serve https://my-app.heroku.com`
     → connects to relay, prints lobby URL  https://my-app.heroku.com/aaa
  2. Browser visits /aaa
     → relay sends spawn_session through tunnel
     → we start a textual-serve subprocess on a free local port
     → reply session_ready with a random session_id
     → relay redirects browser to /aaa/<session_id>/
  3. /aaa/<session_id>/ is shareable; all browsers get the SAME session
     (relay fans out the single local WebSocket to all browsers)
"""

import asyncio
import base64
import json
import logging
import secrets
import socket
import ssl
import string
import sys
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("toad.heroku_tunnel")

SESSION_ID_CHARS = string.ascii_lowercase + string.digits


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _make_session_id(length: int = 8) -> str:
    return "".join(secrets.choice(SESSION_ID_CHARS) for _ in range(length))


def _make_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class SessionData:
    session_id: str
    local_port: int
    proc: Any  # asyncio.subprocess.Process
    local_ws: Any = None  # aiohttp.ClientWebSocketResponse once connected


# ---------------------------------------------------------------------------
# HerokuTunnel
# ---------------------------------------------------------------------------


class HerokuTunnel:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionData] = {}  # session_id → SessionData
        self._heroku_url: str = ""
        self._endpoint: str = ""
        self._tunnel_ws: Any = None
        self._session: Any = None  # aiohttp.ClientSession
        self._register_url: str = ""
        self._ssl_ctx: Any = None

    async def run(self, heroku_url: str) -> None:
        import aiohttp

        self._heroku_url = heroku_url.rstrip("/")

        if self._heroku_url.startswith("https://"):
            ws_url = "wss://" + self._heroku_url[len("https://"):]
        elif self._heroku_url.startswith("http://"):
            ws_url = "ws://" + self._heroku_url[len("http://"):]
        else:
            ws_url = self._heroku_url

        self._register_url = f"{ws_url}/register"
        self._ssl_ctx = _make_ssl_context()

        async with aiohttp.ClientSession() as session:
            self._session = session
            delay = 1.0
            while True:
                try:
                    await self._connect_once()
                    delay = 1.0
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    print(f"Connection lost ({exc}), retrying in {delay:.0f}s…")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)

        for sd in list(self._sessions.values()):
            if sd.proc.returncode is None:
                sd.proc.terminate()

    async def _connect_once(self) -> None:
        import aiohttp

        async with self._session.ws_connect(self._register_url, ssl=self._ssl_ctx) as ws:
            self._tunnel_ws = ws

            # New handshake: client speaks first
            await ws.send_json({
                "type": "register",
                "preferred_endpoint": self._endpoint or None,
            })

            msg = await ws.receive_json()
            if msg.get("type") != "registered":
                print(f"Unexpected message: {msg}", file=sys.stderr)
                return

            self._endpoint = msg["endpoint"]
            lobby_url = msg["url"]

            print(f"Share this URL to spawn sessions: {lobby_url}")
            sys.stdout.flush()

            # Restore existing sessions after reconnect
            if self._sessions:
                await ws.send_json({
                    "type": "restore_sessions",
                    "sessions": list(self._sessions.keys()),
                })

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._dispatch(data)
                elif msg.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                ):
                    break
            # Processes stay alive across reconnects — no terminate() here

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, data: dict) -> None:
        msg_type = data.get("type")

        if msg_type == "spawn_session":
            asyncio.ensure_future(self._handle_spawn_session(data))

        elif msg_type == "http_request":
            asyncio.ensure_future(self._handle_http_request(data))

        elif msg_type == "ws_connect":
            asyncio.ensure_future(self._handle_ws_connect(data))

        elif msg_type == "ws_message":
            await self._handle_ws_message(data)

        elif msg_type == "ws_close":
            await self._handle_ws_close(data)

        else:
            log.debug("Unknown message type: %s", msg_type)

    # ------------------------------------------------------------------
    # spawn_session: start a new textual-serve subprocess
    # ------------------------------------------------------------------

    async def _handle_spawn_session(self, data: dict) -> None:
        spawn_id = data["id"]
        session_id = _make_session_id()
        local_port = _find_free_port()

        # public_url must NOT have trailing slash (textual-serve appends "/path")
        public_url = f"{self._heroku_url}/{self._endpoint}/{session_id}"

        proc = await asyncio.create_subprocess_exec(
            sys.argv[0], "serve",
            "--port", str(local_port),
            "--host", "localhost",
            "--public-url", public_url,
        )

        sd = SessionData(session_id=session_id, local_port=local_port, proc=proc)
        self._sessions[session_id] = sd

        # Give textual-serve time to bind
        await asyncio.sleep(1.5)

        log.info("Session spawned: %s on port %d", session_id, local_port)

        await self._tunnel_ws.send_json({
            "type": "session_ready",
            "id": spawn_id,
            "session_id": session_id,
        })

    # ------------------------------------------------------------------
    # http_request: forward to the correct local textual-serve
    # ------------------------------------------------------------------

    async def _handle_http_request(self, data: dict) -> None:
        import httpx

        req_id = data["id"]
        session_id = data.get("session_id")
        sd = self._sessions.get(session_id)
        if sd is None:
            await self._tunnel_ws.send_json({
                "type": "http_response",
                "id": req_id,
                "status": 503,
                "headers": [],
                "body": base64.b64encode(b"Session not found").decode(),
            })
            return

        method = data.get("method", "GET")
        path = data.get("path", "/")
        query = data.get("query", "")
        headers_list = data.get("headers") or []
        body_b64 = data.get("body")

        url = f"http://localhost:{sd.local_port}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {k: v for k, v in headers_list}
        body = base64.b64decode(body_b64) if body_b64 else None

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.request(
                    method, url,
                    headers=headers,
                    content=body,
                    follow_redirects=True,
                    timeout=30,
                )
            resp_headers = [
                [k, v] for k, v in resp.headers.items()
                if k.lower() not in ("transfer-encoding", "connection", "keep-alive")
            ]
            await self._tunnel_ws.send_json({
                "type": "http_response",
                "id": req_id,
                "status": resp.status_code,
                "headers": resp_headers,
                "body": base64.b64encode(resp.content).decode(),
            })
        except Exception as exc:
            log.warning("HTTP error %s %s: %s", method, url, exc)
            await self._tunnel_ws.send_json({
                "type": "http_response",
                "id": req_id,
                "status": 502,
                "headers": [],
                "body": base64.b64encode(str(exc).encode()).decode(),
            })

    # ------------------------------------------------------------------
    # ws_connect: open (or reuse) a single local WS per session
    # ------------------------------------------------------------------

    async def _handle_ws_connect(self, data: dict) -> None:
        ws_id = data["ws_id"]
        session_id = data.get("session_id")
        sd = self._sessions.get(session_id)
        if sd is None:
            await self._tunnel_ws.send_json({
                "type": "ws_close",
                "session_id": session_id,
            })
            return

        path = data.get("path", "/ws")
        query = data.get("query", "")

        if sd.local_ws is None or sd.local_ws.closed:
            # Open the single local WS for this session
            url = f"ws://localhost:{sd.local_port}{path}"
            if query:
                url = f"{url}?{query}"
            try:
                local_ws = await self._session.ws_connect(url)
            except Exception as exc:
                log.warning("Could not connect local WS %s: %s", url, exc)
                await self._tunnel_ws.send_json({
                    "type": "ws_close",
                    "session_id": session_id,
                })
                return

            sd.local_ws = local_ws
            # Start relay: local WS → tunnel (fan-out handled by server)
            asyncio.ensure_future(self._relay_local_to_tunnel(session_id, local_ws))

        # Acknowledge this browser WS connection
        await self._tunnel_ws.send_json({"type": "ws_open", "ws_id": ws_id})

    async def _relay_local_to_tunnel(
        self,
        session_id: str,
        local_ws: Any,
    ) -> None:
        import aiohttp

        try:
            async for msg in local_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = base64.b64encode(msg.data.encode()).decode()
                    await self._tunnel_ws.send_json({
                        "type": "ws_message",
                        "session_id": session_id,
                        "data": payload,
                        "binary": False,
                    })
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    payload = base64.b64encode(msg.data).decode()
                    await self._tunnel_ws.send_json({
                        "type": "ws_message",
                        "session_id": session_id,
                        "data": payload,
                        "binary": True,
                    })
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            sd = self._sessions.get(session_id)
            if sd:
                sd.local_ws = None
            if not self._tunnel_ws.closed:
                try:
                    await self._tunnel_ws.send_json({
                        "type": "ws_close",
                        "session_id": session_id,
                    })
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # ws_message: forward browser input to the local WS for this session
    # ------------------------------------------------------------------

    async def _handle_ws_message(self, data: dict) -> None:
        session_id = data.get("session_id")
        sd = self._sessions.get(session_id)
        if sd is None or sd.local_ws is None or sd.local_ws.closed:
            return
        raw = data.get("data", "")
        binary = data.get("binary", False)
        decoded = base64.b64decode(raw) if raw else b""
        if binary:
            await sd.local_ws.send_bytes(decoded)
        else:
            await sd.local_ws.send_str(decoded.decode("utf-8", errors="replace"))

    # ------------------------------------------------------------------
    # ws_close: close the local WS for this session
    # ------------------------------------------------------------------

    async def _handle_ws_close(self, data: dict) -> None:
        session_id = data.get("session_id")
        sd = self._sessions.get(session_id)
        if sd and sd.local_ws and not sd.local_ws.closed:
            await sd.local_ws.close()
