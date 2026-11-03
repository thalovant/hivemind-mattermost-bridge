"""End to end: a real HiveMind hub, a local Mattermost server, the real bridge.

What this exercises
-------------------
This boots a **real hivemind-core hub** (hivescope's loopback topology, an
actual localhost WebSocket server) and runs the **real**
:class:`mattermost_bridge.HiveMindMattermostBridge` through ``start()``, over a
real :class:`~hivemind_bus_client.HiveMessageBusClient`. The Mattermost side
is ``tests/fake_mattermost.py``: a local server answering the REST calls and
the websocket the way Mattermost does, over real sockets. Nothing in the
bridge, the bus client or the hub is mocked.

The proven round trip::

    a 'posted' event on the Mattermost websocket
      -> the bridge's websocket listener -> handle_posted (mention gating)
      -> HiveMessageBusClient.emit(BUS recognizer_loop:utterance)   [real WS]
      -> hivemind-core admits + injects on the agent bus
      -> agent responder emits 'speak' routed back to this satellite
      -> bridge.handle_speak -> POST /api/v4/posts on the Mattermost server

Not covered: a real Mattermost server. ``docs/operator-setup.md`` describes
running one (``mattermost/mattermost-preview``) to try the bridge by hand.
"""
import threading
import time

import pytest
from ovos_bus_client.message import Message

from fake_mattermost import BOT_ID, TOWN_SQUARE, FakeMattermost, posted_event, wait_for
from hivemind_bus_client.client import HiveMessageBusClient
from hivemind_bus_client.identity import NodeIdentity
from hivescope.topology import TopologyBuilder

KEY = "mm-bridge-key"
# poorman-handshake >=2.0.0a1 gates the symmetric handshake on password
# entropy (>=40 bits); this must stay comfortably above that floor.
PASSWORD = "correct-horse-battery-staple-9f3a1c7e2b8d4055-mm"
ANSWER = "the weather is sunny"


def _hivemind_client(url: str, key: str, password: str) -> HiveMessageBusClient:
    """A real HiveMessageBusClient pointed at the loopback hub, its identity in memory."""
    host, port = url.replace("ws://", "").replace("wss://", "").rstrip("/").split(":")
    identity = NodeIdentity()
    identity.access_key = key
    identity.password = password
    identity.default_master = f"ws://{host}"
    identity.default_port = int(port)
    identity.site_id = "mattermost"
    from mattermost_bridge import platform

    return HiveMessageBusClient(key=key, password=password, host=f"ws://{host}", port=int(port),
                                useragent=platform, self_signed=False, identity=identity)


def _install_agent_responder(master, answer: str) -> None:
    """Answer every utterance with ``speak``, routed back to the asking satellite.

    hivemind-core stamps the injected utterance with ``context['source']`` =
    ``context['peer']`` = the satellite; the reply must name it as
    ``destination``. The bridge's own context (``channel``, ``root_id``,
    ``user``) rides along untouched, as it does through a real OVOS core.
    """
    bus = master.agent_protocol.bus

    def respond(msg: Message) -> None:
        context = dict(msg.context)
        context["destination"] = context.get("source") or context.get("peer")
        bus.emit(Message("speak", {"utterance": answer}, context))

    bus.on("recognizer_loop:utterance", respond)


@pytest.fixture
def hub():
    builder = TopologyBuilder()
    master = builder.add_master("M0", use_loopback=True)
    # hivemind-core is whitelist-only: grant exactly the type the bridge sends.
    master.register_satellite(KEY, password=PASSWORD, allowed_types=["recognizer_loop:utterance"])
    builder.start_all()
    _install_agent_responder(master, ANSWER)
    yield master
    builder.stop_all()


@pytest.fixture
def mattermost():
    server = FakeMattermost().start()
    yield server
    server.stop()


def _run(bridge):
    outcome = {}

    def run():
        try:
            bridge.start()
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_round_trip_mattermost_to_hivemind_and_back(hub, mattermost):
    from mattermost_bridge import HiveMindMattermostBridge
    from test_health import get

    bridge = HiveMindMattermostBridge(client=_hivemind_client(hub.network_protocol.url, KEY, PASSWORD),
                                      api=mattermost.api(), health_port=0)
    thread, outcome = _run(bridge)
    try:
        assert wait_for(bridge.ready, timeout=30), f"never ready: {outcome}"
        assert hub.connected_peers(), "the bridge did not register as a peer"
        assert get(bridge._health.port, "/readyz") == 200

        mattermost.push(posted_event("@hivebot what is the weather?"))
        assert wait_for(lambda: mattermost.created, timeout=30), \
            "no reply posted: the speak never round-tripped"
        assert mattermost.created[0] == {"channel_id": TOWN_SQUARE, "message": f"@alice {ANSWER}"}

        # A reply in the thread under the bot's answer needs no mention and is
        # answered in that thread.
        answer = next(post_id for post_id, post in mattermost.posts.items() if post["user_id"] == BOT_ID)
        mattermost.push(posted_event("and tomorrow?", root_id=answer))
        assert wait_for(lambda: len(mattermost.created) == 2, timeout=30)
        assert mattermost.created[1] == {"channel_id": TOWN_SQUARE, "message": f"@alice {ANSWER}",
                                         "root_id": answer}
    finally:
        bridge.stop()
        thread.join(10)
    assert "error" not in outcome


def test_rejected_hub_credentials_end_the_bridge_before_mattermost_is_touched(hub, mattermost):
    """Within the bounded handshake retries, never forever.

    A hub that refuses the identity outright ends ``connect()`` with
    ``ConnectionRefusedError``; this loopback hub does not answer a wrong
    password at all, so the bound (3 retries here, 10 by default) is what
    ends it, with ``RuntimeError``.
    """
    from mattermost_bridge import HiveMindMattermostBridge

    wrong = "a-wrong-password-of-enough-entropy-4c1d7e9a0b2f6385"
    bridge = HiveMindMattermostBridge(client=_hivemind_client(hub.network_protocol.url, KEY, wrong),
                                      api=mattermost.api(), handshake_max_retries=3)
    started = time.monotonic()
    thread, outcome = _run(bridge)
    thread.join(60)
    try:
        assert not thread.is_alive(), "the bridge kept trying a password the hub refuses"
        assert isinstance(outcome.get("error"), (ConnectionRefusedError, RuntimeError)), outcome
        assert time.monotonic() - started < 60
        assert mattermost.calls == [] and mattermost.auth_attempts == 0
    finally:
        bridge.stop()
