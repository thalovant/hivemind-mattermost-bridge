"""Who shares a conversation on the hub.

With one session per channel, Bob's "and tomorrow?" continues Alice's
question about the weather. ``session_scope="person"``, the default, gives
every person their own conversation in each channel; ``"chat"`` keeps the
channel as one.
"""
import pytest

from fake_mattermost import ALICE, BOB, OFF_TOPIC, TOWN_SQUARE, bridge_with, forwarded, posted_event


def session_of(bridge, hub, user_id, channel_id=TOWN_SQUARE):
    event = posted_event("@hivebot what is the weather", user_id=user_id, channel_id=channel_id)
    return forwarded(bridge, hub, event).context["session"]["session_id"]


def test_by_default_each_person_has_their_own_conversation():
    bridge, hub, _ = bridge_with()
    alice, bob = session_of(bridge, hub, ALICE), session_of(bridge, hub, BOB)
    assert alice == f"mattermost-{TOWN_SQUARE}-{ALICE}"
    assert bob == f"mattermost-{TOWN_SQUARE}-{BOB}"
    # the same person in another channel is another conversation
    assert session_of(bridge, hub, ALICE, channel_id=OFF_TOPIC) != alice


def test_a_channel_can_be_one_conversation():
    bridge, hub, _ = bridge_with(session_scope="chat")
    assert session_of(bridge, hub, ALICE) == session_of(bridge, hub, BOB) == f"mattermost-{TOWN_SQUARE}"


def test_an_unknown_scope_is_refused():
    with pytest.raises(ValueError):
        bridge_with(session_scope="room")
