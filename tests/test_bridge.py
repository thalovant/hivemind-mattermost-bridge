"""The bridge offline: construction, the hub connection, what is forwarded, where replies go.

A stand-in hub and REST API are injected, so nothing here opens a socket;
``tests/test_websocket.py`` runs the same bridge against a local server.
"""
from unittest.mock import MagicMock

import pytest
from ovos_bus_client.message import Message

from fake_mattermost import (
    ALICE, BOB, BOT_ID, DM_CHANNEL, GROUP_CHANNEL, OFF_TOPIC, TOWN_SQUARE,
    bridge_with, forwarded, make_hub, new_id, posted_event,
)


def test_import_package_and_version():
    import mattermost_bridge
    from mattermost_bridge.version import __version__

    assert isinstance(__version__, str) and __version__
    assert mattermost_bridge.platform.startswith("HiveMindMattermostBridge")
    assert mattermost_bridge.HiveMindMattermostBridge is mattermost_bridge.JarbasMattermostBridge


def test_construct_bridge_without_connecting():
    from mattermost_bridge import HiveMindMattermostBridge

    hub, api = make_hub(), MagicMock()
    bridge = HiveMindMattermostBridge(client=hub, api=api)
    assert bridge._started is False
    hub.connect.assert_not_called()
    api.me.assert_not_called()


def test_url_and_token_are_required_without_an_injected_api():
    from mattermost_bridge import HiveMindMattermostBridge

    with pytest.raises(ValueError):
        HiveMindMattermostBridge(client=make_hub())
    with pytest.raises(ValueError):
        HiveMindMattermostBridge(url="https://chat.example.com", client=make_hub())


def test_connect_hivemind_calls_connect_once_and_registers_handlers():
    """connect_hivemind() must call connect() exactly once, never run_forever()."""
    from mattermost_bridge import DEFAULT_HANDSHAKE_MAX_RETRIES, HiveMindMattermostBridge

    hub = make_hub()
    bridge = HiveMindMattermostBridge(client=hub, api=MagicMock())
    bridge.connect_hivemind()
    hub.connect.assert_called_once_with(site_id="mattermost",
                                        handshake_max_retries=DEFAULT_HANDSHAKE_MAX_RETRIES)
    hub.run_forever.assert_not_called()
    registered = {call.args[0] for call in hub.on_mycroft.call_args_list}
    assert registered == {"speak", "hive.complete_intent_failure"}


def test_handshake_retries_are_bounded_and_configurable():
    """A stalled hub must not hang the bridge: never the client's retry-forever default."""
    from mattermost_bridge import HiveMindMattermostBridge

    hub = make_hub()
    HiveMindMattermostBridge(client=hub, api=MagicMock(), handshake_max_retries=3,
                             site_id="bridge-0b6f").connect_hivemind()
    hub.connect.assert_called_once_with(site_id="bridge-0b6f", handshake_max_retries=3)


def test_the_hub_client_names_no_node_so_its_noise_key_is_where_the_platform_mounts_it(
        tmp_path, monkeypatch):
    """The managed pod mounts the Noise key at ~/.config/hivemind/unnamed-node_noise.key."""
    from mattermost_bridge import HiveMindMattermostBridge

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    bridge = HiveMindMattermostBridge(url="https://chat.example.com", token="tok",
                                      key="key", password="pw", host="ws://127.0.0.1")
    assert bridge.client.identity.noise_key == str(
        tmp_path / ".config" / "hivemind" / "unnamed-node_noise.key")


def test_identify_learns_the_bots_id_and_username():
    bridge, _, api = bridge_with()
    assert (bridge.bot_id, bridge.bot_username) == (BOT_ID, "hivebot")
    api.me.return_value = {"id": "", "username": ""}
    with pytest.raises(ValueError):
        bridge.identify()


def test_a_post_is_forwarded_with_what_the_reply_needs():
    from hivemind_bus_client import HiveMessage, HiveMessageType

    bridge, hub, _ = bridge_with()
    payload = forwarded(bridge, hub, posted_event("@hivebot turn on the lights"))
    sent = hub.emit.call_args[0][0]
    assert isinstance(sent, HiveMessage) and sent.msg_type == HiveMessageType.BUS
    assert payload.msg_type == "recognizer_loop:utterance"
    assert payload.data == {"utterances": ["turn on the lights"], "lang": "en-us"}
    assert payload.context["channel"] == TOWN_SQUARE
    assert payload.context["channel_type"] == "O"
    assert payload.context["root_id"] == ""
    assert payload.context["user"] == {"mattermost_username": "alice",
                                       "mattermost_user_id": ALICE}
    assert payload.context["session"]["session_id"] == f"mattermost-{TOWN_SQUARE}-{ALICE}"


def test_nothing_is_forwarded_while_the_hub_session_is_down():
    bridge, hub, _ = bridge_with()
    hub.handshake_event.clear()
    assert forwarded(bridge, hub, posted_event("@hivebot hello")) is None


def test_nothing_is_forwarded_before_connect_hivemind():
    from mattermost_bridge import HiveMindMattermostBridge

    hub, api = make_hub(), MagicMock()
    api.me.return_value = {"id": BOT_ID, "username": "hivebot"}
    bridge = HiveMindMattermostBridge(client=hub, api=api)
    bridge.identify()
    assert forwarded(bridge, hub, posted_event("@hivebot hello")) is None


@pytest.mark.parametrize("event", [
    posted_event("@hivebot hello", user_id=BOT_ID, username="hivebot"),
    posted_event("@hivebot hello", props={"from_bot": "true"}, user_id=BOB),
    posted_event("@hivebot hello", props={"from_webhook": "true"}),
    posted_event("@hivebot hello", props={"from_plugin": "true"}),
    posted_event("@hivebot joined the channel", post_type="system_join_channel"),
    posted_event(""),
    {"event": "posted", "data": {"post": "not json"}},
    {"event": "posted", "data": "nonsense"},
], ids=["own", "another-bot", "webhook", "plugin", "system", "empty", "bad-json", "no-data"])
def test_posts_not_written_by_a_person_are_never_forwarded(event):
    bridge, hub, _ = bridge_with(require_mention=False)
    assert forwarded(bridge, hub, event) is None


def test_only_allowed_channels_are_answered():
    bridge, hub, _ = bridge_with(allowed_channels=[TOWN_SQUARE])
    assert forwarded(bridge, hub, posted_event("@hivebot hi", channel_id=OFF_TOPIC)) is None
    assert forwarded(bridge, hub, posted_event("@hivebot hi", channel_id=TOWN_SQUARE)) is not None
    assert forwarded(bridge, hub, posted_event("@hivebot hi", channel_id=OFF_TOPIC,
                                               channel_type="P")) is None


def dm(text="what time is it", channel_type="D"):
    channel = DM_CHANNEL if channel_type == "D" else GROUP_CHANNEL
    return posted_event(text, channel_id=channel, channel_type=channel_type)


def test_a_dm_cannot_step_around_the_channel_list():
    """auto, the default, refuses direct and group-direct messages when channels are listed."""
    bridge, hub, _ = bridge_with(allowed_channels=[TOWN_SQUARE])
    assert bridge.dm_policy == "deny"
    assert forwarded(bridge, hub, dm()) is None
    assert forwarded(bridge, hub, dm("@hivebot hi", channel_type="G")) is None


def test_dms_are_accepted_by_default_without_a_channel_list():
    bridge, hub, _ = bridge_with()
    assert bridge.dm_policy == "allow"
    assert forwarded(bridge, hub, dm()).data["utterances"] == ["what time is it"]


def test_dms_can_be_allowed_beside_a_channel_list():
    bridge, hub, _ = bridge_with(allowed_channels=[TOWN_SQUARE], dm_policy="allow")
    assert forwarded(bridge, hub, dm()) is not None
    assert forwarded(bridge, hub, dm("@hivebot hi", channel_type="G")) is not None
    assert forwarded(bridge, hub, posted_event("@hivebot hi", channel_id=OFF_TOPIC)) is None


def test_dms_can_be_refused_without_a_channel_list():
    bridge, hub, _ = bridge_with(dm_policy="deny")
    assert forwarded(bridge, hub, dm()) is None
    assert forwarded(bridge, hub, dm("@hivebot hi", channel_type="G")) is None
    assert forwarded(bridge, hub, posted_event("@hivebot hi")) is not None


def test_an_unknown_dm_policy_is_refused():
    from mattermost_bridge import HiveMindMattermostBridge

    with pytest.raises(ValueError):
        HiveMindMattermostBridge(client=make_hub(), api=MagicMock(), dm_policy="sometimes")


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------
def speak(bridge, utterance, **context):
    """``speak`` from the hub, then wait for the sender thread to have posted it."""
    bridge.handle_speak(Message("speak", {"utterance": utterance}, context))
    assert bridge.flush_replies(5), "the reply was never sent"


def test_a_reply_goes_to_the_questions_channel_naming_the_asker():
    bridge, hub, api = bridge_with()
    context = forwarded(bridge, hub, posted_event("@hivebot what time is it")).context
    speak(bridge, "It is three.", **context)
    api.create_post.assert_called_once_with(TOWN_SQUARE, "@alice It is three.", root_id="")


def test_a_reply_to_a_question_in_a_thread_stays_in_the_thread():
    bridge, hub, api = bridge_with()
    root = new_id()
    context = forwarded(bridge, hub, posted_event("@hivebot and tomorrow?", root_id=root)).context
    assert context["root_id"] == root
    speak(bridge, "Rain.", **context)
    api.create_post.assert_called_once_with(TOWN_SQUARE, "@alice Rain.", root_id=root)


def test_a_reply_in_a_dm_is_the_hubs_text_verbatim():
    bridge, hub, api = bridge_with()
    context = forwarded(bridge, hub, dm()).context
    speak(bridge, "It is three.", **context)
    api.create_post.assert_called_once_with(DM_CHANNEL, "It is three.", root_id="")


def test_a_reply_without_a_channel_is_dropped():
    bridge, _, api = bridge_with()
    speak(bridge, "hi there", user={"mattermost_username": "alice"})
    bridge.handle_intent_failure(Message("hive.complete_intent_failure", {}, {}))
    assert bridge.flush_replies(5)
    speak(bridge, "", channel=TOWN_SQUARE)
    api.create_post.assert_not_called()


def test_an_intent_failure_gets_the_fallback_line():
    bridge, _, api = bridge_with()
    bridge.handle_intent_failure(Message("hive.complete_intent_failure", {},
                                         {"channel": DM_CHANNEL, "channel_type": "D"}))
    assert bridge.flush_replies(5)
    channel, text = api.create_post.call_args[0]
    assert channel == DM_CHANNEL and "don't know" in text


def test_a_failed_reply_does_not_raise():
    bridge, _, api = bridge_with()
    api.create_post.side_effect = ConnectionError("down")
    speak(bridge, "hi", channel=TOWN_SQUARE)  # must not raise on the bus thread


def test_a_refused_token_on_a_reply_stops_the_bridge():
    from mattermost_bridge.mattermost import TokenRejected

    bridge, _, api = bridge_with()
    api.create_post.side_effect = TokenRejected()
    speak(bridge, "hi", channel=TOWN_SQUARE)
    assert bridge._token_rejected.is_set()


# ---------------------------------------------------------------------------
# what a reply may say, and where it may go
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hub_text, posted", [
    ("@channel lunch is here", "@⁠channel lunch is here"),
    ("ping @all and @HERE", "ping @⁠all and @⁠HERE"),
    ("as @bob said", "as @⁠bob said"),
    ("x.@channel _@all (@here)", "x.@⁠channel _@⁠all (@⁠here)"),
    ("write to me@example.com", "write to me@example.com"),
    ("![chart](https://evil.example/p?q=secret)", "!\\[chart](https://evil.example/p?q=secret)"),
    ("**bold**, `code`, [a link](https://example.com)",
     "**bold**, `code`, [a link](https://example.com)"),
])
def test_a_reply_never_pings_anyone_but_the_asker_nor_loads_an_image(hub_text, posted):
    """The hub writes what it is asked to; "say @channel ..." must not notify a whole channel."""
    bridge, hub, api = bridge_with()
    context = forwarded(bridge, hub, posted_event("@hivebot say it")).context
    speak(bridge, hub_text, **context)
    api.create_post.assert_called_once_with(TOWN_SQUARE, f"@alice {posted}", root_id="")


def test_a_channel_wide_name_in_the_context_is_not_used_as_the_asker():
    bridge, _, api = bridge_with()
    speak(bridge, "hi", channel=TOWN_SQUARE, channel_type="O",
          user={"mattermost_username": "channel"})
    api.create_post.assert_called_once_with(TOWN_SQUARE, "hi", root_id="")


def test_a_reply_to_a_channel_off_the_list_is_not_posted():
    bridge, _, api = bridge_with(allowed_channels=[TOWN_SQUARE])
    speak(bridge, "hi", channel=OFF_TOPIC, channel_type="O")
    speak(bridge, "hi", channel=DM_CHANNEL, channel_type="D")
    speak(bridge, "hi", channel=OFF_TOPIC)  # no type: not a DM either
    api.create_post.assert_not_called()
    speak(bridge, "hi", channel=TOWN_SQUARE, channel_type="O")
    api.create_post.assert_called_once()


def test_a_reply_in_a_dm_follows_the_dm_policy():
    bridge, _, api = bridge_with(dm_policy="deny")
    speak(bridge, "hi", channel=DM_CHANNEL, channel_type="D")
    api.create_post.assert_not_called()


def test_a_listed_group_dm_is_answered():
    """A channel id on the list is answered whatever its type."""
    bridge, hub, api = bridge_with(allowed_channels=[TOWN_SQUARE, GROUP_CHANNEL])
    assert bridge.dm_policy == "deny"
    message = forwarded(bridge, hub, posted_event("@hivebot hi", channel_id=GROUP_CHANNEL,
                                                  channel_type="G"))
    assert message is not None
    speak(bridge, "hello", **message.context)
    api.create_post.assert_called_once_with(GROUP_CHANNEL, "@alice hello", root_id="")
    assert forwarded(bridge, hub, posted_event("@hivebot hi", channel_id=DM_CHANNEL,
                                               channel_type="D")) is None
