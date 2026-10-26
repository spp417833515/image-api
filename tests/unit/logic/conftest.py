"""Unit tests of the logic layer never use the network: any DNS lookup or socket connect fails the test loudly.

This is the backstop behind every test's unreachable base_url (offline.UNREACHABLE_URL): if a test's stand-in for
ComfyUI stops being used (e.g. a transport is dropped), the test fails instead of reaching the real ComfyUI.
"""

from __future__ import annotations

import socket

import pytest


class NetworkUsed(OSError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkUsed(f"unit tests must not use the network, attempted with {args!r}")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
