"""Replies: off the hub's thread, paced, bounded, retried on 429, split when too long."""
import threading
import time

import pytest
from ovos_bus_client.message import Message

from fake_mattermost import TOWN_SQUARE, bridge_with, wait_for


def reply(bridge, text, channel=TOWN_SQUARE):
    bridge.handle_speak(Message("speak", {"utterance": text},
                                {"channel": channel, "channel_type": "O"}))


@pytest.fixture
def fast(monkeypatch):
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "REPLY_INTERVAL", 0)


def test_the_hubs_thread_never_waits_for_mattermost(fast):
    bridge, _, api = bridge_with()
    release = threading.Event()
    api.create_post.side_effect = lambda *a, **k: (release.wait(10), {"id": "x"})[1]
    began = time.monotonic()
    for _ in range(3):
        reply(bridge, "hi")
    assert time.monotonic() - began < 0.5, "handle_speak waited on the REST call"
    release.set()
    assert bridge.flush_replies(5)
    assert api.create_post.call_count == 3


def test_replies_are_posted_in_order_and_paced(monkeypatch):
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "REPLY_INTERVAL", 0.2)
    bridge, _, api = bridge_with()
    sent = []
    api.create_post.side_effect = lambda channel, message, root_id="": (
        sent.append((time.monotonic(), message)), {"id": "x"})[1]
    for number in range(3):
        reply(bridge, f"part {number}")
    assert bridge.flush_replies(5)
    assert [message for _, message in sent] == ["part 0", "part 1", "part 2"]
    assert sent[2][0] - sent[0][0] >= 0.38


def test_the_queue_is_bounded(fast, monkeypatch):
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "REPLY_QUEUE_SIZE", 5)
    bridge, _, api = bridge_with()
    release = threading.Event()
    api.create_post.side_effect = lambda *a, **k: (release.wait(10), {"id": "x"})[1]
    for _ in range(20):
        reply(bridge, "hi")
    release.set()
    assert bridge.flush_replies(5)
    assert api.create_post.call_count == 5


def test_a_rate_limited_post_is_retried_after_the_wait_the_server_asks(fake, fast):
    bridge, _, _ = bridge_with()
    bridge.api = fake.api()
    fake.rate_limit_posts = 2
    fake.retry_after = "0.2"
    began = time.monotonic()
    reply(bridge, "hi")
    assert bridge.flush_replies(5)
    assert fake.created == [{"channel_id": TOWN_SQUARE, "message": "hi"}]
    assert time.monotonic() - began >= 0.38
    assert fake.calls.count("POST /api/v4/posts") == 3


def test_a_post_still_rate_limited_is_given_up_without_raising(fake, fast):
    import mattermost_bridge

    bridge, _, _ = bridge_with()
    bridge.api = fake.api()
    fake.rate_limit_posts = 99
    fake.retry_after = "0.01"
    reply(bridge, "hi")
    assert bridge.flush_replies(5)
    assert fake.created == []
    assert fake.calls.count("POST /api/v4/posts") == mattermost_bridge.RATE_LIMIT_ATTEMPTS
    fake.rate_limit_posts = 0
    reply(bridge, "next")  # the sender is still alive
    assert bridge.flush_replies(5)
    assert fake.created == [{"channel_id": TOWN_SQUARE, "message": "next"}]


@pytest.mark.parametrize("headers, seconds", [
    ({"Retry-After": "3"}, 3.0),
    ({"X-Ratelimit-Reset": "7"}, 7.0),
    ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT", "X-Ratelimit-Reset": "2"}, 2.0),
    ({}, 1.0),
    ({"Retry-After": "-5"}, 1.0),
])
def test_the_wait_a_429_asks_for(headers, seconds):
    from mattermost_bridge.mattermost import retry_after

    assert retry_after(headers) == seconds


def test_a_long_rate_limit_wait_is_capped(fast, monkeypatch):
    import mattermost_bridge
    from mattermost_bridge.mattermost import RateLimited

    monkeypatch.setattr(mattermost_bridge, "RATE_LIMIT_MAX_WAIT", 0.05)
    bridge, _, api = bridge_with()
    answers = [RateLimited("POST", "/posts", 3600), {"id": "x"}]
    api.create_post.side_effect = lambda *a, **k: (
        (_ for _ in ()).throw(answers.pop(0)) if isinstance(answers[0], Exception) else answers.pop(0))
    reply(bridge, "hi")
    assert bridge.flush_replies(2), "waited the hour the server asked for"
    assert api.create_post.call_count == 2


def test_a_reply_longer_than_a_post_is_split_and_nothing_is_lost(fast):
    from mattermost_bridge.mattermost import MAX_POST_LENGTH

    bridge, _, api = bridge_with()
    words = " ".join(f"w{number}" for number in range(9000))  # ~ 50,000 characters
    bridge.handle_speak(Message("speak", {"utterance": words},
                                {"channel": TOWN_SQUARE, "channel_type": "O",
                                 "user": {"mattermost_username": "alice"}}))
    assert bridge.flush_replies(5)
    posted = [call.args[1] for call in api.create_post.call_args_list]
    assert len(posted) == 4
    assert all(len(message) <= MAX_POST_LENGTH for message in posted)
    assert posted[0].startswith("@alice ")
    assert " ".join(posted)[len("@alice "):] == words


def test_a_reply_past_the_post_budget_is_cut_visibly(fast):
    import mattermost_bridge
    from mattermost_bridge.mattermost import MAX_POST_LENGTH

    bridge, _, api = bridge_with()
    reply(bridge, "x" * (MAX_POST_LENGTH * 6))
    assert bridge.flush_replies(5)
    posted = [call.args[1] for call in api.create_post.call_args_list]
    assert len(posted) == mattermost_bridge.MAX_REPLY_POSTS
    assert posted[-1].endswith(mattermost_bridge.CUT_MARK)
    assert all(len(message) <= MAX_POST_LENGTH for message in posted)


def test_a_failing_post_does_not_end_the_sender(fast):
    bridge, _, api = bridge_with()
    api.create_post.side_effect = [RuntimeError("boom"), {"id": "x"}]
    reply(bridge, "one")
    reply(bridge, "two")
    assert bridge.flush_replies(5)
    assert api.create_post.call_count == 2


def test_stop_lets_queued_replies_go_out_first(fake, run_bridge, fast):
    running = run_bridge()
    for number in range(3):
        reply(running.bridge, f"bye {number}")
    running.bridge.stop()
    assert [post["message"] for post in fake.created] == ["bye 0", "bye 1", "bye 2"]


def test_stop_does_not_wait_out_a_rate_limit(fake, run_bridge, fast, monkeypatch):
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "REPLY_DRAIN_TIMEOUT", 0.3)
    running = run_bridge()
    fake.rate_limit_posts = 99
    fake.retry_after = "30"
    reply(running.bridge, "hi")
    assert wait_for(lambda: "POST /api/v4/posts" in fake.calls)
    began = time.monotonic()
    running.bridge.stop()
    assert time.monotonic() - began < 3


@pytest.mark.parametrize("text, limit, parts", [
    ("", 5, [""]),
    ("abc", 5, ["abc"]),
    ("aa bb cc dd", 5, ["aa bb", "cc dd"]),
    ("aaaaaaaaaa", 4, ["aaaa", "aaaa", "aa"]),
    ("one two\n\nthree four", 12, ["one two", "three four"]),
    ("line one\nline two", 12, ["line one", "line two"]),
    ("é" * 7, 3, ["ééé", "ééé", "é"]),
])
def test_split_post(text, limit, parts):
    from mattermost_bridge.mattermost import split_post

    assert split_post(text, limit) == parts
