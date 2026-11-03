"""The running bridge against a local Mattermost server, over real sockets.

The websocket login, posts arriving as events, replies going out over REST,
readiness, reconnecting, and a token the server refuses at start or later.
"""
import pytest
from ovos_bus_client.message import Message

from fake_mattermost import BOT_ID, TOWN_SQUARE, make_hub, posted_event, wait_for


def test_a_post_on_the_websocket_reaches_the_hub_and_the_reply_comes_back(fake, run_bridge):
    hub = make_hub()
    running = run_bridge(hub=hub)
    fake.push(posted_event("@hivebot what time is it"))
    assert wait_for(lambda: hub.emit.called)
    context = hub.emit.call_args[0][0].payload.context
    assert hub.emit.call_args[0][0].payload.data["utterances"] == ["what time is it"]

    running.bridge.handle_speak(Message("speak", {"utterance": "It is three."}, context))
    assert fake.created == [{"channel_id": TOWN_SQUARE, "message": "@alice It is three."}]


def test_a_reply_in_a_thread_the_bot_started_is_answered_in_that_thread(fake, run_bridge):
    hub = make_hub()
    running = run_bridge(hub=hub)
    root = fake.add_post(BOT_ID)
    fake.push(posted_event("and tomorrow?", root_id=root))
    assert wait_for(lambda: hub.emit.called)
    context = hub.emit.call_args[0][0].payload.context
    running.bridge.handle_speak(Message("speak", {"utterance": "Rain."}, context))
    assert fake.created == [{"channel_id": TOWN_SQUARE, "message": "@alice Rain.", "root_id": root}]


def test_posts_before_the_login_is_confirmed_are_not_read(fake, run_bridge):
    fake.say_hello = False
    hub = make_hub()
    running = run_bridge(hub=hub, wait_ready=False)
    assert wait_for(lambda: fake.connected == 1)
    fake.push(posted_event("@hivebot hi"))
    assert not wait_for(lambda: hub.emit.called, timeout=0.5)
    assert not running.bridge.ready()


def test_ready_needs_both_the_hub_session_and_the_websocket(fake, run_bridge):
    hub = make_hub()
    running = run_bridge(hub=hub)
    assert running.bridge.ready()
    hub.handshake_event.clear()  # the hub connection dropped
    assert not running.bridge.ready()
    hub.handshake_event.set()
    assert running.bridge.ready()
    fake.drop()  # the Mattermost websocket dropped
    assert wait_for(lambda: not running.bridge.ready(), timeout=5)


def test_a_dropped_websocket_is_reconnected(fake, run_bridge):
    hub = make_hub()
    running = run_bridge(hub=hub)
    fake.drop()
    assert wait_for(lambda: fake.auth_attempts >= 2)
    assert wait_for(running.bridge.ready)
    fake.push(posted_event("@hivebot still there?"))
    assert wait_for(lambda: hub.emit.called)


def test_a_server_that_never_confirms_the_login_is_retried(fake, run_bridge, monkeypatch):
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "HELLO_TIMEOUT", 0.3)
    fake.say_hello = False
    running = run_bridge(wait_ready=False)
    assert wait_for(lambda: fake.auth_attempts >= 2, timeout=5)
    assert not running.bridge.ready()
    assert running.error is None


def test_a_refused_token_at_start_stops_the_bridge(fake, run_bridge):
    from mattermost_bridge.mattermost import TokenRejected

    hub = make_hub()
    running = run_bridge(hub=hub, api=fake.api(token="wrong"), wait_ready=False)
    running.thread.join(10)
    assert isinstance(running.error, TokenRejected)
    hub.close.assert_called()
    assert fake.auth_attempts == 0, "the websocket was tried with a token the server refuses"


def test_a_token_revoked_while_running_stops_the_bridge(fake, run_bridge):
    from mattermost_bridge.mattermost import TokenRejected

    running = run_bridge()
    fake.accept_token = False
    fake.drop()
    running.thread.join(10)
    assert isinstance(running.error, TokenRejected)
    assert not running.bridge.ready()


def test_a_token_revoked_silently_is_found_by_the_periodic_check(fake, run_bridge, monkeypatch):
    """What a real server does: the websocket stays open and simply goes quiet."""
    import mattermost_bridge
    from mattermost_bridge.mattermost import TokenRejected

    monkeypatch.setattr(mattermost_bridge, "TOKEN_CHECK_INTERVAL", 0.2)
    running = run_bridge()
    fake.accept_token = False  # no drop: the socket stays up
    running.thread.join(10)
    assert isinstance(running.error, TokenRejected)
    assert fake.auth_attempts == 1, "found by the check, not by a reconnect"


def test_the_token_check_is_quiet_while_the_token_works(fake, run_bridge, monkeypatch):
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "TOKEN_CHECK_INTERVAL", 0.05)
    running = run_bridge()
    assert wait_for(lambda: fake.calls.count("GET /api/v4/users/me") >= 4)
    assert running.bridge.ready() and running.error is None


def test_a_reply_refused_for_the_token_stops_the_bridge(fake, run_bridge):
    from mattermost_bridge.mattermost import TokenRejected

    running = run_bridge()
    fake.accept_token = False
    running.bridge.handle_speak(Message("speak", {"utterance": "hi"}, {"channel": TOWN_SQUARE}))
    running.thread.join(10)
    assert isinstance(running.error, TokenRejected)


def test_stop_ends_start(fake, run_bridge):
    running = run_bridge()
    running.bridge.stop()
    running.thread.join(10)
    assert not running.thread.is_alive()
    assert running.error is None
    assert wait_for(lambda: fake.connected == 0)


def test_an_unreachable_server_is_retried_not_fatal(run_bridge, fake):
    from mattermost_bridge.mattermost import MattermostAPI

    api = MattermostAPI("http://127.0.0.1:9", "tok", websocket_url="ws://127.0.0.1:9/api/v4/websocket",
                        timeout=1)
    running = run_bridge(api=api, wait_ready=False)
    assert not wait_for(lambda: not running.thread.is_alive(), timeout=1)
    assert not running.bridge.ready()


@pytest.mark.parametrize("path, ready_status", [("/readyz", 200), ("/healthz", 200)])
def test_the_probes_follow_the_bridge(fake, run_bridge, path, ready_status):
    from test_health import get

    hub = make_hub()
    running = run_bridge(hub=hub, health_port=0)
    port = running.bridge._health.port
    assert get(port, path) == ready_status
    hub.handshake_event.clear()
    assert get(port, "/readyz") == 503
    assert get(port, "/healthz") == 200


def test_an_interrupted_bridge_closes_its_hub_session():
    """Ctrl-C or SIGTERM (see __main__.stop_on_sigterm) closes the hub connection on the way out."""
    from unittest.mock import MagicMock

    from mattermost_bridge import HiveMindMattermostBridge

    hub = make_hub()
    bridge = HiveMindMattermostBridge(client=hub, api=MagicMock())

    async def interrupted():
        raise KeyboardInterrupt

    bridge._run_mattermost = interrupted
    with pytest.raises(KeyboardInterrupt):
        bridge.start()
    hub.close.assert_called_once()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_websocket_redirected_to_another_host_never_gets_the_token(fake, run_bridge, status):
    """The websockets library follows a redirect to any host; the token is sent in
    the first frame, so it would go to whoever answers there."""
    from fake_mattermost import Redirector, TOKEN

    from mattermost_bridge.mattermost import MattermostAPI

    redirector = Redirector(status).start()
    try:
        api = MattermostAPI(fake.url, fake.token, websocket_url=redirector.websocket_url, timeout=5)
        running = run_bridge(api=api, wait_ready=False)
        assert wait_for(lambda: redirector.handshakes >= 1, timeout=5), \
            "the redirect was not followed at all; the test proves nothing"
        assert not wait_for(lambda: [f for f in redirector.received if TOKEN in f], timeout=1)
        assert wait_for(lambda: redirector.handshakes >= 2, timeout=5), "not retried"
        assert not [frame for frame in redirector.received if TOKEN in frame]
        assert not running.bridge.ready()
        assert running.error is None
    finally:
        redirector.stop()
