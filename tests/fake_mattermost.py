"""A small Mattermost server for tests: the REST calls and the websocket the bridge uses.

It listens on 127.0.0.1 with real sockets, so the bridge's own ``requests``
and ``websockets`` code is what talks to it; nothing in the bridge is mocked.
It answers like Mattermost does where the bridge depends on it:

- every REST call needs ``Authorization: Bearer <token>``, else 401;
- the websocket expects an ``authentication_challenge`` carrying the token,
  then says ``hello``; a wrong token gets the socket closed, as the real
  server does;
- ``posted`` events carry the post as a JSON string.
"""
import asyncio
import itertools
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional

from websockets.asyncio.server import serve

TOKEN = "fake-bot-token-0123456789"
BOT_ID = "b0b0b0b0b0b0b0b0b0b0b0b0b0"
BOT_USERNAME = "hivebot"
ALICE = "a1a1a1a1a1a1a1a1a1a1a1a1a1"
BOB = "b2b2b2b2b2b2b2b2b2b2b2b2b2"
TOWN_SQUARE = "c0c0c0c0c0c0c0c0c0c0c0c0c0"
OFF_TOPIC = "c1c1c1c1c1c1c1c1c1c1c1c1c1"
DM_CHANNEL = "d0d0d0d0d0d0d0d0d0d0d0d0d0"
GROUP_CHANNEL = "e0e0e0e0e0e0e0e0e0e0e0e0e0"

_ids = itertools.count(1)
# The server's own websocket logger would print the token it receives.
_QUIET = logging.getLogger("tests.fake_mattermost")
_QUIET.setLevel(logging.WARNING)


def new_id(prefix: str = "p") -> str:
    """A fresh 26-character Mattermost-shaped id."""
    return f"{prefix}{next(_ids):0{26 - len(prefix)}d}"


def posted_event(message: str, *, channel_id: str = TOWN_SQUARE, channel_type: str = "O",
                 user_id: str = ALICE, username: str = "alice", root_id: str = "",
                 post_id: Optional[str] = None, post_type: str = "",
                 props: Optional[dict] = None) -> dict:
    """A websocket ``posted`` event as Mattermost sends it."""
    post = {
        "id": post_id or new_id(),
        "create_at": int(time.time() * 1000),
        "user_id": user_id,
        "channel_id": channel_id,
        "root_id": root_id,
        "message": message,
        "type": post_type,
        "props": props or {},
    }
    return {
        "event": "posted",
        "data": {
            "channel_display_name": "Town Square",
            "channel_name": "town-square",
            "channel_type": channel_type,
            "post": json.dumps(post),
            "sender_name": f"@{username}",
            "team_id": "t0t0t0t0t0t0t0t0t0t0t0t0t0",
        },
        "broadcast": {"channel_id": channel_id},
        "seq": next(_ids),
    }


class FakeMattermost:
    """Serve the bridge's slice of the Mattermost API from two local ports."""

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.accept_token = True
        self.say_hello = True
        self.posts: Dict[str, dict] = {}
        self.created: List[dict] = []
        self.calls: List[str] = []
        self.auth_attempts = 0
        #: Answer this many of the next ``POST /posts`` with 429 and ``Retry-After``.
        self.rate_limit_posts = 0
        self.retry_after = "0.05"
        self._connections: set = set()
        self._http: Optional[ThreadingHTTPServer] = None
        self._loop = asyncio.new_event_loop()
        self._ws_server = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "FakeMattermost":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake._rest(self, "GET")

            def do_POST(self) -> None:
                fake._rest(self, "POST")

            def log_message(self, format: str, *args: object) -> None:
                return

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._http.daemon_threads = True
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._ws_server = asyncio.run_coroutine_threadsafe(self._serve(), self._loop).result(5)
        return self

    async def _serve(self):
        return await serve(self._websocket, "127.0.0.1", 0, logger=_QUIET)

    def stop(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            asyncio.run_coroutine_threadsafe(self._ws_server.wait_closed(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._http.server_address[1]}"

    @property
    def websocket_url(self) -> str:
        port = next(iter(self._ws_server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/api/v4/websocket"

    def api(self, token: Optional[str] = None):
        """A MattermostAPI pointed at this server."""
        from mattermost_bridge.mattermost import MattermostAPI

        return MattermostAPI(self.url, token or self.token, websocket_url=self.websocket_url,
                             timeout=5)

    # -- websocket -----------------------------------------------------
    async def _websocket(self, websocket) -> None:
        try:
            request = json.loads(await websocket.recv())
        except Exception:
            return
        self.auth_attempts += 1
        data = request.get("data") if isinstance(request, dict) else None
        if (request.get("action") != "authentication_challenge" or not isinstance(data, dict)
                or data.get("token") != self.token or not self.accept_token):
            await websocket.close()
            return
        await websocket.send(json.dumps({"status": "OK", "seq_reply": request.get("seq")}))
        if self.say_hello:
            await websocket.send(json.dumps({"event": "hello", "data": {"server_version": "10.5.0"},
                                             "broadcast": {}, "seq": 0}))
        self._connections.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            self._connections.discard(websocket)

    def push(self, event: dict) -> None:
        """Send ``event`` to every logged-in websocket.

        Like the real server, nothing is sent once the token is revoked; the
        sockets stay open.
        """
        if not self.accept_token:
            return

        async def send():
            for websocket in list(self._connections):
                await websocket.send(json.dumps(event))
        asyncio.run_coroutine_threadsafe(send(), self._loop).result(5)

    def drop(self) -> None:
        """Close every websocket, as a server restart or a network cut would."""
        async def close():
            for websocket in list(self._connections):
                await websocket.close()
        asyncio.run_coroutine_threadsafe(close(), self._loop).result(5)

    @property
    def connected(self) -> int:
        return len(self._connections)

    # -- REST ----------------------------------------------------------
    def _rest(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        self.calls.append(f"{method} {handler.path}")
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        if handler.headers.get("Authorization") != f"Bearer {self.token}" or not self.accept_token:
            return self._answer(handler, 401, {"id": "api.context.session_expired.app_error",
                                               "status_code": 401})
        if method == "GET" and handler.path == "/api/v4/users/me":
            return self._answer(handler, 200, {"id": BOT_ID, "username": BOT_USERNAME})
        if method == "GET" and handler.path.startswith("/api/v4/posts/"):
            post = self.posts.get(handler.path.rsplit("/", 1)[1])
            if post is None:
                return self._answer(handler, 404, {"status_code": 404})
            return self._answer(handler, 200, post)
        if method == "POST" and handler.path == "/api/v4/posts" and self.rate_limit_posts > 0:
            self.rate_limit_posts -= 1
            return self._answer(handler, 429, {"id": "api.context.rate_limit", "status_code": 429},
                                headers={"Retry-After": self.retry_after,
                                         "X-Ratelimit-Reset": self.retry_after})
        if method == "POST" and handler.path == "/api/v4/posts":
            request = json.loads(body or b"{}")
            post = {"id": new_id(), "user_id": BOT_ID, "root_id": request.get("root_id", ""),
                    "channel_id": request.get("channel_id"), "message": request.get("message")}
            self.posts[post["id"]] = post
            self.created.append(request)
            return self._answer(handler, 201, post)
        return self._answer(handler, 404, {"status_code": 404})

    @staticmethod
    def _answer(handler: BaseHTTPRequestHandler, status: int, payload: dict,
                headers: Optional[Dict[str, str]] = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def add_post(self, user_id: str, channel_id: str = TOWN_SQUARE, root_id: str = "") -> str:
        """Store a post (a thread root, say) and return its id."""
        post_id = new_id()
        self.posts[post_id] = {"id": post_id, "user_id": user_id, "channel_id": channel_id,
                               "root_id": root_id, "message": "(root)"}
        return post_id


def make_hub():
    """A HiveMessageBusClient stand-in whose session is up."""
    from unittest.mock import MagicMock

    hub = MagicMock(name="HiveMessageBusClient")
    hub.handshake_event = threading.Event()
    hub.handshake_event.set()
    return hub


def bridge_with(**kwargs):
    """A bridge with a stand-in hub and REST API, connected and logged in, no sockets."""
    from unittest.mock import MagicMock

    from mattermost_bridge import HiveMindMattermostBridge

    api = MagicMock(name="MattermostAPI")
    api.me.return_value = {"id": BOT_ID, "username": BOT_USERNAME}
    api.create_post.side_effect = lambda channel_id, message, root_id="": {"id": new_id()}
    hub = make_hub()
    bridge = HiveMindMattermostBridge(client=hub, api=api, **kwargs)
    bridge.connect_hivemind()
    bridge.identify()
    return bridge, hub, api


def forwarded(bridge, hub, event):
    """The utterance message ``event`` became on the hub, or ``None``."""
    import asyncio

    hub.emit.reset_mock()
    asyncio.run(bridge.handle_posted(event))
    if not hub.emit.called:
        return None
    return hub.emit.call_args[0][0].payload


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class Redirector:
    """A websocket endpoint that answers every handshake with a redirect, and the place it points to.

    ``elsewhere`` is a second websocket server, standing for another host:
    it accepts the handshake and records every frame it is sent, so a test
    can tell whether the bot's token followed the redirect.
    """

    def __init__(self, status: int = 302):
        self.status = status
        self.received: List[str] = []
        self.handshakes = 0
        self._loop = asyncio.new_event_loop()
        self._servers: list = []

    def start(self) -> "Redirector":
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

        async def serve_both():
            elsewhere = await serve(self._record, "127.0.0.1", 0, logger=_QUIET)
            target = f"ws://127.0.0.1:{self._port(elsewhere)}/api/v4/websocket"

            def redirect(connection, request):
                response = connection.respond(self.status, "moved\n")
                response.headers["Location"] = target
                return response

            redirector = await serve(self._record, "127.0.0.1", 0, logger=_QUIET,
                                     process_request=redirect)
            return [redirector, elsewhere]

        self._servers = asyncio.run_coroutine_threadsafe(serve_both(), self._loop).result(5)
        return self

    @staticmethod
    def _port(server) -> int:
        return next(iter(server.sockets)).getsockname()[1]

    async def _record(self, websocket) -> None:
        self.handshakes += 1
        try:
            async for frame in websocket:
                self.received.append(frame if isinstance(frame, str) else frame.decode())
        except Exception:
            return

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self._port(self._servers[0])}/api/v4/websocket"

    def stop(self) -> None:
        async def close():
            for server in self._servers:
                server.close()
                await server.wait_closed()
        asyncio.run_coroutine_threadsafe(close(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
