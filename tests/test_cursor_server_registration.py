"""Cursor watcher is registered by the dashboard server."""

from hub.dashboard.server import DashboardServer
from hub.watchers.cursor_watcher import CursorWatcher


def test_cursor_watcher_registered_by_default():
    server = DashboardServer(host="localhost", port=0)
    labels = {label for label, _ in server.watchers}
    assert "Cursor" in labels
    assert any(isinstance(w, CursorWatcher) for _, w in server.watchers)


def test_cursor_excluded_when_not_in_providers():
    server = DashboardServer(host="localhost", port=0, providers=["claude"])
    assert not any(isinstance(w, CursorWatcher) for _, w in server.watchers)
