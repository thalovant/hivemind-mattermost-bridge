"""Liveness and readiness, as a container platform asks for them."""
import urllib.error
import urllib.request

import pytest

from mattermost_bridge.health import HealthServer


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def served():
    state = {"ready": False}
    server = HealthServer(0, lambda: state["ready"], host="127.0.0.1")
    server.start()
    yield server, state
    server.stop()


def test_liveness_answers_while_not_ready(served):
    server, _ = served
    assert get(server.port, "/healthz") == 200
    assert get(server.port, "/readyz") == 503


def test_readiness_follows_the_bridge(served):
    server, state = served
    state["ready"] = True
    assert get(server.port, "/readyz") == 200
    state["ready"] = False
    assert get(server.port, "/readyz") == 503


def test_a_failing_check_is_not_ready_not_a_crash():
    def broken():
        raise RuntimeError("boom")

    server = HealthServer(0, broken, host="127.0.0.1")
    server.start()
    try:
        assert get(server.port, "/readyz") == 503
        assert get(server.port, "/healthz") == 200
    finally:
        server.stop()


def test_anything_else_is_not_found(served):
    server, _ = served
    assert get(server.port, "/") == 404
