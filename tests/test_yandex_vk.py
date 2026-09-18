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
        with pytest.raises(ValueError, match="требует указания VK_TOKEN"):
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
