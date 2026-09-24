"""
Unit and integration tests for Yandex Music and VK support:
- Dual-instance VLESS proxy manager & routing
- Yandex Music track metadata resolution to YouTube search
- VK Music API resolution with VK_TOKEN to YouTube search
- Preview tolerance (allows 30s preview metadata for full YouTube search)
- SSRF regression protection (VK API url field ignored)
- Cache key normalization for Yandex & VK
- YouTube candidate selection, duration validation & metadata injection
- Full end-to-end pipeline test
"""
import asyncio
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
    extract_yandex_album_id,
    normalize_yandex_url,
    extract_vk_audio_id,
    resolve_yandex_music_track,
    resolve_vk_music_track,
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


@pytest.mark.asyncio
async def test_resolve_vk_music_ignores_malicious_api_url_and_routes_to_youtube():
    """
    Security regression test:
    Verify that an arbitrary or internal/SSRF url in VK API response (e.g., http://169.254.169.254/...)
    is strictly ignored and NEVER passed to the downloader.
    The resolved output must only produce YouTube search target (ytsearch5:artist - title).
    """
    fake_vk_resp = {
        "response": [
            {
                "id": 128429780,
                "owner_id": -2001429780,
                "artist": "Attacker Artist",
                "title": "Malicious Song",
                "duration": 180,
                "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
                "album": {
                    "title": "Hacked Album",
                    "thumb": {"photo_600": "https://sun9-1.userapi.com/cover600.jpg"}
                }
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_vk_resp
        return mock_resp

    with patch.object(config, "VK_TOKEN", "test_vk_token"), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_vk_music_track("https://vk.com/audio-2001429780_128429780")
        assert res["platform"] == "VK Music"
        assert res["title"] == "Malicious Song"
        assert res["artist"] == "Attacker Artist"
        assert res["album"] == "Hacked Album"
        # The malicious 'url' from VK API must NOT be in the result or used as target
        assert "url" not in res
        assert "169.254.169.254" not in res["target"]
        assert res["target"] == "ytsearch5:Attacker Artist - Malicious Song"
        assert res["is_search"] is True



@pytest.mark.asyncio
async def test_resolve_yandex_music_success():
    """Verify Yandex Music extracts metadata, album, and returns ytsearch target without download-info call."""
    fake_meta = {
        "result": [
            {
                "id": 60292250,
                "title": "Blinding Lights",
                "available": True,
                "artists": [{"name": "The Weeknd"}],
                "albums": [{"title": "After Hours"}],
                "durationMs": 200040,
                "ogImage": "avatars.yandex.net/get-music-content/123/%%"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_meta
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")
        assert res["platform"] == "Yandex Music"
        assert res["title"] == "Blinding Lights"
        assert res["artist"] == "The Weeknd"
        assert res["album"] == "After Hours"
        assert res["duration"] == 200
        assert res["thumbnail_url"] == "https://avatars.yandex.net/get-music-content/123/600x600"
        assert res["target"] == "ytsearch5:The Weeknd - Blinding Lights"
        assert res["is_search"] is True


@pytest.mark.asyncio
async def test_resolve_yandex_music_allows_preview():
    """Verify that 30s preview (Plus-only track) is NOT an error and returns metadata for YouTube search."""
    fake_meta_preview = {
        "result": [
            {
                "id": 60292250,
                "title": "Blinding Lights",
                "available": True,
                "preview": True,
                "artists": [{"name": "The Weeknd"}],
                "albums": [{"title": "After Hours"}],
                "durationMs": 200040,
                "ogImage": "avatars.yandex.net/get-music-content/123/%%"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_meta_preview
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")
        assert res["platform"] == "Yandex Music"
        assert res["title"] == "Blinding Lights"
        assert res["artist"] == "The Weeknd"
        assert res["target"] == "ytsearch5:The Weeknd - Blinding Lights"
        assert res["is_search"] is True


@pytest.mark.asyncio
async def test_resolve_yandex_music_without_token():
    """Verify that YANDEX_MUSIC_TOKEN is optional and Yandex links still resolve to YouTube search."""
    fake_meta = {
        "result": [
            {
                "id": 60292250,
                "title": "Save Your Tears",
                "artists": [{"name": "The Weeknd"}],
                "durationMs": 215000
            }
        ]
    }

    recorded_headers = []
    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        recorded_headers.append(headers or {})
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_meta
        return mock_resp

    with patch.object(config, "YANDEX_MUSIC_TOKEN", None), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")
        assert res["title"] == "Save Your Tears"
        assert res["target"] == "ytsearch5:The Weeknd - Save Your Tears"
        assert res["is_search"] is True
        # Authorization header should NOT be present when token is None
        assert "Authorization" not in recorded_headers[0]


@pytest.mark.asyncio
async def test_resolve_yandex_music_rejects_unavailable_without_metadata():
    """Verify error is raised only when metadata cannot be retrieved at all."""
    fake_meta_err = {
        "result": [
            {
                "id": 60292250,
                "available": False,
                "error": "plus-only-track"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_meta_err
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        with pytest.raises(ValueError, match="только по подписке Яндекс Плюс"):
            await resolve_yandex_music_track("https://music.yandex.ru/track/60292250")


@pytest.mark.asyncio
async def test_resolve_vk_music_requires_token():
    with patch.object(config, "VK_TOKEN", None):
        with pytest.raises(ValueError, match="VK_TOKEN"):
            await resolve_vk_music_track("https://vk.com/audio-2001429780_128429780")


@pytest.mark.asyncio
async def test_resolve_vk_music_success():
    """Verify VK Music extracts metadata, album and returns ytsearch target without direct media downloading."""
    fake_vk_resp = {
        "response": [
            {
                "id": 128429780,
                "owner_id": -2001429780,
                "artist": "MiyaGi & Эндшпиль",
                "title": "Captain",
                "duration": 215,
                "album": {
                    "title": "Buster Keaton",
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
        assert res["album"] == "Buster Keaton"
        assert res["duration"] == 215
        assert res["target"] == "ytsearch5:MiyaGi & Эндшпиль - Captain"
        assert res["is_search"] is True
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



def test_cache_key_normalization_yandex_and_vk():
    assert normalize_cache_key("https://music.yandex.ru/album/123/track/60292250") == "yandex:60292250"
    assert normalize_cache_key("https://music.yandex.ru/track/60292250?from=button") == "yandex:60292250"
    assert normalize_cache_key("https://vk.com/audio-2001429780_128429780") == "vk:-2001429780_128429780"
    assert normalize_cache_key("https://vk.com/audio?z=audio-2001429780_128429780%2Fstatus") == "vk:-2001429780_128429780"


@pytest.mark.asyncio
async def test_yandex_full_pipeline_end_to_end():
    """
    End-to-end integration test:
    Yandex URL -> resolve_track_url -> ExtractedTrack -> download_track ->
    ytsearch5 candidate ranking -> YouTube download -> source metadata injection.
    """
    from services.downloader import download_track

    fake_meta = {
        "result": [
            {
                "id": 60292250,
                "title": "Blinding Lights",
                "available": True,
                "artists": [{"name": "The Weeknd"}],
                "albums": [{"title": "After Hours"}],
                "durationMs": 200000,
                "ogImage": "avatars.yandex.net/get-music-content/123/%%"
            }
        ]
    }

    fake_yt_candidates = [
        {
            "id": "yt_wknd_1",
            "url": "https://www.youtube.com/watch?v=yt_wknd_1",
            "webpage_url": "https://www.youtube.com/watch?v=yt_wknd_1",
            "title": "The Weeknd - Blinding Lights (Official Audio)",
            "duration": 200,
            "_source": "youtube",
            "channel": "The Weeknd - Topic"
        }
    ]

    recorded_downloads = []
    recorded_tags = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if download:
                recorded_downloads.append((url, self.opts.get("proxy")))
                outtmpl = str(self.opts.get("outtmpl", ""))
                if "%" in outtmpl:
                    out = Path(outtmpl.split("%")[0] + "audio.m4a")
                else:
                    out = Path(outtmpl)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"\x00" * 4000)
                return {
                    "id": "yt_wknd_1",
                    "title": "The Weeknd - Blinding Lights (Official Audio)",
                    "duration": 200,
                    "ext": "m4a"
                }
            return {"entries": fake_yt_candidates}

    def fake_apply(audio_path, title, artist, cover_path=None, album=None):
        recorded_tags.append({"title": title, "artist": artist, "album": album})

    with patch("services.yandex_vk._sync_http_request") as mock_http, \
         patch("yt_dlp.YoutubeDL", side_effect=FakeYDL), \
         patch("services.downloader._apply_custom_metadata", side_effect=fake_apply), \
         patch("mutagen.File", return_value=MagicMock(info=MagicMock(length=200, sample_rate=44100))), \
         patch("services.downloader.get_current_youtube_proxy", return_value="socks5://127.0.0.1:10808"), \
         patch("services.yandex_vk.get_proxy_for_source", return_value="socks5://127.0.0.1:10809"):

        mock_http.return_value.json.return_value = fake_meta

        # 1. Resolve Yandex track URL
        track = await resolve_track_url("https://music.yandex.ru/track/60292250")
        assert track.platform == "Yandex Music"
        assert track.title == "Blinding Lights"
        assert track.artist == "The Weeknd"
        assert track.album == "After Hours"
        assert track.duration == 200
        assert track.is_search is True
        assert track.target == "ytsearch5:The Weeknd - Blinding Lights"

        # 2. Download track through YouTube pipeline
        is_apple_music = track.platform in ("Apple Music", "Spotify", "Deezer", "Yandex Music", "VK Music")
        audio = await download_track(
            query_or_url=track.target,
            custom_title=track.title,
            custom_artist=track.artist,
            custom_album=track.album,
            expected_duration=track.duration,
            is_apple_music=is_apple_music
        )

        assert audio is not None
        assert audio.title == "Blinding Lights"
        assert audio.artist == "The Weeknd"
        assert audio.duration == 200

        # Verify YouTube was downloaded, NOT Yandex
        assert len(recorded_downloads) == 1
        dl_url, dl_proxy = recorded_downloads[0]
        assert dl_url == "https://www.youtube.com/watch?v=yt_wknd_1"
        assert dl_proxy == "socks5://127.0.0.1:10808"

        # Verify source metadata was injected into MP3 tags
        assert len(recorded_tags) >= 1
        assert recorded_tags[-1]["title"] == "Blinding Lights"
        assert recorded_tags[-1]["artist"] == "The Weeknd"
        assert recorded_tags[-1]["album"] == "After Hours"


@pytest.mark.asyncio
async def test_yandex_full_pipeline_rejects_on_duration_mismatch():
    """
    Verify that if YouTube candidate search returns candidates whose duration
    differs by more than 4 seconds from the canonical Yandex duration,
    it is rejected to preserve authenticity.
    """
    from services.downloader import download_track

    # Yandex track duration is 200s, but YouTube candidate is 300s
    fake_yt_candidates = [
        {
            "id": "yt_wrong_dur",
            "url": "https://www.youtube.com/watch?v=yt_wrong_dur",
            "webpage_url": "https://www.youtube.com/watch?v=yt_wrong_dur",
            "title": "The Weeknd - Blinding Lights (Extended Remix)",
            "duration": 300,
            "_source": "youtube"
        }
    ]

    class FakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if download:
                outtmpl = str(self.opts.get("outtmpl", ""))
                out = Path(outtmpl.split("%")[0] + "audio.m4a") if "%" in outtmpl else Path(outtmpl)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"\x00" * 4000)
                return {
                    "id": "yt_wrong_dur",
                    "title": "The Weeknd - Blinding Lights (Extended Remix)",
                    "duration": 300,
                    "ext": "m4a"
                }
            return {"entries": fake_yt_candidates}

    with patch("yt_dlp.YoutubeDL", side_effect=FakeYDL), \
         patch("mutagen.File", return_value=MagicMock(info=MagicMock(length=300, sample_rate=44100))), \
         patch("services.downloader.get_current_youtube_proxy", return_value="socks5://127.0.0.1:10808"):

        with pytest.raises(ValueError, match="(Ни один кандидат поиска не подошел|не совпадает по длительности)"):
            await download_track(
                query_or_url="ytsearch5:The Weeknd - Blinding Lights",
                custom_title="Blinding Lights",
                custom_artist="The Weeknd",
                custom_album="After Hours",
                expected_duration=200,
                is_apple_music=True
            )



@pytest.mark.asyncio
async def test_resolve_track_url_dispatches_yandex_and_vk():
    fake_ym_track = {
        "platform": "Yandex Music",
        "target": "ytsearch5:Test Artist - Test YM Song",
        "is_search": True,
        "title": "Test YM Song",
        "artist": "Test Artist",
        "album": "Test Album",
        "thumbnail_url": "https://avatars.yandex.net/cover.jpg",
        "duration": 180,
        "track_id": "12345"
    }

    fake_vk_track = {
        "platform": "VK Music",
        "target": "ytsearch5:VK Artist - Test VK Song",
        "is_search": True,
        "title": "Test VK Song",
        "artist": "VK Artist",
        "album": "VK Album",
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
        assert ym_res.artist == "Test Artist"
        assert ym_res.album == "Test Album"
        assert ym_res.is_search is True
        assert ym_res.target == "ytsearch5:Test Artist - Test YM Song"
        mock_ym.assert_called_once()

        vk_res = await resolve_track_url("https://vk.com/audio1_2")
        assert vk_res.platform == "VK Music"
        assert vk_res.title == "Test VK Song"
        assert vk_res.artist == "VK Artist"
        assert vk_res.album == "VK Album"
        assert vk_res.is_search is True
        assert vk_res.target == "ytsearch5:VK Artist - Test VK Song"
        mock_vk.assert_called_once()


def test_youtube_downloader_receives_candidate_and_applies_source_metadata(tmp_path):
    """
    Verify:
    1. YouTube downloader receives the exact URL of the found YouTube track.
    2. Metadata of the final file (title, artist, album) are populated from the source Yandex/VK track.
    """
    from services.downloader import _sync_download
    recorded_apply_calls = []
    recorded_extract_calls = []

    fake_candidates = [
        {
            "id": "yt_video_123",
            "url": "https://www.youtube.com/watch?v=yt_video_123",
            "webpage_url": "https://www.youtube.com/watch?v=yt_video_123",
            "title": "The Weeknd - Blinding Lights (Official Audio)",
            "duration": 200,
            "_source": "youtube"
        }
    ]

    class FakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            recorded_extract_calls.append((url, download))
            if download:
                out = tmp_path / "audio.m4a"
                out.write_bytes(b"\x00" * 2000)
                return {
                    "id": "yt_video_123",
                    "title": "The Weeknd - Blinding Lights (Official Audio)",
                    "duration": 200,
                    "ext": "m4a"
                }
            return {"entries": fake_candidates}

    def fake_apply(audio_path, title, artist, cover_path=None, album=None):
        recorded_apply_calls.append({
            "audio_path": audio_path,
            "title": title,
            "artist": artist,
            "cover_path": cover_path,
            "album": album
        })

    with patch("yt_dlp.YoutubeDL", side_effect=FakeYDL), \
         patch("services.downloader._apply_custom_metadata", side_effect=fake_apply), \
         patch("mutagen.File", return_value=MagicMock(info=MagicMock(length=200, sample_rate=44100))):

        audio = _sync_download(
            query_or_url="ytsearch5:The Weeknd - Blinding Lights",
            output_dir=tmp_path,
            custom_title="Blinding Lights",
            custom_artist="The Weeknd",
            custom_album="After Hours",
            expected_duration=200,
            is_apple_music=True
        )

        assert audio is not None
        assert audio.title == "Blinding Lights"
        assert audio.artist == "The Weeknd"
        assert audio.duration == 200

    # 1. Verify YouTube downloader was called to download the exact YouTube track URL
    download_urls = [u for u, dl in recorded_extract_calls if dl]
    assert len(download_urls) == 1
    assert download_urls[0] == "https://www.youtube.com/watch?v=yt_video_123"

    # 2. Verify source metadata (title, artist, album) were applied
    assert len(recorded_apply_calls) >= 1
    last_applied = recorded_apply_calls[-1]
    assert last_applied["title"] == "Blinding Lights"
    assert last_applied["artist"] == "The Weeknd"
    assert last_applied["album"] == "After Hours"


@pytest.mark.asyncio
async def test_yandex_draxxxy_clubb_regression(tmp_path):
    """
    Regression test for Yandex Music track Draxxxy — Clubb (154538326):
    1. Resolves metadata (artist, title, album, duration 134s)
    2. Generates search target ytsearch5:Draxxxy - Clubb
    3. Candidate scoring penalizes unrelated tracks (e.g. 'Club Bizarre' by 'U 96')
    4. Legitimate candidate with matching artist/title/duration is selected
    5. When YouTube returns 0 candidates, raises ValueError safely.
    """
    from services.downloader import compute_candidate_penalty, _sync_download

    fake_meta = {
        "result": [
            {
                "id": 154538326,
                "title": "Clubb",
                "available": True,
                "artists": [{"name": "Draxxxy"}],
                "albums": [{"title": "Clubb"}],
                "durationMs": 134200,
                "ogImage": "avatars.yandex.net/get-music-content/17659805/1658ab98.a.43437214-2/%%"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_meta
        return mock_resp

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_yandex_music_track("https://music.yandex.ru/album/43437214/track/154538326")
        assert res["platform"] == "Yandex Music"
        assert res["title"] == "Clubb"
        assert res["artist"] == "Draxxxy"
        assert res["album"] == "Clubb"
        assert res["duration"] == 134
        assert res["target"] == "ytsearch5:Draxxxy - Clubb"

    # Verify candidate penalty on unrelated tracks
    unrelated_cand = {
        "title": "U 96 - Club Bizarre",
        "uploader": "U 96",
        "duration": 301,
        "id": "u96_vid"
    }
    pen_unrelated = compute_candidate_penalty(
        candidate=unrelated_cand,
        custom_artist="Draxxxy",
        custom_title="Clubb",
        expected_duration=134,
        is_apple_music=True
    )
    assert pen_unrelated > 4000.0, "Unrelated track must be penalized heavily"

    # Verify candidate penalty on matching authentic candidate
    legit_cand = {
        "title": "Draxxxy - Clubb (Official Audio)",
        "uploader": "Draxxxy - Topic",
        "duration": 134,
        "id": "draxxxy_vid"
    }
    pen_legit = compute_candidate_penalty(
        candidate=legit_cand,
        custom_artist="Draxxxy",
        custom_title="Clubb",
        expected_duration=134,
        is_apple_music=True
    )
    assert pen_legit < 0.0, "Legitimate candidate must have low penalty"

    # Verify safe failure when YouTube returns 0 candidates
    class EmptyYDL:
        def __init__(self, opts=None):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            return {"entries": []}

    with patch("yt_dlp.YoutubeDL", side_effect=EmptyYDL):
        with pytest.raises(ValueError, match="Трек не найден"):
            _sync_download(
                query_or_url="ytsearch5:Draxxxy - Clubb",
                output_dir=tmp_path,
                custom_title="Clubb",
                custom_artist="Draxxxy",
                expected_duration=134,
                is_apple_music=True
            )


@pytest.mark.asyncio
async def test_vk_music_ru_domain_and_token_regression():
    """
    Regression test for VK URL https://vk.ru/audio-2001878815_33878815:
    1. extract_vk_audio_id parses owner_id and audio_id from vk.ru domain
    2. Without VK_TOKEN, raises ValueError explaining metadata token requirement
    3. With VK_TOKEN, calls VK API, resolves artist/title/duration, sets ytsearch5 target
    4. Any malicious 'url' in VK API response is strictly ignored
    """
    url = "https://vk.ru/audio-2001878815_33878815"
    assert extract_vk_audio_id(url) == ("-2001878815", "33878815")

    # 1. No token -> clear error message about metadata requirement
    with patch.object(config, "VK_TOKEN", None):
        with pytest.raises(ValueError, match="требуется указание VK_TOKEN"):
            await resolve_vk_music_track(url)

    # 2. With token -> metadata resolved, routes to ytsearch5, ignores VK url
    fake_vk_resp = {
        "response": [
            {
                "id": 33878815,
                "owner_id": -2001878815,
                "artist": "Specific Artist",
                "title": "Specific Track",
                "duration": 180,
                "url": "http://169.254.169.254/latest/meta-data/"
            }
        ]
    }

    def fake_sync_http(url, headers=None, proxy=None, timeout=8.0):
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_vk_resp
        return mock_resp

    with patch.object(config, "VK_TOKEN", "mock_token"), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http):
        res = await resolve_vk_music_track(url)
        assert res["platform"] == "VK Music"
        assert res["title"] == "Specific Track"
        assert res["artist"] == "Specific Artist"
        assert res["duration"] == 180
        assert res["target"] == "ytsearch5:Specific Artist - Specific Track"
        assert res["is_search"] is True
        assert "url" not in res
        assert "169.254" not in res["target"]


# ============================================================================
# Regression Tests A - F: Yandex Music Robust Resolution & Bounded Timeout
# ============================================================================

FAKE_NOKTU_META = {
    "result": [
        {
            "id": "143075895",
            "title": "Кайф",
            "available": True,
            "durationMs": 127070,
            "artists": [{"name": "НОКТУ"}],
            "albums": [{"id": 38283718, "title": "Кайф"}],
            "coverUri": "avatars.yandex.net/get-music-content/15018579/fa898c10.a.38283718-2/%%",
            "ogImage": "avatars.yandex.net/get-music-content/15018579/fa898c10.a.38283718-2/%%"
        }
    ]
}


@pytest.mark.asyncio
async def test_yandex_music_regression_test_a_exact_url():
    """
    Test A — exact URL:
    https://music.yandex.ru/album/38283718/track/143075895?utm_medium=copy_link&ref_id=371e271f-356d-48ad-9366-263458700d30
    Проверить:
    platform = Yandex Music
    album_id = 38283718
    track_id = 143075895
    и успешный TrackInfo.
    """
    url = "https://music.yandex.ru/album/38283718/track/143075895?utm_medium=copy_link&ref_id=371e271f-356d-48ad-9366-263458700d30"

    assert extract_yandex_track_id(url) == "143075895"
    assert extract_yandex_album_id(url) == "38283718"
    assert normalize_yandex_url(url) == "https://music.yandex.ru/album/38283718/track/143075895"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = FAKE_NOKTU_META

    with patch("services.yandex_vk._sync_http_request", return_value=mock_resp):
        res = await resolve_yandex_music_track(url)
        assert res["platform"] == "Yandex Music"
        assert res["album_id"] == "38283718"
        assert res["track_id"] == "143075895"
        assert res["title"] == "Кайф"
        assert res["artist"] == "НОКТУ"

        track_info = await resolve_track_url(url)
        assert track_info.platform == "Yandex Music"
        assert track_info.album_id == "38283718"
        assert track_info.track_id == "143075895"
        assert track_info.title == "Кайф"
        assert track_info.artist == "НОКТУ"
        assert track_info.duration == 127
        assert track_info.thumbnail_url == "https://avatars.yandex.net/get-music-content/15018579/fa898c10.a.38283718-2/600x600"
        assert track_info.target == "ytsearch5:НОКТУ - Кайф"


@pytest.mark.asyncio
async def test_yandex_music_regression_test_b_url_without_query():
    """
    Test B — URL без query:
    Проверить тот же track без ?utm...
    """
    url = "https://music.yandex.ru/album/38283718/track/143075895"

    assert extract_yandex_track_id(url) == "143075895"
    assert extract_yandex_album_id(url) == "38283718"
    assert normalize_yandex_url(url) == "https://music.yandex.ru/album/38283718/track/143075895"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = FAKE_NOKTU_META

    with patch("services.yandex_vk._sync_http_request", return_value=mock_resp):
        track_info = await resolve_track_url(url)
        assert track_info.platform == "Yandex Music"
        assert track_info.album_id == "38283718"
        assert track_info.track_id == "143075895"
        assert track_info.title == "Кайф"
        assert track_info.artist == "НОКТУ"


@pytest.mark.asyncio
async def test_yandex_music_regression_test_c_arbitrary_query_parameters():
    """
    Test C — query parameters:
    Проверить, что произвольные tracking-параметры не ломают parsing.
    """
    url = "https://music.yandex.ru/album/38283718/track/143075895?from=button&ref=feed&analytics_id=abcdef123&foo=bar"

    assert extract_yandex_track_id(url) == "143075895"
    assert extract_yandex_album_id(url) == "38283718"
    assert normalize_yandex_url(url) == "https://music.yandex.ru/album/38283718/track/143075895"
    assert normalize_cache_key(url) == "yandex:143075895"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = FAKE_NOKTU_META

    with patch("services.yandex_vk._sync_http_request", return_value=mock_resp):
        track_info = await resolve_track_url(url)
        assert track_info.platform == "Yandex Music"
        assert track_info.album_id == "38283718"
        assert track_info.track_id == "143075895"


@pytest.mark.asyncio
async def test_yandex_music_regression_test_d_metadata_result():
    """
    Test D — metadata result:
    Проверить, что после Yandex resolution формируются:
    artist, title, duration, thumbnail_url, target.
    Также проверяет OpenGraph HTML fallback при недоступности JSON API.
    """
    url = "https://music.yandex.ru/album/38283718/track/143075895"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = FAKE_NOKTU_META

    with patch("services.yandex_vk._sync_http_request", return_value=mock_resp):
        res = await resolve_yandex_music_track(url)
        assert res["artist"] == "НОКТУ"
        assert res["title"] == "Кайф"
        assert res["duration"] == 127
        assert res["thumbnail_url"] == "https://avatars.yandex.net/get-music-content/15018579/fa898c10.a.38283718-2/600x600"
        assert res["target"] == "ytsearch5:НОКТУ - Кайф"

    # Проверка OpenGraph HTML fallback
    html_sample = (
        '<html><head>'
        '<meta property="og:title" content="Кайф">'
        '<meta property="og:description" content="НОКТУ • Трек • 2025">'
        '<meta property="og:image" content="https://avatars.yandex.net/get-music-content/15018579/fa898c10.a.38283718-2/m1000x1000">'
        '</head><body></body></html>'
    )
    def fake_sync_http_og(u, headers=None, proxy=None, timeout=None):
        if "api.music.yandex.net" in u:
            raise requests.exceptions.ConnectionError("API blocked")
        mock_html = MagicMock()
        mock_html.status_code = 200
        mock_html.text = html_sample
        return mock_html

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_sync_http_og):
        res_og = await resolve_yandex_music_track(url)
        assert res_og["artist"] == "НОКТУ"
        assert res_og["title"] == "Кайф"
        assert res_og["thumbnail_url"] == "https://avatars.yandex.net/get-music-content/15018579/fa898c10.a.38283718-2/m1000x1000"
        assert res_og["target"] == "ytsearch5:НОКТУ - Кайф"


@pytest.mark.asyncio
async def test_yandex_music_regression_test_e_timeout_bounding():
    """
    Test E — Yandex request timeout:
    Смоделировать зависший HTTP request.
    Проверить, что функция завершается controlled timeout, а не висит бесконечно.
    """
    import time
    url = "https://music.yandex.ru/album/38283718/track/143075895"

    def hanging_sync_http(u, headers=None, proxy=None, timeout=None):
        time.sleep(2.0)
        return MagicMock()

    async def fake_wait_for(fut, timeout):
        if hasattr(fut, "close"):
            fut.close()
        raise asyncio.TimeoutError()

    with patch("services.yandex_vk._sync_http_request", side_effect=hanging_sync_http), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        with pytest.raises(ValueError, match="Время ожидания ответа от Яндекс Музыки истекло"):
            await resolve_yandex_music_track(url)


@pytest.mark.asyncio
async def test_yandex_music_regression_test_f_metadata_failure_and_fallback():
    """
    Test F — Yandex metadata failure:
    1. Прокси падает с ошибкой/таймаутом -> авто-переход на прямое подключение -> успех.
    2. Полный сбой всех каналов -> понятное исключение ValueError.
    """
    url = "https://music.yandex.ru/album/38283718/track/143075895"

    # 1. Сбой прокси, успех прямого запроса
    direct_called = False
    def fake_proxy_fail_then_direct(u, headers=None, proxy=None, timeout=None):
        nonlocal direct_called
        if proxy is not None:
            raise requests.exceptions.ConnectTimeout("Proxy unreachable")
        direct_called = True
        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.json.return_value = FAKE_NOKTU_META
        return mock_r

    with patch("services.yandex_vk.get_proxy_for_source", return_value="socks5://127.0.0.1:10809"), \
         patch("services.yandex_vk._sync_http_request", side_effect=fake_proxy_fail_then_direct):
        res = await resolve_yandex_music_track(url)
        assert direct_called is True
        assert res["title"] == "Кайф"
        assert res["artist"] == "НОКТУ"

    # 2. Полный сбой (API 404 / 500 и HTML недоступен)
    def fake_complete_fail(u, headers=None, proxy=None, timeout=None):
        raise requests.exceptions.HTTPError("404 Not Found")

    with patch("services.yandex_vk._sync_http_request", side_effect=fake_complete_fail):
        with pytest.raises(ValueError, match="Не удалось связаться с сервером Яндекс Музыки"):
            await resolve_yandex_music_track(url)
