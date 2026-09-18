"""
Unit and integration tests for Yandex Music and VK support:
- Dual-instance VLESS proxy manager & routing
- Yandex Music track metadata and signed media URL resolution
- VK Music API resolution with VK_TOKEN
- Preview detection & rejection (refusing substitutions)
- SSRF protection on CDN hosts
- Cache key normalization for Yandex & VK
- Downloader direct CDN stream routing via Foreign VLESS
"""
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

import config
from services.vless_proxy import (
    start_foreign_vless_proxy,
    start_russian_vless_proxy,
    start_vless_proxy,
    stop_vless_proxy,
    get_foreign_proxy,
    get_russian_proxy,
    get_proxy_for_source
)
from services.yandex_vk import (
    extract_yandex_track_id,
    extract_vk_audio_id,
    is_safe_cdn_domain,
    resolve_yandex_music_track,
    resolve_vk_music_track,
    YANDEX_ALLOWED_SUFFIXES,
    VK_ALLOWED_SUFFIXES
)
from services.database import normalize_cache_key
from services.extractor import resolve_track_url


def test_proxy_routing_for_sources_and_stages():
    """Verify intelligent routing: resolve stage routes regional to RU, download stage routes all to Foreign."""
    config.YOUTUBE_PROXY = "socks5://127.0.0.1:10808"
    config.RUSSIAN_PROXY = "socks5://127.0.0.1:10809"

    # Stage: resolve
    assert get_proxy_for_source("yandex", stage="resolve") == "socks5://127.0.0.1:10809"
    assert get_proxy_for_source("yandex_music", stage="resolve") == "socks5://127.0.0.1:10809"
    assert get_proxy_for_source("ya.cc", stage="resolve") == "socks5://127.0.0.1:10809"
    assert get_proxy_for_source("vk", stage="resolve") == "socks5://127.0.0.1:10809"
    assert get_proxy_for_source("vk_music", stage="resolve") == "socks5://127.0.0.1:10809"
    assert get_proxy_for_source("youtube", stage="resolve") == "socks5://127.0.0.1:10808"
    assert get_proxy_for_source("spotify", stage="resolve") == "socks5://127.0.0.1:10808"
    assert get_proxy_for_source("apple", stage="resolve") == "socks5://127.0.0.1:10808"
    assert get_proxy_for_source("soundcloud", stage="resolve") == "socks5://127.0.0.1:10808"

    # Stage: download (all media CDN downloads use Foreign proxy)
    assert get_proxy_for_source("yandex", stage="download") == "socks5://127.0.0.1:10808"
    assert get_proxy_for_source("vk", stage="download") == "socks5://127.0.0.1:10808"
    assert get_proxy_for_source("youtube", stage="download") == "socks5://127.0.0.1:10808"
    assert get_proxy_for_source("soundcloud", stage="download") == "socks5://127.0.0.1:10808"


def test_dual_vless_instances_lifecycle(tmp_path):
    """Test independent startup and teardown of Foreign and Russian sing-box instances."""
    mock_foreign_proc = MagicMock()
    mock_foreign_proc.poll.return_value = None

    mock_russian_proc = MagicMock()
    mock_russian_proc.poll.return_value = None

    vless_foreign = "vless://11111111-1111-1111-1111-111111111111@vpn-foreign.com:443?security=reality#Foreign"
    vless_russian = "vless://22222222-2222-2222-2222-222222222222@vpn-russian.ru:443?security=reality#Russian"

    procs = [mock_foreign_proc, mock_russian_proc]

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("services.vless_proxy.verify_outbound_connectivity", return_value=(True, "1.2.3.4")), \
         patch("subprocess.Popen", side_effect=procs), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):

        # 1. Start Foreign
        f_proc = start_foreign_vless_proxy(vless_input=vless_foreign, socks_port=10808)
        assert f_proc is mock_foreign_proc
        assert config.YOUTUBE_PROXY == "socks5://127.0.0.1:10808"
        assert get_foreign_proxy() == "socks5://127.0.0.1:10808"

        # 2. Start Russian
        r_proc = start_russian_vless_proxy(vless_input=vless_russian, socks_port=10809)
        assert r_proc is mock_russian_proc
        assert config.RUSSIAN_PROXY == "socks5://127.0.0.1:10809"
        assert get_russian_proxy() == "socks5://127.0.0.1:10809"

        # 3. Stop Foreign individually
        stop_vless_proxy(f_proc)
        assert config.YOUTUBE_PROXY is None
        assert get_foreign_proxy() is None
        assert config.RUSSIAN_PROXY == "socks5://127.0.0.1:10809"

        # 4. Stop all
        stop_vless_proxy()
        assert config.RUSSIAN_PROXY is None
        assert get_russian_proxy() is None


def test_extract_yandex_track_id():
    assert extract_yandex_track_id("https://music.yandex.ru/album/123/track/60292250") == "60292250"
    assert extract_yandex_track_id("https://music.yandex.ru/track/60292250") == "60292250"
    assert extract_yandex_track_id("https://music.yandex.com/album/456?track=60292250") == "60292250"
    assert extract_yandex_track_id("https://music.yandex.kz/track/123456?from=suggest") == "123456"
    assert extract_yandex_track_id("https://music.yandex.ru/album/123") is None


def test_extract_vk_audio_id():
    assert extract_vk_audio_id("https://vk.com/audio-2001429780_128429780") == ("-2001429780", "128429780")
    assert extract_vk_audio_id("https://vk.com/audio123456_789012") == ("123456", "789012")
    assert extract_vk_audio_id("https://vk.com/audio?z=audio-2001429780_128429780%2Fstatus") == ("-2001429780", "128429780")
    assert extract_vk_audio_id("https://vk.com/video-123_456") is None


def test_safe_cdn_domain_validation():
    assert is_safe_cdn_domain("ext-strm-1.strm.yandex.net", YANDEX_ALLOWED_SUFFIXES) is True
    assert is_safe_cdn_domain("api.music.yandex.net", YANDEX_ALLOWED_SUFFIXES) is True
    assert is_safe_cdn_domain("cs1-2.vkuser.net", VK_ALLOWED_SUFFIXES) is True
    assert is_safe_cdn_domain("api.vk.com", VK_ALLOWED_SUFFIXES) is True

    # Block malicious / local / SSRF hosts
    assert is_safe_cdn_domain("127.0.0.1", YANDEX_ALLOWED_SUFFIXES) is False
    assert is_safe_cdn_domain("localhost", YANDEX_ALLOWED_SUFFIXES) is False
    assert is_safe_cdn_domain("169.254.169.254", YANDEX_ALLOWED_SUFFIXES) is False
    assert is_safe_cdn_domain("evil-yandex.net.attacker.com", YANDEX_ALLOWED_SUFFIXES) is False
    assert is_safe_cdn_domain("vkuser.net.attacker.org", VK_ALLOWED_SUFFIXES) is False


@pytest.mark.asyncio
async def test_resolve_yandex_music_success():
    """Verify Yandex Music metadata and signed media URL calculation."""
    fake_meta = {
        "result": [
            {
                "id": 60292250,
                "title": "Blinding Lights",
                "available": True,
                "artists": [{"name": "The Weeknd"}],
                "durationMs": 200040,
                "ogImage": "avatars.yandex.net/get-music-content/123/%%"
            }
        ]
    }

    fake_download_info = {
        "result": [
            {
                "codec": "mp3",
                "bitrateInKbps": 320,
                "preview": False,
                "downloadInfoUrl": "https://api.music.yandex.net/download-info/60292250/xml"
            }
        ]
    }

    fake_xml = """<download-info>
        <host>api.music.yandex.net</host>
        <path>/get-mp3-path/file.mp3</path>
        <ts>1726000000</ts>
        <s>randomsalt123</s>
    </download-info>"""

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        if "/tracks/60292250/download-info" in url:
            mock_resp.json.return_value = fake_download_info
        elif "/tracks/60292250" in url:
            mock_resp.json.return_value = fake_meta
        elif "xml" in url:
            mock_resp.text = fake_xml
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")
        assert res["platform"] == "Yandex Music"
        assert res["title"] == "Blinding Lights"
        assert res["artist"] == "The Weeknd"
        assert res["duration"] == 200
        assert res["thumbnail_url"] == "https://avatars.yandex.net/get-music-content/123/600x600"
        assert res["target"].startswith("https://api.music.yandex.net/get-mp3/")
        assert "track-id=60292250" in res["target"]


@pytest.mark.asyncio
async def test_resolve_yandex_music_rejects_preview():
    """Verify that Plus-only preview tracks are strictly rejected to avoid substituting a 30s preview."""
    fake_meta = {
        "result": [
            {
                "id": 60292250,
                "title": "Blinding Lights",
                "available": True,
                "artists": [{"name": "The Weeknd"}],
                "durationMs": 200040
            }
        ]
    }

    fake_download_info_preview = {
        "result": [
            {
                "codec": "mp3",
                "bitrateInKbps": 192,
                "preview": True,
                "downloadInfoUrl": "https://api.music.yandex.net/download-info/60292250/xml"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        if "/tracks/60292250/download-info" in url:
            mock_resp.json.return_value = fake_download_info_preview
        elif "/tracks/60292250" in url:
            mock_resp.json.return_value = fake_meta
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        with pytest.raises(ValueError, match="только по подписке Яндекс Плюс"):
            await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")


@pytest.mark.asyncio
async def test_resolve_yandex_music_rejects_unavailable_track():
    fake_meta = {
        "result": [
            {
                "id": 60292250,
                "title": "Unavailable Song",
                "available": False,
                "error": "not-available-for-user"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_meta
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        with pytest.raises(ValueError, match="Трек недоступен в каталоге"):
            await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")


@pytest.mark.asyncio
async def test_resolve_yandex_music_rejects_ssrf_host():
    fake_meta = {
        "result": [{"id": 1, "title": "Test", "available": True, "artists": []}]
    }
    fake_d_info = {
        "result": [{"codec": "mp3", "preview": False, "downloadInfoUrl": "https://api.music.yandex.net/xml"}]
    }
    malicious_xml = "<download-info><host>127.0.0.1</host><path>/file.mp3</path><ts>1</ts><s>salt</s></download-info>"

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        if "download-info" in url:
            mock_resp.json.return_value = fake_d_info
        elif "xml" in url:
            mock_resp.text = malicious_xml
        else:
            mock_resp.json.return_value = fake_meta
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        with pytest.raises(ValueError, match="Недопустимый хост CDN"):
            await resolve_yandex_music_track("https://music.yandex.ru/track/1")


@pytest.mark.asyncio
async def test_resolve_vk_music_requires_token():
    with patch.object(config, "VK_TOKEN", None):
        with pytest.raises(ValueError, match="требует указания VK_TOKEN"):
            await resolve_vk_music_track("https://vk.com/audio-2001429780_128429780")


@pytest.mark.asyncio
async def test_resolve_vk_music_success():
    fake_vk_resp = {
        "response": [
            {
                "id": 128429780,
                "owner_id": -2001429780,
                "artist": "MiyaGi & Эндшпиль",
                "title": "Captain",
                "duration": 215,
                "url": "https://cs1-2.vkuser.net/audio/stream123.mp3",
                "album": {
                    "thumb": {
                        "photo_600": "https://sun9-1.userapi.com/cover600.jpg"
                    }
                }
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_vk_resp
        return mock_resp

    with patch.object(config, "VK_TOKEN", "fake_valid_vk_token"), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_vk_music_track("https://vk.com/audio-2001429780_128429780")
        assert res["platform"] == "VK Music"
        assert res["title"] == "Captain"
        assert res["artist"] == "MiyaGi & Эндшпиль"
        assert res["duration"] == 215
        assert res["target"] == "https://cs1-2.vkuser.net/audio/stream123.mp3"
        assert res["thumbnail_url"] == "https://sun9-1.userapi.com/cover600.jpg"


@pytest.mark.asyncio
async def test_resolve_vk_music_token_expired():
    fake_vk_err = {
        "error": {
            "error_code": 5,
            "error_msg": "User authorization failed: access_token was given to another ip address."
        }
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_vk_err
        return mock_resp

    with patch.object(config, "VK_TOKEN", "expired_token"), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        with pytest.raises(ValueError, match="VK_TOKEN.*недействителен или истёк"):
            await resolve_vk_music_track("https://vk.com/audio-2001429780_128429780")


@pytest.mark.asyncio
async def test_resolve_vk_music_rejects_ssrf_host():
    fake_vk_resp = {
        "response": [
            {
                "id": 128429780,
                "owner_id": -2001429780,
                "artist": "Artist",
                "title": "Title",
                "duration": 180,
                "url": "http://169.254.169.254/latest/meta-data"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_vk_resp
        return mock_resp

    with patch.object(config, "VK_TOKEN", "token"), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        with pytest.raises(ValueError, match="Недопустимый сервер аудиопотока"):
            await resolve_vk_music_track("https://vk.com/audio-2001429780_128429780")


def test_cache_key_normalization_yandex_and_vk():
    assert normalize_cache_key("https://music.yandex.ru/album/123/track/60292250") == "yandex:60292250"
    assert normalize_cache_key("https://music.yandex.ru/track/60292250?from=button") == "yandex:60292250"
    assert normalize_cache_key("https://vk.com/audio-2001429780_128429780") == "vk:-2001429780_128429780"
    assert normalize_cache_key("https://vk.com/audio?z=audio-2001429780_128429780%2Fstatus") == "vk:-2001429780_128429780"


def test_downloader_yandex_direct_cdn_stream_options(tmp_path):
    """Verify that when downloader processes a direct CDN stream URL, Foreign proxy is used and YouTube options omitted."""
    from services.downloader import _sync_download
    recorded_opts = []

    class CapturingFakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)
            recorded_opts.append(self.opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            f = tmp_path / "stream.m4a"
            f.write_bytes(b"\x00" * 1000)
            return {"title": "Streamed Song", "duration": 200}

    yandex_cdn_url = "https://ext-strm-1.strm.yandex.net/music-v2/raw/ysign1=abc/track-id=60292250"
    foreign_proxy = "socks5://127.0.0.1:10808"

    with patch("services.downloader.YOUTUBE_PROXY", foreign_proxy), \
         patch("yt_dlp.YoutubeDL", side_effect=CapturingFakeYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):

        res = _sync_download(
            query_or_url=yandex_cdn_url,
            output_dir=tmp_path,
            custom_title="Blinding Lights",
            custom_artist="The Weeknd",
            expected_duration=200
        )
        assert res is not None

    assert len(recorded_opts) >= 1
    dl_opts = recorded_opts[0]
    # Foreign proxy is applied to the CDN stream
    assert dl_opts.get("proxy") == foreign_proxy
    # YouTube-specific extractor args are removed
    assert "extractor_args" not in dl_opts
    assert "cookiefile" not in dl_opts


@pytest.mark.asyncio
async def test_resolve_track_url_dispatches_yandex_and_vk():
    fake_ym_track = {
        "platform": "Yandex Music",
        "target": "https://api.music.yandex.net/get-mp3/test_signed_url",
        "is_search": False,
        "title": "Test YM Song",
        "artist": "Test Artist",
        "thumbnail_url": "https://avatars.yandex.net/cover.jpg",
        "duration": 180,
        "track_id": "12345"
    }

    fake_vk_track = {
        "platform": "VK Music",
        "target": "https://cs1-2.vkuser.net/stream.mp3",
        "is_search": False,
        "title": "Test VK Song",
        "artist": "VK Artist",
        "thumbnail_url": None,
        "duration": 210,
        "track_id": "1_2"
    }

    with patch("services.extractor.resolve_yandex_music_track", return_value=fake_ym_track) as mock_ym, \
         patch("services.extractor.resolve_vk_music_track", return_value=fake_vk_track) as mock_vk, \
         patch("services.extractor.is_safe_url", return_value=(True, "")):

        ym_res = await resolve_track_url("https://music.yandex.ru/track/12345")
        assert ym_res.platform == "Yandex Music"
        assert ym_res.title == "Test YM Song"
        assert ym_res.target == "https://api.music.yandex.net/get-mp3/test_signed_url"
        mock_ym.assert_called_once()

        vk_res = await resolve_track_url("https://vk.com/audio1_2")
        assert vk_res.platform == "VK Music"
        assert vk_res.title == "Test VK Song"
        assert vk_res.target == "https://cs1-2.vkuser.net/stream.mp3"
        mock_vk.assert_called_once()
