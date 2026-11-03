"""The REST side, against a local server: the token, the URL, the calls, the errors."""
import pytest

from fake_mattermost import BOT_ID, TOWN_SQUARE


@pytest.mark.parametrize("given, base", [
    ("https://chat.example.com", "https://chat.example.com"),
    ("https://chat.example.com/", "https://chat.example.com"),
    ("chat.example.com", "https://chat.example.com"),
    ("https://example.com:8443/mattermost/", "https://example.com:8443/mattermost"),
    ("https://chat.example.com/api/v4", "https://chat.example.com"),
    ("http://localhost:8065", "http://localhost:8065"),
])
def test_the_base_url_is_normalised(given, base):
    from mattermost_bridge.mattermost import normalise_url

    assert normalise_url(given) == base


@pytest.mark.parametrize("bad", [
    "", "ftp://chat.example.com", "https://", "https://bot:secret@chat.example.com",
    "https://chat.example.com/?team=x", "https://chat.example.com/#x",
])
def test_a_bad_url_is_refused(bad):
    from mattermost_bridge.mattermost import normalise_url

    with pytest.raises(ValueError):
        normalise_url(bad)


def test_the_websocket_follows_the_base_url():
    from mattermost_bridge.mattermost import websocket_url_for

    assert websocket_url_for("https://chat.example.com") == "wss://chat.example.com/api/v4/websocket"
    assert websocket_url_for("https://example.com:8443/mm") == "wss://example.com:8443/mm/api/v4/websocket"
    assert websocket_url_for("http://localhost:8065") == "ws://localhost:8065/api/v4/websocket"


def test_an_https_server_is_verified():
    import ssl

    from mattermost_bridge.mattermost import MattermostAPI

    context = MattermostAPI("https://chat.example.com", "tok").ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_ca_bundle_can_be_a_private_one(monkeypatch, tmp_path):
    from mattermost_bridge.mattermost import ca_bundle

    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    import certifi

    assert ca_bundle() == certifi.where()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    assert ca_bundle() == str(tmp_path / "ca.pem")


def test_me_names_the_bot(fake):
    assert fake.api().me() == {"id": BOT_ID, "username": "hivebot"}
    assert fake.calls == ["GET /api/v4/users/me"]


def test_a_wrong_token_is_rejected(fake):
    from mattermost_bridge.mattermost import TokenRejected

    with pytest.raises(TokenRejected):
        fake.api(token="not-the-token").me()
    fake.accept_token = False  # revoked
    with pytest.raises(TokenRejected):
        fake.api().create_post(TOWN_SQUARE, "hi")


def test_a_reply_is_posted_in_the_thread_when_given(fake):
    api = fake.api()
    top = api.create_post(TOWN_SQUARE, "top")
    api.create_post(TOWN_SQUARE, "in thread", root_id=top["id"])
    assert fake.created == [
        {"channel_id": TOWN_SQUARE, "message": "top"},
        {"channel_id": TOWN_SQUARE, "message": "in thread", "root_id": top["id"]},
    ]
    assert api.get_post(top["id"])["user_id"] == BOT_ID


def test_any_other_failure_names_the_call_and_status_not_a_body(fake):
    from mattermost_bridge.mattermost import MattermostError

    with pytest.raises(MattermostError) as failed:
        fake.api().get_post("x" * 26)
    assert failed.value.status == 404
    assert str(failed.value) == "GET /posts/{id} answered HTTP 404"
