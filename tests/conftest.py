"""Shared fixtures: a local Mattermost server, a stand-in hub, a running bridge."""
import threading

import pytest

from fake_mattermost import FakeMattermost, make_hub, wait_for


@pytest.fixture
def fake():
    server = FakeMattermost().start()
    yield server
    server.stop()


class Running:
    """A bridge started on its own thread, and what ``start()`` raised."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.bridge.start()
        except BaseException as error:  # the test inspects it
            self.error = error

    def stop(self):
        self.bridge.stop()
        self.thread.join(10)


@pytest.fixture
def run_bridge(fake, monkeypatch):
    """Start a bridge against ``fake``; every one started is stopped afterwards."""
    import mattermost_bridge

    monkeypatch.setattr(mattermost_bridge, "RECONNECT_MIN_DELAY", 0.05)
    monkeypatch.setattr(mattermost_bridge, "RECONNECT_MAX_DELAY", 0.2)
    started = []

    def start(wait_ready=True, hub=None, api=None, **kwargs):
        from mattermost_bridge import HiveMindMattermostBridge

        bridge = HiveMindMattermostBridge(client=hub or make_hub(), api=api or fake.api(), **kwargs)
        running = Running(bridge)
        started.append(running)
        if wait_ready:
            assert wait_for(bridge.ready), "the bridge never became ready"
        return running

    yield start
    for running in started:
        running.stop()
