import pytest
from services.security import is_ip_blocked, is_safe_url
from services.extractor import resolve_track_url, parse_url_and_modifiers


def test_ssrf_ip_blocking():
    # Loopback
    assert is_ip_blocked("127.0.0.1") is True
    assert is_ip_blocked("127.1.2.3") is True
    # Private RFC 1918
    assert is_ip_blocked("10.0.0.1") is True
    assert is_ip_blocked("172.16.0.1") is True
    assert is_ip_blocked("192.168.1.1") is True
    # Cloud metadata
    assert is_ip_blocked("169.254.169.254") is True
    # IPv6 Loopback and Unique Local
    assert is_ip_blocked("::1") is True
    assert is_ip_blocked("fc00::1") is True
    # Public IP
    assert is_ip_blocked("8.8.8.8") is False
    assert is_ip_blocked("1.1.1.1") is False


def test_is_safe_url():
    safe1, _ = is_safe_url("http://127.0.0.1/admin")
    assert safe1 is False
    safe2, _ = is_safe_url("http://169.254.169.254/latest/meta-data")
    assert safe2 is False
    safe3, _ = is_safe_url("http://localhost:8080")
    assert safe3 is False

    from unittest.mock import patch
    import socket
    real_gai = socket.getaddrinfo
    def mock_gai(host, port, *args, **kwargs):
        if "youtube" in host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('142.250.180.14', 443))]
        if "spotify" in host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('35.186.224.25', 443))]
        return real_gai(host, port, *args, **kwargs)

    with patch("socket.getaddrinfo", side_effect=mock_gai):
        safe4, _ = is_safe_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert safe4 is True
        safe5, _ = is_safe_url("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        assert safe5 is True


@pytest.mark.asyncio
async def test_unsupported_containers_rejected():
    # Spotify playlist & album
    with pytest.raises(ValueError):
        await resolve_track_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")

    with pytest.raises(ValueError):
        await resolve_track_url("https://open.spotify.com/album/4eLPsYPBmXABThSJ821sqY")

    # YouTube playlist
    with pytest.raises(ValueError):
        await resolve_track_url("https://www.youtube.com/playlist?list=PL12345")

    # SoundCloud sets
    with pytest.raises(ValueError):
        await resolve_track_url("https://soundcloud.com/user/sets/album")


def test_parse_url_and_modifiers():
    url, mods, mult = parse_url_and_modifiers("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT slowed")
    assert url == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    assert "slowed" in mods
    assert mult is None

    url2, mods2, mult2 = parse_url_and_modifiers("https://youtu.be/dQw4w9WgXcQ 1.25x")
    assert url2 == "https://youtu.be/dQw4w9WgXcQ"
    assert mult2 == 1.25
