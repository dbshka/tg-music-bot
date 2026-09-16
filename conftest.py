import pytest
import socket
from unittest.mock import patch


@pytest.fixture(autouse=True)
def hermetic_dns_for_tests():
    orig_getaddrinfo = socket.getaddrinfo

    def hermetic_getaddrinfo(host, port=None, *args, **kwargs):
        known_hosts = (
            "www.youtube.com",
            "open.spotify.com",
            "youtube.com",
            "music.youtube.com",
            "soundcloud.com",
            "api.soundcloud.com",
            "itunes.apple.com",
        )
        if host in known_hosts:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.190.46", port or 443))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=hermetic_getaddrinfo):
        yield
