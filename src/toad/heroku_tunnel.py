"""Heroku tunnel client for Toad.

Connects a local textual-serve instance to a Heroku relay server so the
Toad TUI can be accessed from the public internet.

Usage (via CLI):
    toad serve https://my-app.heroku.com

What it does:
1. Find a free local port.
2. Open a WebSocket to wss://{heroku_host}/register.
3. Receive the "registered" message → get endpoint + public URL.
4. Start a subprocess: `toad serve --port PORT --host localhost --public-url URL`
5. Print the public URL.
6. Loop: receive tunnel messages, dispatch to handlers:
   - http_request  → forward to local textual-serve, reply with http_response
   - ws_connect    → open WS to local textual-serve, relay bidirectionally
   - ws_message    → forward to appropriate local WS connection
   - ws_close      → close the corresponding local WS connection
"""

import asyncio
import base64
import json
import logging
import socket
import ssl
import sys
from typing import Any

log = logging.getLogger("toad.heroku_tunnel")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _make_ssl_context() -> ssl.SSLContext:
    """Return an SSL context using certifi's CA bundle (works on macOS/Python from python.org)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# HerokuTunnel
# ---------------------------------------------------------------------------


class HerokuTunnel:
    """Manages the WebSocket tunnel to a Heroku relay server."""

    def __init__(self) -> None:
        # Active local WebSocket connections keyed by ws_id
        self._local_ws: dict[str, Any] = {}  # ws_id -> aiohttp.ClientWebSocketResponse
        self._local_port: int = 0

    async def run(self, heroku_url: str) -> None:
        """Connect to the relay server and start the local textual-serve subprocess."""
        import aiohttp

        heroku_url = heroku_url.rstrip("/")

        # Build the WebSocket URL for /register
        if heroku_url.startswith("https://"):
            ws_url = "wss://" + heroku_url[len("https://"):]
        elif heroku_url.startswith("http://"):
            ws_url = "ws://" + heroku_url[len("http://"):]
        else:
            ws_url = heroku_url  # assume already ws:// or wss://

        register_url = f"{ws_url}/register"

        self._local_port = _find_free_port()

        ssl_ctx = _make_ssl_context()
        proc: asyncio.subprocess.Process | None = None

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(register_url, ssl=ssl_ctx) as ws:

                # Wait for "registered" message
                msg = await ws.receive_json()
                if msg.get("type") != "registered":
                    print(
                        f"Unexpected message from relay server: {msg}",
                        file=sys.stderr,
                    )
                    return

                public_url = msg["url"]

                # textual-serve constructs URLs as f"{public_url}{path}" where path
                # starts with "/", so public_url must NOT have a trailing slash.
                serve_public_url = public_url.rstrip("/")

                # Start textual-serve as a subprocess (signal handlers require main thread,
                # so we can't use Server.serve() inside an asyncio thread).
                proc = await asyncio.create_subprocess_exec(
                    sys.argv[0], "serve",
                    "--port", str(self._local_port),
                    "--host", "localhost",
                    "--public-url", serve_public_url,
                )

                # Give textual-serve a moment to bind the port
                await asyncio.sleep(1.5)

                print(f"Live at {public_url}")
                sys.stdout.flush()

                try:
                    # Main tunnel message loop
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                log.warning("Invalid JSON from relay: %s", msg.data[:200])
                                continue
                            await self._dispatch(session, ws, data)
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                        ):
                            log.info("Tunnel WebSocket closed")
                            break
                finally:
                    if proc and proc.returncode is None:
                        proc.terminate()

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        session: Any,
        tunnel_ws: Any,
        data: dict,
    ) -> None:
        msg_type = data.get("type")

        if msg_type == "http_request":
            asyncio.ensure_future(self._handle_http_request(tunnel_ws, data))

        elif msg_type == "ws_connect":
            asyncio.ensure_future(self._handle_ws_connect(session, tunnel_ws, data))

        elif msg_type == "ws_message":
            await self._handle_ws_message(data)

        elif msg_type == "ws_close":
            await self._handle_ws_close(data)

        else:
            log.debug("Unknown message type from relay: %s", msg_type)

    # ------------------------------------------------------------------
    # HTTP proxy
    # ------------------------------------------------------------------

    async def _handle_http_request(self, tunnel_ws: Any, data: dict) -> None:
        import httpx

        req_id = data["id"]
        method = data.get("method", "GET")
        path = data.get("path", "/")
        query = data.get("query", "")
        headers_list = data.get("headers") or []
        body_b64 = data.get("body")

        url = f"http://localhost:{self._local_port}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {k: v for k, v in headers_list}
        body = base64.b64decode(body_b64) if body_b64 else None

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    content=body,
                    follow_redirects=True,
                    timeout=30,
                )
            resp_headers = [
                [k, v]
                for k, v in resp.headers.items()
                if k.lower()
                not in (
                    "transfer-encoding",
                    "connection",
                    "keep-alive",
                )
            ]
            body_b64_resp = base64.b64encode(resp.content).decode()
            await tunnel_ws.send_json({
                "type": "http_response",
                "id": req_id,
                "status": resp.status_code,
                "headers": resp_headers,
                "body": body_b64_resp,
            })
        except Exception as exc:
            log.warning("HTTP proxy error for %s %s: %s", method, url, exc)
            await tunnel_ws.send_json({
                "type": "http_response",
                "id": req_id,
                "status": 502,
                "headers": [],
                "body": base64.b64encode(str(exc).encode()).decode(),
            })

    # ------------------------------------------------------------------
    # WebSocket proxy
    # ------------------------------------------------------------------

    async def _handle_ws_connect(
        self,
        session: Any,
        tunnel_ws: Any,
        data: dict,
    ) -> None:
        import aiohttp

        ws_id = data["id"]
        path = data.get("path", "/ws")
        query = data.get("query", "")

        url = f"ws://localhost:{self._local_port}{path}"
        if query:
            url = f"{url}?{query}"

        try:
            local_ws = await session.ws_connect(url)
        except Exception as exc:
            log.warning("Could not connect local WS %s: %s", url, exc)
            await tunnel_ws.send_json({
                "type": "ws_close",
                "id": ws_id,
                "code": 1011,
                "reason": str(exc),
            })
            return

        self._local_ws[ws_id] = local_ws

        # Acknowledge to relay server
        await tunnel_ws.send_json({"type": "ws_open", "id": ws_id})

        # Relay local → tunnel in background
        asyncio.ensure_future(
            self._relay_local_to_tunnel(ws_id, local_ws, tunnel_ws)
        )

    async def _relay_local_to_tunnel(
        self,
        ws_id: str,
        local_ws: Any,
        tunnel_ws: Any,
    ) -> None:
        import aiohttp

        try:
            async for msg in local_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = base64.b64encode(msg.data.encode()).decode()
                    await tunnel_ws.send_json({
                        "type": "ws_message",
                        "id": ws_id,
                        "data": payload,
                        "binary": False,
                    })
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    payload = base64.b64encode(msg.data).decode()
                    await tunnel_ws.send_json({
                        "type": "ws_message",
                        "id": ws_id,
                        "data": payload,
                        "binary": True,
                    })
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            self._local_ws.pop(ws_id, None)
            if not tunnel_ws.closed:
                try:
                    await tunnel_ws.send_json({
                        "type": "ws_close",
                        "id": ws_id,
                        "code": 1000,
                        "reason": "",
                    })
                except Exception:
                    pass

    async def _handle_ws_message(self, data: dict) -> None:
        ws_id = data.get("id")
        local_ws = self._local_ws.get(ws_id)
        if local_ws is None or local_ws.closed:
            return
        raw = data.get("data", "")
        binary = data.get("binary", False)
        decoded = base64.b64decode(raw) if raw else b""
        if binary:
            await local_ws.send_bytes(decoded)
        else:
            await local_ws.send_str(decoded.decode("utf-8", errors="replace"))

    async def _handle_ws_close(self, data: dict) -> None:
        ws_id = data.get("id")
        local_ws = self._local_ws.pop(ws_id, None)
        if local_ws and not local_ws.closed:
            code = data.get("code", 1000)
            await local_ws.close(code=code)
