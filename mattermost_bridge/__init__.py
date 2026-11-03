"""HiveMind <-> Mattermost bridge.

A HiveMind bridge is a satellite whose input and output are a chat
platform instead of a microphone. This one uses
:class:`~hivemind_bus_client.HiveMessageBusClient` directly (composition)
for the hub, and :mod:`mattermost_bridge.mattermost` (a bot access token,
the REST v4 API and the websocket) for Mattermost.

Connection lifecycle, spelled out because getting it wrong is the
recurring bug across every HiveMind bridge written so far:

- ``HiveMessageBusClient.connect()`` already starts and owns the
  reconnect worker in a background thread, and blocks synchronously
  until the handshake completes (or fails). Call it exactly once, from
  ``start()``. Do not also call ``run_forever()`` afterwards to "keep it
  running" -- there is nothing left to start, the connection is already
  live in its own thread for as long as the process runs.
- Nothing is forwarded to HiveMind before ``connect()`` returns, and the
  bot's own posts (and those of other bots, webhooks and plugins) are
  dropped before they are ever considered, so neither an unauthenticated
  connection nor bots answering each other reaches the hub.
- host/port are configuration, not constants (``ws://127.0.0.1:5678``
  is only ever a default value).
- a freshly registered HiveMind client is denied every message type
  until a hub admin runs ``hivemind-core allow-msg
  recognizer_loop:utterance <client_id>`` (and usually ``speak`` too);
  this bridge cannot do that step itself. See the README.

Threads: the Mattermost websocket runs on an asyncio loop in the thread
that called ``start()``. HiveMind's ``speak`` handler runs on the bus
client's own thread and only queues the reply; one sender thread posts the
queued replies in order, paced, with blocking REST calls that
:class:`~mattermost_bridge.mattermost.MattermostAPI` makes thread safe. The
hub's thread never waits on Mattermost.
"""
import asyncio
import contextlib
import json
import logging
import queue
import random
import threading
import time
from collections import OrderedDict
from typing import Iterable, Optional, Tuple

from hivemind_bus_client import (
    HiveMessage,
    HiveMessageType,
    HiveMessageBusClient,
)
from ovos_bus_client.message import Message
from ovos_utils.log import LOG
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from mattermost_bridge.health import HealthServer
from mattermost_bridge.mattermost import (
    DIRECT,
    DIRECT_CHANNEL_TYPES,
    MAX_POST_LENGTH,
    MattermostAPI,
    Post,
    RateLimited,
    USERNAME_PATTERN,
    TokenRejected,
    defuse_reply,
    mention_pattern,
    parse_posted,
    split_post,
    websocket_host,
)

platform = "HiveMindMattermostBridgeV0.3"

# hivemind-bus-client >= 1.0.13a1 makes HiveMessageBusClient.connect()
# block on the handshake; handshake_max_retries=None (the client's own
# default) retries forever. Bound it here so a stalled/unreachable hub
# (down, wrong password) fails fast instead of hanging the bridge.
DEFAULT_HANDSHAKE_MAX_RETRIES = 10

#: How conversations are kept apart on the hub. ``person``: one session per
#: person in each channel, so in a channel each person's follow-up questions
#: continue their own conversation. ``chat``: one session per channel,
#: shared by everyone in it.
SESSION_SCOPES = ("person", "chat")

#: Direct and group-direct messages: ``auto`` refuses them when an allow-list
#: of channels is set and accepts them otherwise; ``allow`` and ``deny`` say
#: so whatever the allow-list.
DM_POLICIES = ("auto", "allow", "deny")

#: Mentions that notify everyone in a channel; never a person's username.
CHANNEL_WIDE_MENTIONS = frozenset({"all", "channel", "here"})

#: Seconds the server has to confirm the websocket login with its ``hello``.
HELLO_TIMEOUT = 30
#: Reconnect backoff, in seconds, doubling from the first to the last.
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30
#: How many thread roots the bridge remembers the author of.
THREAD_ROOT_CACHE_SIZE = 1024

#: Channels whose type the bridge learned from a post it forwarded (least
#: recently used dropped first). A reply's channel type comes from here, never
#: from the hub's context.
SEEN_CHANNEL_MEMORY = 1024
#: Seconds between checks that the server still accepts the token. Revoking
#: a token does not close the websocket: the server keeps it open and stops
#: sending events on it, so without this the bridge would go deaf and stay
#: "ready".
TOKEN_CHECK_INTERVAL = 60

#: Replies waiting for the sender thread; a reply beyond this is dropped
#: (logged), so a hub that answers faster than Mattermost takes posts cannot
#: grow the bridge's memory without end.
REPLY_QUEUE_SIZE = 100
#: Seconds between two posts by the bridge.
REPLY_INTERVAL = 0.2
#: Tries per post while the server answers 429 (rate limited).
RATE_LIMIT_ATTEMPTS = 3
#: The longest wait a 429 may ask for, in seconds.
RATE_LIMIT_MAX_WAIT = 30
#: A reply longer than one post is split; past this many posts it is cut.
MAX_REPLY_POSTS = 4
#: What ends a cut reply.
CUT_MARK = " [...]"
#: Seconds ``stop()`` gives the queued replies to go out.
REPLY_DRAIN_TIMEOUT = 5

# The websockets library logs every frame at debug level, the token's
# authentication challenge and everyone's messages among them. Give it a
# logger of its own that never goes below warning, whatever the root level.
_WEBSOCKET_LOG = logging.getLogger("mattermost_bridge.websocket")
_WEBSOCKET_LOG.setLevel(logging.WARNING)


def jittered(delay: float) -> float:
    """``delay`` times a random factor between 0.5 and 1.5.

    A server restart drops every bridge's websocket at once; without the
    jitter they would all come back at the same instants, again and again.
    """
    return delay * random.uniform(0.5, 1.5)


class HiveMindMattermostBridge:
    """Bridge a Mattermost bot account to a HiveMind node."""

    def __init__(self,
                 url: Optional[str] = None,
                 token: Optional[str] = None,
                 key: Optional[str] = None,
                 password: Optional[str] = None,
                 host: Optional[str] = None,
                 port: int = 5678,
                 self_signed: bool = False,
                 lang: str = "en-us",
                 site_id: str = "mattermost",
                 allowed_channels: Optional[Iterable[str]] = None,
                 dm_policy: str = "auto",
                 require_mention: bool = True,
                 session_scope: str = "person",
                 health_port: Optional[int] = None,
                 handshake_max_retries: int = DEFAULT_HANDSHAKE_MAX_RETRIES,
                 *,
                 client: Optional[HiveMessageBusClient] = None,
                 api: Optional[MattermostAPI] = None):
        """
        Parameters
        ----------
        url: the Mattermost server's base URL (``https://chat.example.com``).
        token: the bot account's access token. ``url`` and ``token`` are
            required unless ``api`` is supplied (tests inject one).
        key, password, host, port, self_signed: HiveMind hub connection.
        lang: default utterance language tag.
        site_id: this bridge's HiveMind site id.
        allowed_channels: if given, only posts in these channel ids are
            forwarded and answered, whatever the channel type (a listed
            group DM too). Other direct and group-direct messages follow
            ``dm_policy``.
        dm_policy: for direct and group-direct messages that are not
            listed: ``"deny"`` ignores them, ``"allow"`` forwards them,
            ``"auto"`` denies them when ``allowed_channels`` is given and
            allows them otherwise.
        require_mention: outside a one-to-one direct message, forward only
            posts that mention the bot (``@botname``) or that reply in a
            thread the bot started. The mention is removed before
            forwarding.
        session_scope: ``"person"`` (one hub session per person in each
            channel, the default) or ``"chat"`` (one per channel).
        health_port: if set, serve ``/healthz`` and ``/readyz`` on this
            port (see :mod:`mattermost_bridge.health`).
        handshake_max_retries: bounds how many times ``connect()`` retries
            the HiveMind handshake before giving up. ``None`` retries
            forever, which hangs the bridge on a stalled/unreachable hub.
        client: pre-built HiveMessageBusClient (tests / advanced setups).
            NOTE: HiveMessageBusClient does NOT open a connection in
            __init__ -- call start() (or connect_hivemind()) to connect.
        api: pre-built MattermostAPI (tests).
        """
        if api is None and not (url and token):
            raise ValueError("url and token are required unless an API is injected")
        if session_scope not in SESSION_SCOPES:
            raise ValueError(f"session_scope must be one of {SESSION_SCOPES}, "
                             f"not {session_scope!r}")
        if dm_policy not in DM_POLICIES:
            raise ValueError(f"dm_policy must be one of {DM_POLICIES}, not {dm_policy!r}")

        self.lang = lang
        self.site_id = site_id
        self.allowed_channels = set(allowed_channels) if allowed_channels else None
        if dm_policy == "auto":
            dm_policy = "deny" if self.allowed_channels is not None else "allow"
        self.dm_policy = dm_policy
        self.require_mention = require_mention
        self.session_scope = session_scope
        self.handshake_max_retries = handshake_max_retries

        self.api: MattermostAPI = api or MattermostAPI(url, token)

        # The client is given no identity, so it names itself
        # "unnamed-node" and keeps its Noise static key at
        # ~/.config/hivemind/unnamed-node_noise.key: the path a managed
        # deployment mounts the key at.
        self.client = client or HiveMessageBusClient(
            key=key,
            password=password,
            host=host,
            port=port,
            useragent=platform,
            self_signed=self_signed,
        )

        self.health_port = health_port
        self._health: Optional[HealthServer] = None
        self._started = False
        self._connected = threading.Event()
        self._listening = threading.Event()
        self._token_rejected = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._websocket = None
        self.bot_id: Optional[str] = None
        self.bot_username: Optional[str] = None
        self._mention = None
        self._roots: "OrderedDict[str, bool]" = OrderedDict()
        self._roots_lock = threading.Lock()
        # channel id -> the channel type Mattermost gave on a forwarded post
        self._seen_channels: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._seen_lock = threading.Lock()
        # Replies: queued by the hub's thread, posted by the sender thread.
        # The count is the bound (the queue itself is unbounded so that the
        # sender's wake-up on stop always fits).
        self._outbox: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._unsent = 0
        self._unsent_changed = threading.Condition()
        self._sender: Optional[threading.Thread] = None
        self._sender_lock = threading.Lock()
        self._sender_stop = threading.Event()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect_hivemind(self) -> None:
        """Connect to the HiveMind hub and wait for the handshake.

        Calls ``HiveMessageBusClient.connect()`` exactly once; that call
        already starts and owns the reconnect worker thread. Never call
        ``run_forever()`` in addition to this -- it would try to claim
        the same lifecycle a second time.
        """
        self.client.connect(site_id=self.site_id,
                            handshake_max_retries=self.handshake_max_retries)
        self.client.on_mycroft("speak", self.handle_speak)
        self.client.on_mycroft("hive.complete_intent_failure",
                               self.handle_intent_failure)
        self._connected.set()
        LOG.info("== connected to HiveMind")

    def start(self) -> None:
        """Connect to HiveMind, then listen to Mattermost until stopped.

        Blocks the calling thread for as long as the bridge runs. Intended
        to be the last call in a ``__main__``. Raises
        :class:`~mattermost_bridge.mattermost.TokenRejected` when the
        Mattermost server refuses the bot's token, at start or later.
        """
        if self._started:
            return
        self._started = True
        if self.health_port is not None:
            self._health = HealthServer(self.health_port, self.ready)
            self._health.start()
        self.connect_hivemind()
        LOG.warning(
            "a freshly registered HiveMind client is denied every message "
            "type until an admin runs `hivemind-core allow-msg "
            "recognizer_loop:utterance <client_id>` on the hub (and "
            "usually `speak` too). If messages seem to vanish silently, "
            "check that first."
        )
        try:
            asyncio.run(self._run_mattermost())
        except (TokenRejected, KeyboardInterrupt):
            self.stop()
            raise

    def stop(self) -> None:
        """Stop listening to Mattermost and close the HiveMind connection."""
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:  # the loop already closed
                pass
        try:
            self.client.close()
        except Exception:
            LOG.exception("error closing HiveMind client")
        self._started = False
        self._connected.clear()
        self._stop_replies(REPLY_DRAIN_TIMEOUT)
        if self._health is not None:
            self._health.stop()
            self._health = None

    def ready(self) -> bool:
        """Whether a message can go end to end right now.

        The HiveMind session must be live, not just once established: the
        client clears its handshake event when the connection drops and sets
        it again on reconnect. And the Mattermost websocket must be open and
        logged in: the server has said ``hello`` on it and it has not closed
        since.
        """
        return self._hub_up() and self._listening.is_set()

    def _hub_up(self) -> bool:
        handshake = getattr(self.client, "handshake_event", None)
        return bool(self._connected.is_set() and (handshake is None or handshake.is_set()))

    def _request_stop(self) -> None:
        """On the loop: stop reconnecting and close the websocket."""
        if self._stop_event is not None:
            self._stop_event.set()
        websocket = self._websocket
        if websocket is not None and self._loop is not None:
            self._loop.create_task(websocket.close())

    def _reject_token(self) -> None:
        """From any thread: the token is refused, stop the bridge."""
        self._token_rejected.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                pass

    async def _sleep(self, seconds: float) -> None:
        """Wait ``seconds``, or less if the bridge is asked to stop."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _stopping(self) -> bool:
        stop = self._stop_event
        return (stop is not None and stop.is_set()) or self._token_rejected.is_set()

    async def _run_mattermost(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._token_rejected.is_set():
            raise TokenRejected()
        try:
            if await self._log_in():
                watcher = asyncio.create_task(self._watch_token())
                try:
                    await self._listen_until_stopped()
                finally:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
        finally:
            self._listening.clear()
            self._websocket = None
            self._loop = None
        if self._token_rejected.is_set():
            raise TokenRejected()

    async def _log_in(self) -> bool:
        """Learn the bot's id and username from the token; retry while the server is unreachable.

        Returns ``False`` if the bridge was stopped first. Raises
        ``TokenRejected`` when the server refuses the token.
        """
        delay = RECONNECT_MIN_DELAY
        while not self._stopping():
            try:
                await asyncio.to_thread(self.identify)
            except TokenRejected:
                self._token_rejected.set()
                raise
            except Exception as error:
                wait = jittered(delay)
                LOG.warning(f"cannot reach Mattermost ({type(error).__name__}); "
                            f"retrying in {wait:.1f} s")
                await self._sleep(wait)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                continue
            LOG.info(f"== logged in to Mattermost as @{self.bot_username}")
            return True
        return False

    def identify(self) -> None:
        """Ask the server who the token belongs to: the bot's id and ``@username``.

        Raises ``TokenRejected`` when the server refuses the token.
        """
        me = self.api.me()
        bot_id = str(me.get("id") or "")
        username = str(me.get("username") or "")
        if not bot_id or not username:
            raise ValueError("Mattermost did not say who the token belongs to")
        self.bot_id = bot_id
        self.bot_username = username
        self._mention = mention_pattern(username)

    async def _listen_until_stopped(self) -> None:
        delay = RECONNECT_MIN_DELAY
        while not self._stopping():
            logged_in = False
            try:
                logged_in = await self._listen()
            except TokenRejected:
                self._token_rejected.set()
                raise
            except (OSError, asyncio.TimeoutError, WebSocketException) as error:
                LOG.warning(f"Mattermost websocket failed: {type(error).__name__}: {error}")
            finally:
                self._listening.clear()
                self._websocket = None
            if self._stopping():
                break
            if logged_in:
                delay = RECONNECT_MIN_DELAY
            else:
                await self._check_token()
            wait = jittered(delay)
            LOG.info(f"reconnecting to Mattermost in {wait:.1f} s")
            await self._sleep(wait)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _watch_token(self) -> None:
        """Every ``TOKEN_CHECK_INTERVAL`` seconds, ask the server whether the token still works.

        A refused token stops the bridge (``TokenRejected`` from ``start()``);
        an unreachable server is the websocket loop's business.
        """
        while not self._stopping():
            await self._sleep(TOKEN_CHECK_INTERVAL)
            if self._stopping():
                return
            try:
                await asyncio.to_thread(self.api.me)
            except TokenRejected:
                LOG.error("Mattermost no longer accepts the bot's access token")
                self._token_rejected.set()
                self._request_stop()
                return
            except Exception:
                pass

    async def _check_token(self) -> None:
        """After a websocket that never logged in: is it the token? Raises ``TokenRejected`` if so."""
        try:
            await asyncio.to_thread(self.api.me)
        except TokenRejected:
            self._token_rejected.set()
            raise
        except Exception:  # the server is unreachable; the next attempt will tell
            pass

    async def _listen(self) -> bool:
        """One websocket connection, until it closes. Returns whether it ever logged in."""
        url = self.api.websocket_url
        options = {"ssl": self.api.ssl_context()} if url.startswith("wss://") else {}
        logged_in = False
        async with websocket_connect(url, open_timeout=HELLO_TIMEOUT, close_timeout=5,
                                     ping_interval=20, ping_timeout=20, max_size=2 ** 22,
                                     user_agent_header=platform, logger=_WEBSOCKET_LOG,
                                     **options) as websocket:
            self._websocket = websocket
            if self._stopping():
                return False
            request = getattr(websocket, "request", None)
            final_host = request.headers.get("Host") if request is not None else None
            if final_host != websocket_host(url):
                # A redirect took the handshake to another host (or port, or
                # scheme). The token would go to whoever answers there.
                LOG.error("the Mattermost websocket was redirected to another host; "
                          "not sending the token there. Set MATTERMOST_URL to the "
                          "server's own address.")
                return False
            await websocket.send(json.dumps({
                "seq": 1,
                "action": "authentication_challenge",
                "data": {"token": self.api.token},
            }))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + HELLO_TIMEOUT
            while True:
                timeout = None if logged_in else max(0.0, deadline - loop.time())
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    LOG.warning("Mattermost did not confirm the websocket login")
                    return False
                except ConnectionClosed:
                    return logged_in
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if kind == "hello":
                    logged_in = True
                    self._listening.set()
                    LOG.info("== listening to Mattermost")
                elif kind == "posted" and logged_in:
                    try:
                        await self.handle_posted(event)
                    except TokenRejected:
                        raise
                    except Exception as error:
                        # One bad post must not take the listener down. The
                        # type only: a message could quote what was written.
                        LOG.error(f"could not handle a Mattermost post: {type(error).__name__}")

    # ------------------------------------------------------------------
    # Mattermost -> HiveMind
    # ------------------------------------------------------------------
    async def handle_posted(self, event: dict) -> None:
        """Forward a ``posted`` websocket event onto the HiveMind bus.

        Drops the post instead of forwarding when: it has no text, it is a
        system post, it was written by the bot itself or by another bot, a
        webhook or a plugin (two bots answering each other would never
        stop), it is in a channel outside ``allowed_channels`` (when
        configured), it is a direct or group-direct message and
        ``dm_policy`` refuses those, it is not addressed to the bot (see
        :meth:`addressed_text`), or HiveMind is not connected.
        """
        post = parse_posted(event)
        if post is None or not post.message or not post.channel_id:
            return
        if post.type or post.automated:
            return
        if not post.user_id or post.user_id == self.bot_id:
            return

        if not self.may_answer(post.channel_id, post.channel_type):
            # Silently: answering every stranger's DM would let anyone make
            # the bot post, and say that it exists.
            LOG.debug(f"ignoring a post in non-allowed channel {post.channel_id}")
            return

        text = await self.addressed_text(post)
        if text is None:
            return

        if not self._hub_up():
            LOG.warning("dropping Mattermost message, not connected to HiveMind")
            return

        self._remember_channel(post.channel_id, post.channel_type)
        self.forward_to_hivemind(text, post)

    def _remember_channel(self, channel_id: str, channel_type: Optional[str]) -> None:
        with self._seen_lock:
            self._seen_channels[channel_id] = channel_type
            self._seen_channels.move_to_end(channel_id)
            while len(self._seen_channels) > SEEN_CHANNEL_MEMORY:
                self._seen_channels.popitem(last=False)

    def seen_channel_type(self, channel_id: str) -> Tuple[bool, Optional[str]]:
        """``(seen, channel_type)``: the type of ``channel_id`` as Mattermost gave it."""
        with self._seen_lock:
            if channel_id in self._seen_channels:
                return True, self._seen_channels[channel_id]
        return False, None

    def may_reply(self, channel_id: str) -> bool:
        """Whether a reply the hub sends may go to ``channel_id``.

        The hub's context is not trusted for the channel type: with
        ``dm_policy="allow"`` a context calling an unlisted public channel
        ``D`` would get past ``allowed_channels``. A listed channel is
        answered; otherwise the type comes from the post the bridge itself
        forwarded from that channel. A channel it never saw is answered only
        when every channel type would be (no list, and DMs allowed).
        """
        if self.allowed_channels is not None and channel_id in self.allowed_channels:
            return True
        seen, channel_type = self.seen_channel_type(channel_id)
        if seen:
            return self.may_answer(channel_id, channel_type)
        return self.allowed_channels is None and self.dm_policy == "allow"

    def may_answer(self, channel_id: str, channel_type: Optional[str]) -> bool:
        """Whether the bridge talks in ``channel_id`` at all, both ways.

        A channel id in ``allowed_channels`` is answered whatever its type
        (a listed group DM too). Any other direct or group-direct channel
        follows ``dm_policy``; any other channel is answered only when no
        list is set.
        """
        if self.allowed_channels is not None and channel_id in self.allowed_channels:
            return True
        if channel_type in DIRECT_CHANNEL_TYPES:
            return self.dm_policy == "allow"
        return self.allowed_channels is None

    async def addressed_text(self, post: Post) -> Optional[str]:
        """The text to forward, or ``None`` when the post is not for the bot.

        The bot's ``@username`` is removed wherever it appears: a question
        in a channel reads "@botname what time is it", and the mention is
        not part of what was asked. Outside a one-to-one direct message,
        with ``require_mention``, a post that neither mentions the bot nor
        replies in a thread the bot started is not addressed to it.
        """
        text = post.message
        mentioned = False
        if self._mention is not None:
            text, count = self._mention.subn(" ", text)
            mentioned = count > 0
        text = " ".join(text.split())
        if not text:
            return None
        if self.require_mention and post.channel_type != DIRECT and not mentioned:
            if not await self._in_bot_thread(post.root_id):
                return None
        return text

    async def _in_bot_thread(self, root_id: str) -> bool:
        """Whether the thread ``root_id`` was started by the bot."""
        if not root_id or not self.bot_id:
            return False
        with self._roots_lock:
            known = self._roots.get(root_id)
        if known is not None:
            return known
        try:
            root = await asyncio.to_thread(self.api.get_post, root_id)
        except TokenRejected:
            self._token_rejected.set()
            raise
        except Exception as error:
            LOG.warning(f"cannot tell who started thread {root_id}: {type(error).__name__}")
            return False
        started_by_bot = root.get("user_id") == self.bot_id
        self._remember_root(root_id, started_by_bot)
        return started_by_bot

    def _remember_root(self, post_id: str, started_by_bot: bool) -> None:
        with self._roots_lock:
            self._roots[post_id] = started_by_bot
            self._roots.move_to_end(post_id)
            while len(self._roots) > THREAD_ROOT_CACHE_SIZE:
                self._roots.popitem(last=False)

    def forward_to_hivemind(self, text: str, post: Post) -> None:
        msg = Message(
            "recognizer_loop:utterance",
            {"utterances": [text], "lang": self.lang},
            {
                "source": platform,
                "destination": "HiveMind",
                "platform": platform,
                "channel": post.channel_id,
                "channel_type": post.channel_type,
                # the thread to answer in: the question's, or none
                "root_id": post.root_id,
                "user": {"mattermost_username": post.sender_username,
                         "mattermost_user_id": post.user_id},
                "session": {"session_id": self.session_id(post.channel_id, post.user_id)},
            },
        )
        self.client.emit(HiveMessage(HiveMessageType.BUS, msg))

    def session_id(self, channel_id: str, user_id: Optional[str]) -> str:
        """The hub session a post by ``user_id`` in ``channel_id`` belongs to."""
        if self.session_scope == "person" and user_id:
            return f"mattermost-{channel_id}-{user_id}"
        return f"mattermost-{channel_id}"

    # ------------------------------------------------------------------
    # HiveMind -> Mattermost
    # ------------------------------------------------------------------
    def handle_speak(self, message: Message) -> None:
        utterance = message.data.get("utterance")
        if not utterance:
            return
        self._reply(utterance, message.context)

    def handle_intent_failure(self, message: Message) -> None:
        if not message.context.get("channel"):
            return
        LOG.error("complete intent failure")
        self._reply("I don't know how to answer that", message.context)

    def _reply(self, text: str, context: dict) -> None:
        channel_id = context.get("channel")
        if not channel_id or not isinstance(channel_id, str):
            return
        if not self.may_reply(channel_id):
            # The hub only echoes the context of what was forwarded, so this
            # is a reply to a channel the bridge no longer serves (a reminder
            # set before the list changed or before a restart) or a context
            # the hub made up.
            LOG.warning(f"not replying in non-allowed channel {channel_id}")
            return
        seen, channel_type = self.seen_channel_type(channel_id)
        if not seen:
            channel_type = context.get("channel_type")  # only decides the @asker prefix
        user = context.get("user") if isinstance(context.get("user"), dict) else {}
        self._queue_reply({
            "text": text,
            "channel_id": channel_id,
            "root_id": context.get("root_id") or "",
            "username": user.get("mattermost_username"),
            "channel_type": channel_type,
        })

    def _queue_reply(self, reply: dict) -> None:
        """Hand ``reply`` (``speak``'s arguments) to the sender thread; never blocks."""
        with self._unsent_changed:
            if self._unsent >= REPLY_QUEUE_SIZE:
                LOG.warning(f"dropping a reply for channel {reply['channel_id']}: "
                            f"{REPLY_QUEUE_SIZE} replies are already waiting")
                return
            self._unsent += 1
        self._ensure_sender()
        self._outbox.put(reply)

    def _ensure_sender(self) -> None:
        with self._sender_lock:
            if self._sender is not None and self._sender.is_alive():
                return
            self._sender_stop.clear()
            self._sender = threading.Thread(target=self._send_replies,
                                            name="mattermost-replies", daemon=True)
            self._sender.start()

    def _send_replies(self) -> None:
        """The sender thread: post queued replies in order, ``REPLY_INTERVAL`` apart."""
        last = None
        while True:
            reply = self._outbox.get()
            if reply is None:
                return
            try:
                if self._sender_stop.is_set() or self._token_rejected.is_set():
                    continue  # stopping: what is left is dropped
                if last is not None:
                    wait = last + REPLY_INTERVAL - time.monotonic()
                    if wait > 0 and self._sender_stop.wait(wait):
                        continue
                self.speak(**reply)
                last = time.monotonic()
            except Exception as error:  # the sender must outlive one bad reply
                LOG.error(f"could not send a reply: {type(error).__name__}")
            finally:
                with self._unsent_changed:
                    self._unsent -= 1
                    self._unsent_changed.notify_all()

    def flush_replies(self, timeout: float = REPLY_DRAIN_TIMEOUT) -> bool:
        """Wait until every queued reply was posted (or given up); ``False`` on timeout."""
        with self._unsent_changed:
            return self._unsent_changed.wait_for(lambda: self._unsent == 0, timeout)

    def _stop_replies(self, timeout: float) -> None:
        """Give the queued replies ``timeout`` seconds, drop the rest, end the sender."""
        sender = self._sender
        if sender is None:
            return
        if not self._token_rejected.is_set() and not self.flush_replies(timeout):
            LOG.warning("stopping with replies not yet posted; they are dropped")
        self._sender_stop.set()  # also ends a rate-limit wait
        self._outbox.put(None)
        sender.join(2)
        with self._sender_lock:
            if self._sender is sender:
                self._sender = None

    def speak(self, text: str, channel_id: str, root_id: str = "",
              username: Optional[str] = None, channel_type: Optional[str] = None) -> None:
        """Post ``text`` in ``channel_id``, in the thread ``root_id`` when given.

        The text is the hub's, so its mentions are broken and its images
        escaped first (:func:`~mattermost_bridge.mattermost.defuse_reply`):
        a reply never pings ``@channel``, ``@all``, ``@here``, a group or a
        person. Outside a one-to-one direct message the reply then starts
        with the asker's ``@username``, the one mention the bridge makes, so
        in a busy channel it is clear whom it answers. A reply longer than
        one post is split, at most ``MAX_REPLY_POSTS`` posts; a post the
        server rate limits is retried (``RATE_LIMIT_ATTEMPTS``). Blocking:
        the sender thread calls it (``handle_speak`` only queues).
        """
        text = defuse_reply(str(text))
        prefix = ""
        if (username and channel_type != DIRECT and USERNAME_PATTERN.fullmatch(str(username))
                and str(username).lower() not in CHANNEL_WIDE_MENTIONS):
            prefix = f"@{username} "
        # One post holds MAX_POST_LENGTH characters: a longer reply is split
        # (the first part names the asker), and past MAX_REPLY_POSTS cut.
        limit = MAX_POST_LENGTH - len(prefix)
        parts = split_post(text, limit)
        if len(parts) > MAX_REPLY_POSTS:
            LOG.warning(f"a reply of {len(text)} characters is cut to {MAX_REPLY_POSTS} posts")
            parts = parts[:MAX_REPLY_POSTS]
            parts[-1] = parts[-1][:limit - len(CUT_MARK)] + CUT_MARK
        for number, part in enumerate(parts):
            if self._sender_stop.is_set():
                # stopping began while an earlier part was in flight
                LOG.warning(f"stopping: {len(parts) - number} part(s) of a reply not posted")
                return
            message = prefix + part if number == 0 else part
            # The length, never the text: replies carry what people asked about,
            # and a debug log is kept, shipped and read far more widely than the chat.
            LOG.debug(f"Sending message to Mattermost channel {channel_id}: "
                      f"{len(message)} characters")
            post = self._post(channel_id, message, root_id)
            if post is None:
                return
            if not root_id and post.get("id"):
                # A top-level reply can become a thread; replies in it are for the bot.
                self._remember_root(str(post["id"]), True)

    def _post(self, channel_id: str, message: str, root_id: str) -> Optional[dict]:
        """One post, retried while the server rate limits it; ``None`` when it was not posted."""
        for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
            try:
                post = self.api.create_post(channel_id, message, root_id=root_id)
            except RateLimited as limited:
                if attempt == RATE_LIMIT_ATTEMPTS:
                    break
                wait = min(limited.retry_after, RATE_LIMIT_MAX_WAIT)
                LOG.warning(f"Mattermost rate limit on channel {channel_id}; "
                            f"retrying in {wait:.1f} s")
                if self._sender_stop.wait(wait):
                    return None
                continue
            except TokenRejected:
                LOG.error("Mattermost refused the bot's access token")
                self._reject_token()
                return None
            except Exception as error:
                LOG.error(f"failed to post to Mattermost channel {channel_id}: "
                          f"{type(error).__name__}: {error}")
                return None
            return post if isinstance(post, dict) else {}
        LOG.error(f"failed to post to Mattermost channel {channel_id}: "
                  f"still rate limited after {RATE_LIMIT_ATTEMPTS} tries")
        return None


# Backwards-compat alias for the old public name.
JarbasMattermostBridge = HiveMindMattermostBridge

__all__ = ["HiveMindMattermostBridge", "JarbasMattermostBridge", "TokenRejected", "platform"]
