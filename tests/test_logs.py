"""Nothing anyone wrote or was told, and no token, ends up in a log, at any level.

A debug log is shipped and read far more widely than the chat it came from.
The bridge runs against the local server with every standard logger at
debug, so the websocket and HTTP libraries' own logs are checked too.
"""
import logging
from unittest.mock import MagicMock

from ovos_bus_client.message import Message

from fake_mattermost import TOKEN, make_hub, posted_event, wait_for

SECRET_QUESTION = "is my biopsy result back"
SECRET_ANSWER = "Your appointment is on Tuesday"


def test_neither_questions_nor_answers_nor_the_token_are_logged(fake, run_bridge, monkeypatch, caplog):
    import mattermost_bridge

    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = lambda *a, **k: lines.append(" ".join(map(str, a)))
    monkeypatch.setattr(mattermost_bridge, "LOG", log)
    caplog.set_level(logging.DEBUG)

    hub = make_hub()
    running = run_bridge(hub=hub)
    fake.push(posted_event(f"@hivebot {SECRET_QUESTION}"))
    fake.push(posted_event(f"{SECRET_QUESTION} (not for the bot)"))
    assert wait_for(lambda: hub.emit.called)
    context = hub.emit.call_args[0][0].payload.context
    running.bridge.handle_speak(Message("speak", {"utterance": SECRET_ANSWER}, context))
    assert wait_for(lambda: fake.created)
    fake.drop()  # a reconnect logs too
    assert wait_for(lambda: fake.auth_attempts >= 2)

    assert lines, "the bridge logged nothing; the test would prove nothing"
    assert caplog.records, "no library logged anything; the test would prove nothing"
    joined = "\n".join(lines) + "\n" + caplog.text
    assert SECRET_QUESTION not in joined
    assert SECRET_ANSWER not in joined
    assert TOKEN not in joined
    # The websocket library logs frames truncated to a few dozen characters,
    # enough to show a short token or a short question in full: none at all.
    assert "authentication_challenge" not in joined
    assert not [record for record in caplog.records
                if record.name.startswith("mattermost_bridge.websocket")
                and record.levelno < logging.WARNING]
