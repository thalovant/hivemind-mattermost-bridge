"""Outside a one-to-one DM the bot answers only what is addressed to it, and never sees its own name.

A question in a channel reads "@hivebot what time is it". The mention is how
the person addresses the bot, not part of the question, and an intent
matcher that receives it may match nothing. And a bot account receives every
post in every channel it belongs to; forwarding them all would ask the hub
about conversations that were never meant for it.
"""
import pytest

from fake_mattermost import ALICE, BOT_ID, bridge_with, forwarded, new_id, posted_event


def utterance(bridge, hub, text, **kwargs):
    payload = forwarded(bridge, hub, posted_event(text, **kwargs))
    return None if payload is None else payload.data["utterances"][0]


@pytest.fixture
def bot():
    return bridge_with()


def test_the_mention_is_removed_from_the_question(bot):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, "@hivebot what time is it") == "what time is it"


def test_the_mention_is_matched_whatever_its_case_and_position(bot):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, "what time is it @HiveBot?") == "what time is it ?"
    assert utterance(bridge, hub, "thanks @hivebot.") == "thanks ."
    assert utterance(bridge, hub, "(@hivebot) weather", channel_type="P") == "( ) weather"


def test_a_channel_post_not_addressed_to_the_bot_is_not_forwarded(bot):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, "lunch at noon?") is None
    assert utterance(bridge, hub, "lunch at noon?", channel_type="P") is None


@pytest.mark.parametrize("text", [
    "@hivebotfan hello", "@hivebot.team hello", "@hivebot_two hello", "@hivebot-2 hello",
    "mail me@hivebot.example", "@@hivebot hello",
])
def test_another_name_or_an_address_is_not_a_mention(bot, text):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, text) is None


def test_a_one_to_one_dm_needs_no_mention(bot):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, "what time is it", channel_type="D") == "what time is it"
    assert utterance(bridge, hub, "@hivebot what time is it", channel_type="D") == "what time is it"


def test_a_group_dm_is_a_conversation_between_people_and_needs_the_mention(bot):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, "lunch?", channel_type="G") is None
    assert utterance(bridge, hub, "@hivebot lunch?", channel_type="G") == "lunch?"


def test_a_message_that_is_only_the_mention_is_not_forwarded(bot):
    bridge, hub, _ = bot
    assert utterance(bridge, hub, "@hivebot") is None
    assert utterance(bridge, hub, "  @hivebot  ", channel_type="D") is None


def test_a_reply_in_a_thread_the_bot_started_is_addressed_to_it(bot):
    bridge, hub, api = bot
    root = new_id()
    api.get_post.return_value = {"id": root, "user_id": BOT_ID}
    assert utterance(bridge, hub, "and tomorrow?", root_id=root) == "and tomorrow?"
    api.get_post.assert_called_once_with(root)
    # the author of a thread does not change: asked once
    assert utterance(bridge, hub, "and the day after?", root_id=root) == "and the day after?"
    api.get_post.assert_called_once()


def test_a_reply_in_someone_elses_thread_needs_the_mention(bot):
    bridge, hub, api = bot
    root = new_id()
    api.get_post.return_value = {"id": root, "user_id": ALICE}
    assert utterance(bridge, hub, "and tomorrow?", root_id=root) is None
    assert utterance(bridge, hub, "@hivebot and tomorrow?", root_id=root) == "and tomorrow?"


def test_a_thread_under_the_bots_own_reply_needs_no_lookup(bot):
    """The bot's top-level answer becomes a thread when someone replies to it."""
    from ovos_bus_client.message import Message

    bridge, hub, api = bot
    answer = new_id()
    api.create_post.side_effect = None
    api.create_post.return_value = {"id": answer, "user_id": BOT_ID}
    context = forwarded(bridge, hub, posted_event("@hivebot weather?")).context
    bridge.handle_speak(Message("speak", {"utterance": "Sunny."}, context))
    assert utterance(bridge, hub, "and tomorrow?", root_id=answer) == "and tomorrow?"
    api.get_post.assert_not_called()


def test_a_thread_whose_author_cannot_be_read_is_not_addressed(bot):
    bridge, hub, api = bot
    api.get_post.side_effect = ConnectionError("down")
    assert utterance(bridge, hub, "and tomorrow?", root_id=new_id()) is None


def test_gating_can_be_turned_off():
    bridge, hub, _ = bridge_with(require_mention=False)
    assert utterance(bridge, hub, "lunch at noon?") == "lunch at noon?"
    assert utterance(bridge, hub, "@hivebot hi") == "hi"
