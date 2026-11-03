"""The CLI resolves its settings from flags, the environment and secret files.

A secret on the command line is readable by every process on the host
(``ps``, ``/proc/<pid>/cmdline``), so a container must be able to start the
bridge with no secret in its arguments at all.
"""
from pathlib import Path

import pytest

from mattermost_bridge.__main__ import resolve_options

CHANNEL_A = "c0c0c0c0c0c0c0c0c0c0c0c0c0"
CHANNEL_B = "c1c1c1c1c1c1c1c1c1c1c1c1c1"
BASE = {"MATTERMOST_URL": "https://chat.example.com", "MATTERMOST_TOKEN": "tok"}


def test_everything_can_come_from_the_environment():
    options = resolve_options([], {
        "MATTERMOST_URL": "https://chat.example.com/",
        "MATTERMOST_TOKEN": "tok",
        "MATTERMOST_ALLOWED_CHANNELS": f"{CHANNEL_A}, {CHANNEL_B}",
        "MATTERMOST_DM_POLICY": "allow",
        "MATTERMOST_REQUIRE_MENTION": "false",
        "MATTERMOST_SESSION_SCOPE": "chat",
        "BRIDGE_HEALTH_PORT": "8080",
        "HIVEMIND_ACCESS_KEY": "key",
        "HIVEMIND_PASSWORD": "pw",
        "HIVEMIND_HOST": "hub.local",
        "HIVEMIND_PORT": "6000",
        "HIVEMIND_SITE_ID": "bridge-0b6f",
        "HIVEMIND_SELF_SIGNED": "true",
        "HIVEMIND_LANG": "fr-fr",
    })
    assert options == {
        "url": "https://chat.example.com",
        "token": "tok",
        "key": "key",
        "password": "pw",
        "host": "ws://hub.local",
        "port": 6000,
        "self_signed": True,
        "lang": "fr-fr",
        "site_id": "bridge-0b6f",
        "allowed_channels": [CHANNEL_A, CHANNEL_B],
        "dm_policy": "allow",
        "require_mention": False,
        "session_scope": "chat",
        "health_port": 8080,
    }


def test_the_managed_pods_environment_resolves():
    """Exactly what the platform sets: secrets by file, never by value."""
    secrets = Path(__file__).parent  # any readable files will do for the shape
    options = resolve_options([], {
        "HOME": "/home/bridge",
        "HIVEMIND_HOST": "ws://hub-own.tenant.svc.cluster.local",
        "HIVEMIND_PORT": "5678",
        "HIVEMIND_SITE_ID": "bridge-0b6f",
        "HIVEMIND_ACCESS_KEY_FILE": str(secrets / "test_cli.py"),
        "HIVEMIND_PASSWORD_FILE": str(secrets / "test_cli.py"),
        "MATTERMOST_URL": "https://chat.example.com",
        "MATTERMOST_TOKEN_FILE": str(secrets / "test_cli.py"),
        "MATTERMOST_ALLOWED_CHANNELS": CHANNEL_A,
        "MATTERMOST_REQUIRE_MENTION": "true",
        "MATTERMOST_SESSION_SCOPE": "person",
        "BRIDGE_HEALTH_PORT": "8080",
    })
    assert options["allowed_channels"] == [CHANNEL_A]
    assert options["dm_policy"] == "auto"
    assert options["site_id"] == "bridge-0b6f"
    assert options["health_port"] == 8080


def test_secrets_can_come_from_files(tmp_path):
    for name, value in (("token", "tok-from-file\n"), ("key", "key-from-file"),
                        ("password", "pw-from-file")):
        (tmp_path / name).write_text(value, encoding="utf-8")
    options = resolve_options([], {
        "MATTERMOST_URL": "https://chat.example.com",
        "MATTERMOST_TOKEN_FILE": str(tmp_path / "token"),
        "HIVEMIND_ACCESS_KEY_FILE": str(tmp_path / "key"),
        "HIVEMIND_PASSWORD_FILE": str(tmp_path / "password"),
    })
    assert (options["token"], options["key"], options["password"]) == (
        "tok-from-file", "key-from-file", "pw-from-file")


def test_a_flag_wins_over_the_environment_and_the_environment_over_a_file(tmp_path):
    token = tmp_path / "token"
    token.write_text("from-file", encoding="utf-8")
    environ = {"MATTERMOST_URL": "https://chat.example.com", "MATTERMOST_TOKEN": "from-env",
               "MATTERMOST_TOKEN_FILE": str(token)}
    assert resolve_options(["--token", "from-flag"], environ)["token"] == "from-flag"
    assert resolve_options([], environ)["token"] == "from-env"
    del environ["MATTERMOST_TOKEN"]
    assert resolve_options([], environ)["token"] == "from-file"
    assert resolve_options(["--url", "https://other.example.com"], environ)["url"] == \
        "https://other.example.com"


def test_defaults_when_nothing_but_the_server_and_token_are_given():
    options = resolve_options([], dict(BASE))
    assert options["port"] == 5678
    assert options["site_id"] == "mattermost"
    assert options["lang"] == "en-us"
    assert options["self_signed"] is False
    assert options["allowed_channels"] is None
    assert options["dm_policy"] == "auto"
    assert options["require_mention"] is True
    assert options["session_scope"] == "person"
    assert options["health_port"] is None
    assert options["host"] is None


def test_a_missing_token_is_a_usage_error_naming_the_variable(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {"MATTERMOST_URL": "https://chat.example.com"})
    assert "MATTERMOST_TOKEN" in capsys.readouterr().err


def test_a_missing_server_is_a_usage_error_naming_the_variable(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {"MATTERMOST_TOKEN": "tok"})
    assert "MATTERMOST_URL" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["ftp://chat.example.com", "https://bot:pw@chat.example.com"])
def test_a_bad_server_url_is_a_usage_error(url, capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATTERMOST_URL": url})
    assert "MATTERMOST_URL" in capsys.readouterr().err


def test_an_unreadable_secret_file_names_the_variable(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        resolve_options([], {"MATTERMOST_URL": "https://chat.example.com",
                             "MATTERMOST_TOKEN_FILE": str(tmp_path / "absent")})
    assert "MATTERMOST_TOKEN_FILE" in str(stopped.value)


@pytest.mark.parametrize("listed", ["town-square", f"{CHANNEL_A},general", CHANNEL_A.upper(), " , "])
def test_a_malformed_or_empty_channel_list_is_a_usage_error(listed):
    """Channel names are not ids, and a list that names nothing must not mean every channel."""
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATTERMOST_ALLOWED_CHANNELS": listed})


def test_channels_can_be_listed_by_flag():
    options = resolve_options(["--allowed-channel", CHANNEL_A, "--allowed-channel", CHANNEL_B], dict(BASE))
    assert options["allowed_channels"] == [CHANNEL_A, CHANNEL_B]


def test_the_dm_policy_comes_from_a_flag_or_the_environment():
    assert resolve_options(["--dm-policy", "allow"], dict(BASE))["dm_policy"] == "allow"
    assert resolve_options([], {**BASE, "MATTERMOST_DM_POLICY": "deny"})["dm_policy"] == "deny"
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATTERMOST_DM_POLICY": "sometimes"})


def test_mention_gating_is_on_by_default_and_can_be_turned_off():
    assert resolve_options([], dict(BASE))["require_mention"] is True
    assert resolve_options(["--no-require-mention"], dict(BASE))["require_mention"] is False
    assert resolve_options([], {**BASE, "MATTERMOST_REQUIRE_MENTION": "false"})["require_mention"] is False
    assert resolve_options([], {**BASE, "MATTERMOST_REQUIRE_MENTION": "TRUE"})["require_mention"] is True


def test_a_misspelt_boolean_is_refused_not_read_as_false():
    """'ture' must not quietly turn mention gating off."""
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATTERMOST_REQUIRE_MENTION": "ture"})
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "HIVEMIND_SELF_SIGNED": "maybe"})


def test_the_session_scope_defaults_to_the_person_and_rejects_anything_else():
    assert resolve_options([], dict(BASE))["session_scope"] == "person"
    assert resolve_options(["--session-scope", "chat"], dict(BASE))["session_scope"] == "chat"
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "MATTERMOST_SESSION_SCOPE": "channel"})


@pytest.mark.parametrize("port", ["http", "0", "70000"])
def test_a_bad_health_port_is_a_usage_error(port):
    with pytest.raises(SystemExit):
        resolve_options([], {**BASE, "BRIDGE_HEALTH_PORT": port})


def test_the_old_key_flag_still_works():
    assert resolve_options(["--key", "k"], dict(BASE))["key"] == "k"
    assert resolve_options(["--access-key", "k"], dict(BASE))["key"] == "k"


# ---------------------------------------------------------------------------
# a refused token
# ---------------------------------------------------------------------------
def test_a_refused_token_leaves_token_rejected_for_kubernetes_and_exits_non_zero(tmp_path, monkeypatch):
    import mattermost_bridge.__main__ as cli
    from mattermost_bridge.mattermost import TokenRejected

    log = tmp_path / "termination-log"
    monkeypatch.setattr(cli, "TERMINATION_LOG", str(log))

    def refused(**options):
        raise TokenRejected()

    monkeypatch.setattr(cli, "connect_mattermost_to_hivemind", refused)
    monkeypatch.setattr("os.environ", dict(BASE))
    with pytest.raises(SystemExit) as stopped:
        cli.main([])
    assert stopped.value.code == cli.TOKEN_REJECTED_EXIT_CODE != 0
    assert log.read_text(encoding="utf-8") == "token_rejected"


def test_without_kubernetes_the_termination_log_is_skipped_quietly(tmp_path):
    from mattermost_bridge.__main__ import write_termination_message

    write_termination_message("token_rejected", str(tmp_path / "missing" / "termination-log"))


def test_any_other_stop_writes_no_termination_message(tmp_path, monkeypatch):
    import mattermost_bridge.__main__ as cli

    log = tmp_path / "termination-log"
    monkeypatch.setattr(cli, "TERMINATION_LOG", str(log))
    monkeypatch.setattr(cli, "connect_mattermost_to_hivemind", lambda **options: None)
    monkeypatch.setattr("os.environ", dict(BASE))
    cli.main([])
    assert not log.exists()


def test_sigterm_stops_the_bridge_like_ctrl_c(monkeypatch):
    """PID 1 in a container ignores a signal it has no handler for."""
    import signal

    import mattermost_bridge.__main__ as cli

    previous = signal.getsignal(signal.SIGTERM)
    seen = {}

    def run(**options):
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(cli, "connect_mattermost_to_hivemind", run)
    monkeypatch.setattr("os.environ", dict(BASE))
    try:
        cli.main([])  # returns: the SIGTERM became a clean shutdown
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert seen["handler"] is cli.stop_on_sigterm
