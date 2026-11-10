"""All tests in this suite are strictly offline, including subprocesses' transports."""
import socket
import pytest


@pytest.fixture(autouse=True)
def deny_network(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise AssertionError("External networking is forbidden in media library tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setenv("PIXABAY_API_KEY", "offline-test-secret")
    monkeypatch.setenv("MEDIA_LIBRARY_ROOT", str(tmp_path))
