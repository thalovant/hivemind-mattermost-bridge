"""The Mattermost side of the bridge: a bot token, a few REST v4 calls, the websocket.

Only what the bridge uses, written against the Mattermost API v4 directly:

- ``GET /api/v4/users/me``: who the token belongs to (the bot's id and
  username), and whether the server accepts the token at all.
- ``GET /api/v4/posts/{id}``: who started a thread, so a reply in a thread
  the bot started counts as addressed to the bot.
- ``POST /api/v4/posts``: the bot's reply, in the question's channel and,
  when the question was in a thread, in that thread.
- the websocket at ``/api/v4/websocket``, authenticated with the same token
  through an ``authentication_challenge``, for ``posted`` events.

Every call sends the token as ``Authorization: Bearer``; nothing here logs a
token, a message or a reply. A 401 from the server raises
:class:`TokenRejected`, the one failure no retry can fix.
"""
import json
import os
import re
import ssl
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests

API_PATH = "/api/v4"
DEFAULT_TIMEOUT = 10

#: Mattermost ids (users, channels, posts) are 26 lowercase letters and digits.
ID_PATTERN = re.compile(r"[a-z0-9]{26}")

#: The characters a Mattermost username may hold.
_USERNAME_CHARS = "a-z0-9._-"
USERNAME_PATTERN = re.compile(rf"[{_USERNAME_CHARS}]+", re.IGNORECASE)

#: Channel types (``channel_type`` on a ``posted`` event): ``O`` public,
#: ``P`` private, ``D`` a direct message between two people, ``G`` a group
#: direct message.
DIRECT = "D"
GROUP_DIRECT = "G"
DIRECT_CHANNEL_TYPES = frozenset({DIRECT, GROUP_DIRECT})

#: Post props that mark a post as written by software, not by a person.
_AUTOMATED_PROPS = ("from_bot", "from_webhook", "from_plugin")


class TokenRejected(Exception):
    """The Mattermost server refused the bot's access token (HTTP 401)."""


class MattermostError(Exception):
    """A Mattermost REST call failed for a reason other than the token.

    The message names the call and the HTTP status, never a request or a
    response body: either can hold what someone wrote.
    """

    def __init__(self, method: str, endpoint: str, status: int):
        super().__init__(f"{method} {endpoint} answered HTTP {status}")
        self.status = status


class RateLimited(MattermostError):
    """The server refused a call for its rate limit (HTTP 429); ``retry_after`` seconds to wait."""

    def __init__(self, method: str, endpoint: str, retry_after: float):
        super().__init__(method, endpoint, 429)
        self.retry_after = retry_after


#: Seconds to wait after a 429 that names no time.
DEFAULT_RETRY_AFTER = 1.0


def retry_after(headers: Any) -> float:
    """Seconds a 429 asks to wait: ``Retry-After``, else ``X-Ratelimit-Reset`` (Mattermost sends both)."""
    for name in ("Retry-After", "X-Ratelimit-Reset"):
        try:
            seconds = float(headers.get(name))
        except (TypeError, ValueError):  # absent, or an HTTP date
            continue
        if seconds >= 0:
            return seconds
    return DEFAULT_RETRY_AFTER


#: The longest post Mattermost takes by default (``MaxPostSize``), in
#: characters (the server counts code points, as ``len`` does).
MAX_POST_LENGTH = 16383


def split_post(text: str, limit: int = MAX_POST_LENGTH) -> List[str]:
    """``text`` as pieces of at most ``limit`` characters.

    Each cut is at the last paragraph break, else line break, else space in
    the second half of the piece, and that separator is dropped; a piece
    with none is cut at ``limit``. Nothing else is changed or lost.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    parts: List[str] = []
    while len(text) > limit:
        cut, skip = limit, 0
        for separator in ("\n\n", "\n", " "):
            found = text.rfind(separator, limit // 2, limit + len(separator))
            if found > 0:
                cut, skip = found, len(separator)
                break
        parts.append(text[:cut])
        text = text[cut + skip:]
    if text or not parts:
        parts.append(text)
    return parts


def normalise_url(url: str) -> str:
    """The server's base URL: ``https://host[:port][/path]``, no trailing slash.

    A bare host (``chat.example.com``) means HTTPS. ``http://`` is accepted
    for a local test server. Anything else, credentials in the URL, a query
    or a fragment raise ``ValueError``.
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("the Mattermost URL is empty")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("https", "http"):
        raise ValueError("the Mattermost URL must start with https://")
    if not parts.hostname:
        raise ValueError("the Mattermost URL has no host")
    if parts.username or parts.password:
        raise ValueError("the Mattermost URL must not carry credentials")
    if parts.query or parts.fragment:
        raise ValueError("the Mattermost URL must not have a query or a fragment")
    path = parts.path.rstrip("/")
    if path.endswith(API_PATH):
        path = path[: -len(API_PATH)]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def websocket_url_for(base_url: str) -> str:
    """The websocket endpoint of the server at ``base_url`` (``wss://`` for HTTPS)."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, f"{parts.path}{API_PATH}/websocket", "", ""))


def websocket_host(websocket_url: str) -> str:
    """The ``Host`` header a websocket handshake to ``websocket_url`` carries.

    The websockets library follows HTTP redirects during the handshake, to
    other hosts too. The token goes in the first frame after the handshake,
    not in a header the library would strip on a cross-origin redirect, so
    the bridge compares the handshake's final ``Host`` with this before it
    sends the token (:meth:`HiveMindMattermostBridge._listen`).
    """
    from websockets.headers import build_host
    from websockets.uri import parse_uri

    uri = parse_uri(websocket_url)
    return build_host(uri.host, uri.port, uri.secure)


def ca_bundle() -> str:
    """The CA file both the REST calls and the websocket verify the server with.

    ``REQUESTS_CA_BUNDLE`` or ``SSL_CERT_FILE`` when set (a private CA),
    otherwise certifi's bundle, which is what ``requests`` uses by default.
    One source for both, so the two connections never disagree.
    """
    for variable in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        value = os.environ.get(variable)
        if value:
            return value
    import certifi

    return certifi.where()


def mention_pattern(username: str) -> "re.Pattern[str]":
    """Matches ``@username`` as Mattermost reads a mention.

    Not inside a longer name (``@usernamefan``, ``@username.team``) nor in an
    address (``me@username.example``). Trailing ``.``, ``-`` or ``_`` before
    a space or the end are punctuation, as in "thanks @username.", the way
    Mattermost itself parses them.
    """
    chars = _USERNAME_CHARS
    return re.compile(rf"(?<![\w@.-])@{re.escape(username)}(?=[._-]*(?![{chars}]))",
                      re.IGNORECASE)


#: An ``@`` that Mattermost's mention parser could read as the start of a
#: mention (``@channel``, ``@all``, ``@here``, a user, a group): one not
#: preceded by a letter or a digit (``me@example.com`` is an address, but
#: ``x.@channel`` and ``_@all`` are mentions to the server) and followed by
#: a character a name can hold.
_MENTION_START = re.compile(rf"(?<![^\W_])@(?=[{_USERNAME_CHARS}])", re.IGNORECASE)
#: U+2060 WORD JOINER: invisible, and neither a letter nor a digit, so the
#: server's parser ends the word there and ``@\u2060channel`` pings no one.
WORD_JOINER = "\u2060"


def defuse_reply(text: str) -> str:
    """The hub's reply made safe to post as the bot.

    The reply is the hub's text, and the hub writes what it was asked to
    (an assistant told to "say @channel ..." will): every mention in it is
    broken with an invisible :data:`WORD_JOINER`, so the bot never notifies
    a whole channel, a group or a person on someone else's behalf. Markdown
    images (``![...](...)``) are escaped to plain text, because every
    client that shows the post would fetch the image URL by itself, which
    leaks who reads the channel and, from a hub that was talked into
    putting the conversation in the URL, what was said. The rest of the
    Markdown (lists, emphasis, code, links) is kept.
    """
    text = _MENTION_START.sub("@" + WORD_JOINER, text)
    return text.replace("![", "!\\[")


@dataclass(frozen=True)
class Post:
    """The parts of a ``posted`` event the bridge reads."""

    id: str
    channel_id: str
    channel_type: str
    user_id: str
    root_id: str
    message: str
    type: str
    automated: bool
    sender_username: Optional[str]


def parse_posted(event: Dict[str, Any]) -> Optional[Post]:
    """The post carried by a websocket ``posted`` event, or ``None`` if it has none.

    Mattermost sends the post as a JSON string inside the event. A system
    post (someone joined, the header changed) has a non-empty ``type``; a
    post by a bot, a webhook or a plugin carries ``from_bot``,
    ``from_webhook`` or ``from_plugin`` in its props.
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    raw = data.get("post")
    if isinstance(raw, str):
        try:
            post = json.loads(raw)
        except ValueError:
            return None
    else:
        post = raw
    if not isinstance(post, dict):
        return None
    props = post.get("props") if isinstance(post.get("props"), dict) else {}
    automated = any(str(props.get(name, "")).lower() == "true" for name in _AUTOMATED_PROPS)
    sender = data.get("sender_name")
    username = None
    if isinstance(sender, str) and sender.startswith("@") and USERNAME_PATTERN.fullmatch(sender[1:]):
        username = sender[1:]
    return Post(
        id=str(post.get("id") or ""),
        channel_id=str(post.get("channel_id") or ""),
        channel_type=str(data.get("channel_type") or ""),
        user_id=str(post.get("user_id") or ""),
        root_id=str(post.get("root_id") or ""),
        message=post.get("message") if isinstance(post.get("message"), str) else "",
        type=str(post.get("type") or ""),
        automated=automated,
        sender_username=username,
    )


class MattermostAPI:
    """The REST calls the bridge makes, authenticated with the bot's access token.

    Safe to call from several threads: the bridge reads from the asyncio
    loop's worker threads and replies from the HiveMind bus thread, so one
    lock serialises use of the shared ``requests.Session``.
    """

    def __init__(self, url: str, token: str, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 websocket_url: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        """
        Parameters
        ----------
        url: the server's base URL (see :func:`normalise_url`).
        token: the bot account's access token.
        timeout: seconds each REST call may take.
        websocket_url: the websocket endpoint; derived from ``url`` when
            not given (tests serve it from another port).
        session: a pre-built ``requests.Session`` (tests).
        """
        if not token:
            raise ValueError("a Mattermost access token is required")
        self.url = normalise_url(url)
        self.websocket_url = websocket_url or websocket_url_for(self.url)
        self.timeout = timeout
        self._token = token
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._lock = threading.Lock()

    @property
    def token(self) -> str:
        """The access token, for the websocket's authentication challenge."""
        return self._token

    def ssl_context(self) -> ssl.SSLContext:
        """A client TLS context that verifies the server's certificate and name."""
        return ssl.create_default_context(cafile=ca_bundle())

    def _call(self, method: str, endpoint: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            response = self._session.request(method, f"{self.url}{API_PATH}{path}",
                                             timeout=self.timeout, verify=ca_bundle(),
                                             allow_redirects=False, **kwargs)
        if response.status_code == 401:
            raise TokenRejected()
        if response.status_code == 429:
            raise RateLimited(method, endpoint, retry_after(response.headers))
        if response.status_code >= 300:
            raise MattermostError(method, endpoint, response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise MattermostError(method, endpoint, response.status_code) from None
        if not isinstance(body, dict):
            raise MattermostError(method, endpoint, response.status_code)
        return body

    def me(self) -> Dict[str, Any]:
        """The bot's own user (``id``, ``username``); ``TokenRejected`` on a bad token."""
        return self._call("GET", "/users/me", "/users/me")

    def get_post(self, post_id: str) -> Dict[str, Any]:
        """One post by id."""
        return self._call("GET", "/posts/{id}", f"/posts/{quote(post_id, safe='')}")

    def create_post(self, channel_id: str, message: str, root_id: str = "") -> Dict[str, Any]:
        """Post ``message`` in ``channel_id``, in the thread ``root_id`` when given."""
        body: Dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        return self._call("POST", "/posts", "/posts", json=body)


__all__ = [
    "API_PATH", "DIRECT", "DIRECT_CHANNEL_TYPES", "GROUP_DIRECT", "ID_PATTERN",
    "MAX_POST_LENGTH", "MattermostAPI", "MattermostError", "Post", "RateLimited",
    "TokenRejected", "WORD_JOINER", "ca_bundle", "defuse_reply", "retry_after",
    "split_post",
    "mention_pattern", "normalise_url", "parse_posted", "websocket_host",
    "websocket_url_for",
]
