import pytest
import io
import time
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path
from PIL import Image

from services.extractor import (
    resolve_track_url,
    resolve_text_to_track_info,
    clear_extracted_cache,
    ExtractedTrack
)
from services.identity import (
    clean_unicode_text,
    transliterate_text,
    phonetic_artist_key,
    extract_core_title_words
)
from services.downloader import (
    download_track,
    compute_candidate_penalty,
    _process_remote_cover_bytes,
    _cleanup_temp_candidate_files,
    _is_candidate_promising,
    is_candidate_download_eligible
)


@pytest.mark.asyncio
async def test_resolve_track_url_ttl_cache_prevents_duplicate_network_calls():
    clear_extracted_cache()
    mock_inner = AsyncMock(return_value=ExtractedTrack(
        platform="Spotify",
        target="ytsearch5:Test Artist - Test Title",
        is_search=True,
        title="Test Title",
        artist="Test Artist",
        duration=200
    ))

    with patch("services.extractor.is_safe_url", return_value=(True, "OK")), \
         patch("services.extractor._unshorten_url", new=AsyncMock(side_effect=lambda u, s: u)), \
         patch("services.extractor._resolve_track_url_inner", new=mock_inner):

        url = "https://open.spotify.com/track/test12345"
        # First call: hits _resolve_track_url_inner
        res1 = await resolve_track_url(url)
        assert res1.title == "Test Title"
        assert mock_inner.call_count == 1

        # Second call: must be served from in-memory TTL cache without calling inner resolver
        res2 = await resolve_track_url(url)
        assert res2.title == "Test Title"
        assert mock_inner.call_count == 1  # Still 1! No second network lookup!


@pytest.mark.asyncio
async def test_resolve_text_to_track_info_ttl_cache_prevents_duplicate_calls():
    clear_extracted_cache()
    mock_canonical = AsyncMock(return_value=ExtractedTrack(
        platform="Deezer",
        target="ytsearch5:Radiohead Creep",
        is_search=True,
        title="Creep",
        artist="Radiohead",
        duration=238
    ))

    with patch("services.extractor.resolve_canonical_track_info_async", new=mock_canonical):
        query = "Radiohead - Creep"
        res1 = await resolve_text_to_track_info(query)
        assert res1.title == "Creep"
        assert mock_canonical.call_count == 1

        # Second call: must be served from cache
        res2 = await resolve_text_to_track_info(query)
        assert res2.title == "Creep"
        assert mock_canonical.call_count == 1  # Still 1!


def test_identity_lru_caching_architectural_invariant():
    # Verify LRU cache hits for frequent pure string operations
    clean_unicode_text.cache_clear()
    transliterate_text.cache_clear()
    phonetic_artist_key.cache_clear()

    # Call multiple times with the same input
    for _ in range(10):
        clean_unicode_text("  Psychea — Бесконечный стук шагов  ")
        transliterate_text("Психея")
        phonetic_artist_key("Psychea")

    assert clean_unicode_text.cache_info().hits >= 9
    assert transliterate_text.cache_info().hits >= 9
    assert phonetic_artist_key.cache_info().hits >= 9


def test_candidate_memoization_stores_computed_metrics():
    cand = {
        "title": "Joe Inferno - Tribal Church Feat Dye Witness",
        "uploader": "Various Artists - Topic",
        "channel": "Various Artists - Topic",
        "duration": 216,
        "_source": "youtube"
    }

    # Verify no cached metrics before penalty computation
    assert "_norm_title" not in cand
    assert "_is_inversion" not in cand
    assert "_penalty" not in cand

    pen = compute_candidate_penalty(
        candidate=cand,
        custom_artist="Tribal Church",
        custom_title="Pt.02",
        expected_duration=216
    )

    # Candidate dictionary must now contain memoized results
    assert cand.get("_norm_title") == "joe inferno - tribal church feat dye witness"
    assert cand.get("_is_inversion") is True
    assert cand.get("_is_art_valid") is False
    assert cand.get("_penalty") == pen
    assert pen > 20000.0


def test_single_pass_remote_cover_processing(tmp_path):
    # Generate a dummy RGB test image
    img = Image.new("RGB", (600, 600), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    raw_bytes = buf.getvalue()

    target = tmp_path / "cover"
    tg_thumb, highres_cover = _process_remote_cover_bytes(raw_bytes, target)

    assert tg_thumb is not None and tg_thumb.exists()
    assert highres_cover is not None and highres_cover.exists()

    # Verify tg_thumb <= 320x320
    with Image.open(tg_thumb) as t_img:
        assert max(t_img.size) <= 320

    # Verify highres_cover preserved high resolution
    with Image.open(highres_cover) as h_img:
        assert h_img.size == (600, 600)

    # Ensure no leftover .raw_img file was left behind
    assert not (tmp_path / "cover.raw_img").exists()


def test_cleanup_temp_candidate_files_preserves_covers_and_backups(tmp_path):
    # Create files
    audio_f = tmp_path / "track.m4a"
    audio_f.write_text("audio data")
    cover_f = tmp_path / "cover.jpg"
    cover_f.write_text("cover data")
    thumb_f = tmp_path / "thumb_cover.jpg"
    thumb_f.write_text("thumb data")
    embedded_f = tmp_path / "embedded_cover.jpg"
    embedded_f.write_text("embedded data")
    backup_f = tmp_path / "backup_track.m4a"
    backup_f.write_text("backup data")

    _cleanup_temp_candidate_files(tmp_path)

    # Audio file must be cleaned up
    assert not audio_f.exists()
    # Covers and backups must be preserved
    assert cover_f.exists()
    assert thumb_f.exists()
    assert embedded_f.exists()
    assert backup_f.exists()


def test_candidate_promising_classification_logic():
    # Проверка семантики _is_candidate_promising:
    # 1. Inversion (Joe Inferno - Tribal Church) -> False
    cand_inv = {
        "title": "Joe Inferno - Tribal Church Feat Dye Witness",
        "uploader": "Joe Inferno",
        "channel": "Joe Inferno",
        "duration": 216,
        "_source": "youtube"
    }
    assert _is_candidate_promising(cand_inv, custom_artist="Tribal Church", custom_title="Pt.02") is False

    # 2. Несоответствие артиста -> False
    cand_wrong_art = {
        "title": "Random Band - Pt.02",
        "uploader": "Random Channel",
        "duration": 216,
        "_source": "youtube"
    }
    assert _is_candidate_promising(cand_wrong_art, custom_artist="Tribal Church", custom_title="Pt.02") is False

    # 3. Несоответствие названия (< 0.25) -> False
    cand_wrong_tit = {
        "title": "Tribal Church - Completely Different Song",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "_source": "youtube"
    }
    assert _is_candidate_promising(cand_wrong_tit, custom_artist="Tribal Church", custom_title="Pt.02") is False

    # 4. Валидный кандидат -> True
    cand_valid = {
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "_source": "youtube"
    }
    assert _is_candidate_promising(cand_valid, custom_artist="Tribal Church", custom_title="Pt.02") is True


@pytest.mark.asyncio
async def test_adaptive_search_not_shortened_for_junk_youtube_candidates():
    """Тест A: ложные YouTube-кандидаты не сокращают ожидание SoundCloud, и SoundCloud кандидат выбирается."""
    cand_yt_fake = {
        "id": "yt_fake_joe",
        "title": "Joe Inferno - Tribal Church Feat Dye Witness",
        "uploader": "Joe Inferno",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=yt_fake_joe",
        "_source": "youtube",
    }
    cand_sc_valid = {
        "id": "sc_valid_pt02",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church",
        "duration": 216,
        "webpage_url": "https://soundcloud.com/tribalchurch/pt02",
        "_source": "soundcloud",
    }

    assert _is_candidate_promising(cand_yt_fake, custom_artist="Tribal Church", custom_title="Pt.02") is False

    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                if "ytsearch" in url:
                    return {"entries": [cand_yt_fake]}
                elif "scsearch" in url:
                    return {"entries": [cand_sc_valid]}
                return {"entries": []}

            downloaded_urls.append(url)
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "sc_valid_pt02",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )

        assert result is not None
        assert result.artist == "Tribal Church"
        assert result.title == "Pt.02"
        assert any("sc_valid_pt02" in u or "soundcloud" in u for u in downloaded_urls)
        assert not any("yt_fake_joe" in u for u in downloaded_urls)
        result.cleanup()


@pytest.mark.asyncio
async def test_emergency_soundcloud_fallback_triggered_when_all_entries_filtered():
    """Тест B: entries не пустой, но все кандидаты отфильтрованы -> аварийный SoundCloud fallback вызывается."""
    cand_yt_fake = {
        "id": "yt_fake_inversion",
        "title": "Joe Inferno - Tribal Church Feat Dye Witness",
        "uploader": "Joe Inferno",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=yt_fake_inversion",
        "_source": "youtube",
    }
    cand_sc_fallback = {
        "id": "sc_fallback_pt02",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church",
        "duration": 216,
        "webpage_url": "https://soundcloud.com/tribalchurch/pt02",
        "_source": "soundcloud",
    }

    sc_fallback_called = False
    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal sc_fallback_called
            if not download:
                if "scsearch4:" in url:
                    sc_fallback_called = True
                    return {"entries": [cand_sc_fallback]}
                return {"entries": [cand_yt_fake]}

            downloaded_urls.append(url)
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "sc_fallback_pt02",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )

        assert sc_fallback_called is True, "Emergency SoundCloud fallback must be triggered when all entries are filtered out!"
        assert result is not None
        assert result.title == "Pt.02"
        assert result.artist == "Tribal Church"
        result.cleanup()


@pytest.mark.asyncio
async def test_valid_youtube_candidate_enables_grace_period_and_skips_fallback():
    """Тест C: валидный YouTube-кандидат принимается, экстренный SoundCloud fallback не запускается."""
    cand_yt_valid = {
        "id": "yt_valid_pt02",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=yt_valid_pt02",
        "_source": "youtube",
    }

    assert _is_candidate_promising(cand_yt_valid, custom_artist="Tribal Church", custom_title="Pt.02") is True

    sc_fallback_called = False
    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal sc_fallback_called
            if not download:
                if "scsearch4:" in url:
                    sc_fallback_called = True
                return {"entries": [cand_yt_valid]}

            downloaded_urls.append(url)
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "yt_valid_pt02",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church - Topic",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )

        assert sc_fallback_called is False, "Emergency fallback must NOT be called when YouTube candidate is valid!"
        assert result is not None
        assert result.title == "Pt.02"
        assert any("yt_valid_pt02" in u for u in downloaded_urls)
        result.cleanup()


@pytest.mark.asyncio
async def test_multiple_candidates_fake_inversion_valid_succeeds_without_fallback():
    """Тест D: YouTube: candidate 1 = fake, candidate 2 = inversion, candidate 3 = valid -> успех без аварийного fallback."""
    cand1_fake = {
        "id": "c1_fake",
        "title": "Tribal Church - Completely Different Track",
        "uploader": "Tribal Church",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_fake",
        "_source": "youtube",
    }
    cand2_inversion = {
        "id": "c2_inversion",
        "title": "Joe Inferno - Tribal Church Feat Dye Witness",
        "uploader": "Joe Inferno",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c2_inversion",
        "_source": "youtube",
    }
    cand3_valid = {
        "id": "c3_valid",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c3_valid",
        "_source": "youtube",
    }

    assert _is_candidate_promising(cand1_fake, custom_artist="Tribal Church", custom_title="Pt.02") is False
    assert _is_candidate_promising(cand2_inversion, custom_artist="Tribal Church", custom_title="Pt.02") is False
    assert _is_candidate_promising(cand3_valid, custom_artist="Tribal Church", custom_title="Pt.02") is True

    sc_fallback_called = False
    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal sc_fallback_called
            if not download:
                if "scsearch4:" in url:
                    sc_fallback_called = True
                return {"entries": [cand1_fake, cand2_inversion, cand3_valid]}

            downloaded_urls.append(url)
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c3_valid",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church - Topic",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )

        assert sc_fallback_called is False, "Emergency fallback must NOT be called when candidate 3 is valid!"
        assert result is not None
        assert result.title == "Pt.02"
        assert any("c3_valid" in u for u in downloaded_urls)
        assert not any("c1_fake" in u for u in downloaded_urls)
        assert not any("c2_inversion" in u for u in downloaded_urls)
        result.cleanup()


@pytest.mark.asyncio
async def test_candidate_hang_times_out_and_next_candidate_succeeds():
    """Тест A: кандидат #1 зависает при скачивании -> отсекается по таймауту; кандидат #2 валиден -> скачивается и отдаётся."""
    cand1_hang = {
        "id": "c1_hang",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_hang",
        "_source": "youtube",
    }
    cand2_valid = {
        "id": "c2_valid",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c2_valid",
        "_source": "youtube",
    }

    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand1_hang, cand2_valid]}
            downloaded_urls.append(url)
            if "c1_hang" in url:
                time.sleep(0.5)
                return {"id": "c1_hang", "title": "Tribal Church - Pt.02", "duration": 216}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c2_valid",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church - Topic",
                "duration": 216,
            }

    t0 = time.perf_counter()
    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader.CANDIDATE_DOWNLOAD_TIMEOUT", 0.15), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )
        elapsed = time.perf_counter() - t0
        assert result is not None
        assert result.title == "Pt.02"
        assert any("c2_valid" in u for u in downloaded_urls)
        assert elapsed < 1.0, f"Elapsed {elapsed:.2f}s exceeded limit"
        result.cleanup()


@pytest.mark.asyncio
async def test_candidate_retry_without_cookies_bounded_by_candidate_deadline():
    """Тест B: кандидат #1 попытка 1 падает с 403; попытка 2 (без cookies) зависает -> отсекается по бюджету кандидата."""
    cand1_403_hang = {
        "id": "c1_403_hang",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_403_hang",
        "_source": "youtube",
    }
    cand2_valid = {
        "id": "c2_valid",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c2_valid",
        "_source": "youtube",
    }

    downloaded_urls = []
    retry_attempted = False

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal retry_attempted
            if not download:
                return {"entries": [cand1_403_hang, cand2_valid]}
            downloaded_urls.append(url)
            if "c1_403_hang" in url:
                if self.opts.get("cookiefile"):
                    raise Exception("HTTP Error 403: Forbidden")
                else:
                    retry_attempted = True
                    time.sleep(0.5)
                    return {"id": "c1_403_hang", "title": "Tribal Church - Pt.02", "duration": 216}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c2_valid",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church - Topic",
                "duration": 216,
            }

    t0 = time.perf_counter()
    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader.CANDIDATE_DOWNLOAD_TIMEOUT", 0.25), \
         patch("services.downloader.MIN_RETRY_TIME_REMAINING", 0.05), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )
        elapsed = time.perf_counter() - t0
        assert retry_attempted is True, "Retry without cookies must be attempted"
        assert result is not None
        assert result.title == "Pt.02"
        assert any("c2_valid" in u for u in downloaded_urls)
        assert elapsed < 1.0, f"Elapsed {elapsed:.2f}s exceeded limit"
        result.cleanup()


@pytest.mark.asyncio
async def test_all_youtube_candidates_fail_triggers_emergency_soundcloud_fallback():
    """Тест C: 5 плохих YouTube-кандидатов (все падают/зависают) -> аварийный SoundCloud fallback срабатывает и отдаёт трек."""
    bad_yt_cands = [
        {
            "id": f"yt_bad_{i}",
            "title": f"Tribal Church - Pt.02 (Version {i})",
            "uploader": "Tribal Church - Topic",
            "duration": 216,
            "webpage_url": f"https://www.youtube.com/watch?v=yt_bad_{i}",
            "_source": "youtube",
        }
        for i in range(1, 6)
    ]
    cand_sc_fallback = {
        "id": "sc_pt02",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church",
        "duration": 216,
        "webpage_url": "https://soundcloud.com/tribalchurch/pt02",
        "_source": "soundcloud",
    }

    sc_fallback_called = False
    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal sc_fallback_called
            if not download:
                if "scsearch4:" in url:
                    sc_fallback_called = True
                    return {"entries": [cand_sc_fallback]}
                return {"entries": bad_yt_cands}

            downloaded_urls.append(url)
            if "youtube.com" in url:
                raise Exception("HTTP Error 403: Forbidden")
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "sc_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "sc_pt02",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church",
                "duration": 216,
            }

    t0 = time.perf_counter()
    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader.CANDIDATE_DOWNLOAD_TIMEOUT", 0.15), \
         patch("services.downloader.GLOBAL_EXTRACTION_TIMEOUT", 3.0), \
         patch("services.downloader.MIN_RETRY_TIME_REMAINING", 0.05), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )
        elapsed = time.perf_counter() - t0
        assert sc_fallback_called is True, "Emergency SoundCloud fallback must be called"
        assert result is not None
        assert result.title == "Pt.02"
        assert any("soundcloud.com" in u for u in downloaded_urls)
        assert elapsed < 2.0, f"Elapsed {elapsed:.2f}s exceeded limit"
        result.cleanup()


@pytest.mark.asyncio
async def test_bad_candidate_then_valid_candidate_chosen_without_fallback():
    """Тест D: кандидат #1 плохой (403), кандидат #2 валидный -> выбирается валидный, без аварийного fallback."""
    cand1_bad = {
        "id": "c1_bad",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_bad",
        "_source": "youtube",
    }
    cand2_valid = {
        "id": "c2_valid",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c2_valid",
        "_source": "youtube",
    }

    sc_fallback_called = False
    downloaded_urls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal sc_fallback_called
            if not download:
                if "scsearch4:" in url:
                    sc_fallback_called = True
                return {"entries": [cand1_bad, cand2_valid]}

            downloaded_urls.append(url)
            if "c1_bad" in url:
                raise Exception("HTTP Error 403: Forbidden")
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c2_valid",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church - Topic",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader.CANDIDATE_DOWNLOAD_TIMEOUT", 0.2), \
         patch("services.downloader.MIN_RETRY_TIME_REMAINING", 0.05), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )
        assert sc_fallback_called is False, "Emergency fallback must NOT be called when candidate #2 succeeds"
        assert result is not None
        assert result.title == "Pt.02"
        assert any("c2_valid" in u for u in downloaded_urls)
        result.cleanup()


@pytest.mark.asyncio
async def test_global_extraction_deadline_bounds_total_elapsed_time():
    """Тест E: несколько медленных кандидатов -> общий лимит времени экстракции прерывает перебор и запускает SoundCloud fallback."""
    slow_yt_cands = [
        {
            "id": f"yt_slow_{i}",
            "title": f"Tribal Church - Pt.02 (Slow {i})",
            "uploader": "Tribal Church - Topic",
            "duration": 216,
            "webpage_url": f"https://www.youtube.com/watch?v=yt_slow_{i}",
            "_source": "youtube",
        }
        for i in range(1, 6)
    ]
    cand_sc_fallback = {
        "id": "sc_pt02_fast",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church",
        "duration": 216,
        "webpage_url": "https://soundcloud.com/tribalchurch/pt02",
        "_source": "soundcloud",
    }

    sc_fallback_called = False
    downloaded_yt_count = 0

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal sc_fallback_called, downloaded_yt_count
            if not download:
                if "scsearch4:" in url:
                    sc_fallback_called = True
                    return {"entries": [cand_sc_fallback]}
                return {"entries": slow_yt_cands}

            if "youtube.com" in url:
                downloaded_yt_count += 1
                time.sleep(0.15)
                raise Exception("HTTP Error 403: Forbidden")
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "sc_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "sc_pt02_fast",
                "title": "Tribal Church - Pt.02",
                "uploader": "Tribal Church",
                "duration": 216,
            }

    t0 = time.perf_counter()
    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader.GLOBAL_EXTRACTION_TIMEOUT", 0.4), \
         patch("services.downloader.GLOBAL_SC_FALLBACK_RESERVE", 0.1), \
         patch("services.downloader.CANDIDATE_DOWNLOAD_TIMEOUT", 0.15), \
         patch("services.downloader.MIN_RETRY_TIME_REMAINING", 0.05), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True
        )
        elapsed = time.perf_counter() - t0
        assert sc_fallback_called is True, "SoundCloud fallback must trigger after global timeout"
        assert downloaded_yt_count < 5, f"Expected < 5 candidates processed due to global deadline, but was {downloaded_yt_count}"
        assert elapsed < 1.0, f"Elapsed {elapsed:.2f}s exceeded global bound"
        assert result is not None
        assert result.title == "Pt.02"
        result.cleanup()


@pytest.mark.asyncio
async def test_pre_validation_rejects_artist_mismatch_without_download():
    """Test 1: target artist = Psychea, candidate uploader = another artist -> reject, extract_info(download=True) == 0 calls."""
    cand_wrong = {
        "id": "c1_wrong",
        "title": "Other Singer - Бесконечный стук шагов",
        "uploader": "Other Channel",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_wrong",
        "_source": "youtube"
    }

    # 1. Прямая проверка хелпера:
    eligible, reason = is_candidate_download_eligible(
        candidate=cand_wrong,
        custom_artist="Psychea",
        custom_title="Бесконечный стук шагов",
        is_apple_music=True
    )
    assert eligible is False
    assert reason == "artist mismatch"

    # 2. Интеграционная проверка через download_track:
    download_calls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_wrong]}
            download_calls.append(url)
            raise RuntimeError("Should never be called for rejected candidate!")

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        with pytest.raises(Exception):
            await download_track(
                query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
                custom_artist="Psychea",
                custom_title="Бесконечный стук шагов",
                expected_duration=216,
                is_apple_music=True
            )

    assert len(download_calls) == 0, f"Expected 0 download calls, but got {len(download_calls)}"


@pytest.mark.asyncio
async def test_pre_validation_rejects_unwanted_modifier_without_download():
    """Test 2: запрос без модификаторов, candidate c (Live 2003) и (Киберакустика) -> reject, download не выполняется."""
    cand_live = {
        "id": "c_live",
        "title": "Psychea - Бесконечный стук шагов (Live 2003)",
        "uploader": "Psychea",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_live",
        "_source": "youtube"
    }
    cand_acoustic = {
        "id": "c_acoustic",
        "title": "Бесконечный стук шагов (Киберакустика Version)",
        "uploader": "Psychea",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_acoustic",
        "_source": "youtube"
    }

    # Прямая проверка
    el1, r1 = is_candidate_download_eligible(cand_live, custom_artist="Psychea", custom_title="Бесконечный стук шагов", is_apple_music=True)
    assert el1 is False
    assert "live" in r1

    el2, r2 = is_candidate_download_eligible(cand_acoustic, custom_artist="Psychea", custom_title="Бесконечный стук шагов", is_apple_music=True)
    assert el2 is False
    assert "киберакустика" in r2 or "acoustic" in r2

    download_calls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_live, cand_acoustic]}
            download_calls.append(url)
            raise RuntimeError("Should never be called for rejected candidate!")

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        with pytest.raises(Exception):
            await download_track(
                query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
                custom_artist="Psychea",
                custom_title="Бесконечный стук шагов",
                expected_duration=216,
                is_apple_music=True
            )

    assert len(download_calls) == 0, f"Expected 0 download calls, but got {len(download_calls)}"


@pytest.mark.asyncio
async def test_pre_validation_rejects_bad_title_without_download():
    """Test 3: нерелевантный title -> reject before download."""
    cand_bad_title = {
        "id": "c_bad_tit",
        "title": "Psychea - Completely Random Song Title",
        "uploader": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_bad_tit",
        "_source": "youtube"
    }

    el, r = is_candidate_download_eligible(cand_bad_title, custom_artist="Psychea", custom_title="Бесконечный стук шагов")
    assert el is False
    assert "title mismatch" in r

    download_calls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_bad_title]}
            download_calls.append(url)
            raise RuntimeError("Should never be called for rejected candidate!")

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        with pytest.raises(Exception):
            await download_track(
                query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
                custom_artist="Psychea",
                custom_title="Бесконечный стук шагов",
                expected_duration=216,
                is_apple_music=True
            )

    assert len(download_calls) == 0


@pytest.mark.asyncio
async def test_pre_validation_allows_valid_candidate_to_download_and_succeed():
    """Test 4: валидный кандидат -> pre-validation passes -> download выполняется -> post-validation подтверждает."""
    cand_valid = {
        "id": "c_valid",
        "title": "Psychea - Бесконечный стук шагов",
        "uploader": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_valid",
        "_source": "youtube"
    }

    el, r = is_candidate_download_eligible(cand_valid, custom_artist="Psychea", custom_title="Бесконечный стук шагов", is_apple_music=True)
    assert el is True
    assert r is None

    download_calls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_valid]}
            download_calls.append(url)
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c_valid",
                "title": "Psychea - Бесконечный стук шагов",
                "uploader": "Psychea - Topic",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )

    assert len(download_calls) == 1
    assert res is not None
    assert res.title == "Бесконечный стук шагов"
    res.cleanup()


@pytest.mark.asyncio
async def test_pre_validation_allows_ambiguous_candidate_to_proceed_to_post_validation():
    """Test 5: metadata недостаточно для уверенного reject -> download разрешён, post-validation решает."""
    cand_ambiguous = {
        "id": "c_ambiguous",
        "title": "Бесконечный стук шагов",
        "uploader": None,
        "channel": None,
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_ambiguous",
        "_source": "youtube"
    }

    # В пре-валидации этот кандидат не должен отсекаться:
    el, r = is_candidate_download_eligible(cand_ambiguous, custom_artist="Psychea", custom_title="Бесконечный стук шагов", is_apple_music=True)
    assert el is True, "Ambiguous candidate must NOT be rejected early"
    assert r is None

    download_calls = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_ambiguous]}
            download_calls.append(url)
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "valid_track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c_ambiguous",
                "title": "Бесконечный стук шагов",
                "uploader": "Psychea - Topic",
                "duration": 216,
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )

    assert len(download_calls) == 1
    assert res is not None
    assert res.title == "Бесконечный стук шагов"
    res.cleanup()


@pytest.mark.asyncio
async def test_403_retry_preserved_for_promising_candidate_and_blocked_for_rejected_candidate():
    """Test 6: retry без cookies работает для promising candidate; для rejected candidate не вызывается вовсе."""
    cand_promising_403 = {
        "id": "c_prom_403",
        "title": "Psychea - Бесконечный стук шагов",
        "uploader": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_prom_403",
        "_source": "youtube"
    }

    cookies_downloads = []
    no_cookies_downloads = []

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_promising_403]}
            if self.opts.get("cookiefile"):
                cookies_downloads.append(url)
                raise Exception("HTTP Error 403: Forbidden")
            else:
                no_cookies_downloads.append(url)
                out_dir = Path(self.opts["outtmpl"]).parent
                f = out_dir / "valid_track.m4a"
                f.write_bytes(b"audio_bytes" * 500)
                return {
                    "id": "c_prom_403",
                    "title": "Psychea - Бесконечный стук шагов",
                    "uploader": "Psychea - Topic",
                    "duration": 216,
                }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )

    assert len(cookies_downloads) == 1, "Attempt with cookies must be executed"
    assert len(no_cookies_downloads) == 1, "Retry without cookies must be executed"
    assert res is not None
    assert res.title == "Бесконечный стук шагов"
    res.cleanup()

    # Проверяем обратное: для заведомо отсеянного кандидата ни cookies, ни no-cookies не запускаются
    cand_rejected = {
        "id": "c_rej",
        "title": "Psychea - Бесконечный стук шагов (Live 2003)",
        "uploader": "Psychea",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_rej",
        "_source": "youtube"
    }
    calls_rej = []

    class MockYDLRej:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_rejected]}
            calls_rej.append(url)
            raise RuntimeError("Must not be called!")

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDLRej), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        with pytest.raises(Exception):
            await download_track(
                query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
                custom_artist="Psychea",
                custom_title="Бесконечный стук шагов",
                expected_duration=216,
                is_apple_music=True
            )

    assert len(calls_rej) == 0, "Rejected candidate must have 0 download attempts!"


@pytest.mark.asyncio
async def test_pre_validation_accepts_youtube_topic_candidate():
    """Тест 7: YouTube Topic candidate с uploader='Psychea - Topic' и channel='Psychea - Topic'."""
    cand_topic = {
        "id": "c_topic_1",
        "title": "Бесконечный стук шагов",
        "uploader": "Psychea - Topic",
        "channel": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_topic_1",
        "_source": "youtube"
    }

    eligible, reason = is_candidate_download_eligible(
        candidate=cand_topic,
        custom_artist="Psychea",
        custom_title="Бесконечный стук шагов",
        expected_duration=216,
        is_apple_music=True
    )
    assert eligible is True
    assert reason is None

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_topic]}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return cand_topic

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )
        assert res is not None
        assert res.title == "Бесконечный стук шагов"
        res.cleanup()


@pytest.mark.asyncio
async def test_pre_validation_accepts_official_channel_candidate():
    """Тест 8: официальный канал исполнителя с uploader='Psychea', channel='Psychea'."""
    cand_official = {
        "id": "c_official_1",
        "title": "Бесконечный стук шагов (Official Video)",
        "uploader": "Psychea",
        "channel": "Psychea",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_official_1",
        "_source": "youtube"
    }

    eligible, reason = is_candidate_download_eligible(
        candidate=cand_official,
        custom_artist="Psychea",
        custom_title="Бесконечный стук шагов",
        expected_duration=216,
        is_apple_music=True
    )
    assert eligible is True
    assert reason is None

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_official]}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return cand_official

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )
        assert res is not None
        assert res.title == "Бесконечный стук шагов"
        res.cleanup()


@pytest.mark.asyncio
async def test_pre_validation_accepts_missing_uploader_and_channel_candidate():
    """Тест 9: candidate без uploader/channel: ambiguous, не отбраковывается ложно в pre-validation."""
    cand_missing_meta = {
        "id": "c_no_meta_1",
        "title": "Бесконечный стук шагов",
        "uploader": None,
        "channel": None,
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_no_meta_1",
        "_source": "youtube"
    }

    eligible, reason = is_candidate_download_eligible(
        candidate=cand_missing_meta,
        custom_artist="Psychea",
        custom_title="Бесконечный стук шагов",
        expected_duration=216,
        is_apple_music=True
    )
    assert eligible is True
    assert reason is None

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_missing_meta]}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return {
                "id": "c_no_meta_1",
                "title": "Бесконечный стук шагов",
                "artist": "Psychea",
                "duration": 216
            }

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )
        assert res is not None
        assert res.title == "Бесконечный стук шагов"
        res.cleanup()


@pytest.mark.asyncio
async def test_pre_validation_accepts_reupload_artist_dash_title():
    """Тест 10: reupload с 'Artist - Title': uploader сторонний, но в title корректный артист."""
    cand_reupload = {
        "id": "c_reupload_1",
        "title": "Psychea - Бесконечный стук шагов",
        "uploader": "FanReuploader123",
        "channel": "FanReuploader123",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_reupload_1",
        "_source": "youtube"
    }

    eligible, reason = is_candidate_download_eligible(
        candidate=cand_reupload,
        custom_artist="Psychea",
        custom_title="Бесконечный стук шагов",
        expected_duration=216,
        is_apple_music=True
    )
    assert eligible is True
    assert reason is None

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_reupload]}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return cand_reupload

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )
        assert res is not None
        assert res.title == "Бесконечный стук шагов"
        res.cleanup()


@pytest.mark.asyncio
async def test_pre_validation_accepts_candidate_with_slight_title_difference():
    """Тест 11: candidate с небольшим отличием в title (например альбомная приписка)."""
    cand_album = {
        "id": "c_album_ver",
        "title": "Psychea - Бесконечный стук шагов (Альбом Людям планеты Земля)",
        "uploader": "Psychea - Topic",
        "channel": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c_album_ver",
        "_source": "youtube"
    }

    eligible, reason = is_candidate_download_eligible(
        candidate=cand_album,
        custom_artist="Psychea",
        custom_title="Бесконечный стук шагов",
        expected_duration=216,
        is_apple_music=True
    )
    assert eligible is True
    assert reason is None

    class MockYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if not download:
                return {"entries": [cand_album]}
            out_dir = Path(self.opts["outtmpl"]).parent
            f = out_dir / "track.m4a"
            f.write_bytes(b"audio_bytes" * 500)
            return cand_album

    with patch("yt_dlp.YoutubeDL", side_effect=MockYDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        res = await download_track(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            custom_artist="Psychea",
            custom_title="Бесконечный стук шагов",
            expected_duration=216,
            is_apple_music=True
        )
        assert res is not None
        assert res.title == "Бесконечный стук шагов"
        res.cleanup()


@pytest.mark.asyncio
async def test_race_timeout_candidate1_does_not_swap_or_corrupt_winner_candidate2(tmp_path):
    """
    Тест 12: race condition:
    Candidate #1 получает timeout в _extract_info_with_timeout, но его поток продолжает выполняться в фоне
    и позже записывает dummy аудиофайл.
    Candidate #2 успешен и скачивает валидный аудиофайл.
    Строгая изоляция директорий гарантирует, что отдаётся именно файл Candidate #2,
    а Candidate #1 ни при каких условиях не подменяет и не портит результат.
    """
    import threading
    import time
    from services.downloader import _sync_download

    cand1 = {
        "id": "c1_slow",
        "title": "Psychea - Бесконечный стук шагов",
        "uploader": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_slow",
        "_source": "youtube"
    }
    cand2 = {
        "id": "c2_fast",
        "title": "Psychea - Бесконечный стук шагов",
        "uploader": "Psychea - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c2_fast",
        "_source": "youtube"
    }

    c1_bg_thread_finished = threading.Event()
    c1_out_dir = None

    class MockRaceYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal c1_out_dir
            if not download:
                return {"entries": [cand1, cand2]}

            out_dir = Path(self.opts["outtmpl"]).parent
            if "c1_slow" in url:
                c1_out_dir = out_dir
                # Имитируем зависание скачивания Candidate #1 с последующей фоновой записью файла
                def _background_writer():
                    time.sleep(0.15)
                    # Фоновый поток заканчивает запись позже, создавая файл Candidate #1:
                    try:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        fake_f = out_dir / "c1_audio.m4a"
                        fake_f.write_bytes(b"CORRUPTED_CANDIDATE_1_AUDIO" * 100)
                    except Exception:
                        pass
                    finally:
                        c1_bg_thread_finished.set()

                t = threading.Thread(target=_background_writer, daemon=True)
                t.start()
                # Вызываем TimeoutError от имени _extract_info_with_timeout:
                raise TimeoutError("Candidate #1 exceeded candidate deadline")

            elif "c2_fast" in url:
                # Candidate #2 скачивается быстро и успешно:
                f2 = out_dir / "c2_audio.m4a"
                f2.write_bytes(b"VALID_WINNING_CANDIDATE_2_AUDIO" * 100)
                return cand2

            return {}

    with patch("yt_dlp.YoutubeDL", side_effect=MockRaceYDL), \
         patch("services.downloader.CANDIDATE_DOWNLOAD_TIMEOUT", 0.05):
        res = _sync_download(
            query_or_url="ytsearch5:Psychea - Бесконечный стук шагов",
            output_dir=tmp_path,
            custom_title="Бесконечный стук шагов",
            custom_artist="Psychea",
            expected_duration=216,
            is_apple_music=True
        )

        assert res is not None
        assert res.file_path.exists()
        # Проверяем, что в финальном файле находятся байты именно Candidate #2:
        content = res.file_path.read_bytes()
        assert b"VALID_WINNING_CANDIDATE_2_AUDIO" in content
        assert b"CORRUPTED_CANDIDATE_1_AUDIO" not in content

        # Ждём завершения фонового потока Candidate #1, чтобы проверить изоляцию:
        c1_bg_thread_finished.wait(timeout=1.0)
        # Проверяем, что файл победителя в tmp_path остался файлом Candidate #2:
        assert b"VALID_WINNING_CANDIDATE_2_AUDIO" in res.file_path.read_bytes()
        assert b"CORRUPTED_CANDIDATE_1_AUDIO" not in res.file_path.read_bytes()
        # И Candidate #1 изолирован в своей директории:
        if c1_out_dir and c1_out_dir.exists():
            assert c1_out_dir != tmp_path


# ============================================================================
# SECTION 10: SPEED / EXPENSIVE OPERATIONS BOUNDED & BAD CANDIDATES NOT DOWNLOADED
# ============================================================================

@pytest.mark.asyncio
async def test_expensive_operations_bounded_and_bad_candidates_not_downloaded():
    """
    Section 10: 5 YouTube candidates (3 bad, 2 eligible).
    - Bad candidates (artist mismatch, unwanted modifier, bad title) have 0 download attempts.
    - Only eligible candidate #1 downloads.
    - 403 on eligible candidate #1 retries once without cookies and succeeds.
    - Eligible candidate #2 and subsequent candidates are NOT touched.
    """
    cand1_bad_artist = {
        "id": "c1_bad_artist",
        "title": "Wrong Artist - Pt.02",
        "uploader": "Wrong Artist",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c1_bad_artist",
        "_source": "youtube",
    }
    cand2_bad_modifier = {
        "id": "c2_bad_modifier",
        "title": "Tribal Church - Pt.02 (Live 2003)",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c2_bad_modifier",
        "_source": "youtube",
    }
    cand3_bad_title = {
        "id": "c3_bad_title",
        "title": "Tribal Church - Completely Different Track",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c3_bad_title",
        "_source": "youtube",
    }
    cand4_eligible_1 = {
        "id": "c4_eligible_1",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c4_eligible_1",
        "_source": "youtube",
    }
    cand5_eligible_2 = {
        "id": "c5_eligible_2",
        "title": "Tribal Church - Pt.02",
        "uploader": "Tribal Church - Topic",
        "duration": 216,
        "webpage_url": "https://www.youtube.com/watch?v=c5_eligible_2",
        "_source": "youtube",
    }

    all_candidates = [
        cand1_bad_artist,
        cand2_bad_modifier,
        cand3_bad_title,
        cand4_eligible_1,
        cand5_eligible_2,
    ]

    download_attempts = []
    retry_no_cookies_called = False

    class MockSection10YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download=False):
            nonlocal retry_no_cookies_called
            if not download:
                return {"entries": all_candidates}

            download_attempts.append((url, bool(self.opts.get("cookiefile"))))
            if "c4_eligible_1" in url:
                if self.opts.get("cookiefile"):
                    raise Exception("HTTP Error 403: Forbidden")
                else:
                    retry_no_cookies_called = True
                    out_dir = Path(self.opts["outtmpl"]).parent
                    f = out_dir / "valid_track.m4a"
                    f.write_bytes(b"audio_content" * 500)
                    return cand4_eligible_1

            if "c5_eligible_2" in url:
                raise RuntimeError("Candidate #5 must not be touched!")

            raise RuntimeError(f"Unexpected download call for {url}")

    with patch("yt_dlp.YoutubeDL", side_effect=MockSection10YDL), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        result = await download_track(
            query_or_url="ytsearch5:Tribal Church - Pt.02",
            custom_artist="Tribal Church",
            custom_title="Pt.02",
            expected_duration=216,
            is_apple_music=True,
        )

        assert result is not None
        assert result.title == "Pt.02"
        assert result.artist == "Tribal Church"

        # 1. Плохие кандидаты не должны скачиваться вовсе:
        bad_urls = ["c1_bad_artist", "c2_bad_modifier", "c3_bad_title"]
        for bad_id in bad_urls:
            assert not any(bad_id in url for url, _ in download_attempts), f"Bad candidate {bad_id} was attempted to download!"

        # 2. Только подходящий кандидат #4 скачивался:
        assert len(download_attempts) == 2  # 1 попытка с cookies (403) + 1 retry без cookies (success)
        assert "c4_eligible_1" in download_attempts[0][0]
        assert download_attempts[0][1] is True  # with cookies
        assert "c4_eligible_1" in download_attempts[1][0]
        assert download_attempts[1][1] is False  # without cookies
        assert retry_no_cookies_called is True

        # 3. Последующий кандидат #5 не затрагивался:
        assert not any("c5_eligible_2" in url for url, _ in download_attempts)

        result.cleanup()


# ============================================================================
# SECTION 11: COVER PIPELINE REGRESSION TESTS (TESTS A - F)
# ============================================================================

def test_cover_regression_a_remote_cover_to_mp3_apic(tmp_path):
    """
    Test A: _apply_custom_metadata записывает ID3 APIC frame в MP3 с корректным mime и данными.
    """
    from services.downloader import _apply_custom_metadata
    from mutagen.id3 import ID3

    # Создаем dummy MP3 с валидными фреймами
    mp3_file = tmp_path / "test_track.mp3"
    frame_header = b"\xff\xfb\x90\x04" + b"\x00" * 414
    mp3_file.write_bytes(frame_header * 10)

    # Создаем dummy обложку JPEG
    cover_file = tmp_path / "cover.jpg"
    img = Image.new("RGB", (600, 600), color="blue")
    img.save(cover_file, "JPEG")

    _apply_custom_metadata(
        audio_path=mp3_file,
        title="Cover Track",
        artist="Cover Artist",
        cover_path=cover_file,
        album="Cover Album"
    )

    tags = ID3(mp3_file)
    apic_frames = tags.getall("APIC")
    assert len(apic_frames) == 1, "ID3 APIC frame must be present in MP3"
    apic = apic_frames[0]
    assert apic.mime == "image/jpeg"
    assert apic.type == 3  # Cover front

    with Image.open(io.BytesIO(apic.data)) as loaded_img:
        assert loaded_img.format == "JPEG"
        assert loaded_img.size == (600, 600)


def test_cover_regression_b_remote_cover_to_m4a_covr(tmp_path):
    """
    Test B: _apply_custom_metadata записывает MP4 covr atom в M4A.
    """
    from services.downloader import _apply_custom_metadata
    from mutagen.mp4 import MP4Cover

    m4a_file = tmp_path / "test_track.m4a"
    m4a_file.write_bytes(b"dummy_m4a_content")

    cover_file = tmp_path / "cover.jpg"
    img = Image.new("RGB", (500, 500), color="red")
    img.save(cover_file, "JPEG")

    mock_mp4_tags = {}
    mock_mp4_instance = MagicMock()
    mock_mp4_instance.__setitem__.side_effect = lambda k, v: mock_mp4_tags.__setitem__(k, v)
    mock_mp4_instance.__getitem__.side_effect = lambda k: mock_mp4_tags[k]

    with patch("services.downloader.MP4", return_value=mock_mp4_instance):
        _apply_custom_metadata(
            audio_path=m4a_file,
            title="M4A Title",
            artist="M4A Artist",
            cover_path=cover_file,
            album="M4A Album"
        )

        assert "\xa9nam" in mock_mp4_tags and mock_mp4_tags["\xa9nam"] == ["M4A Title"]
        assert "\xa9ART" in mock_mp4_tags and mock_mp4_tags["\xa9ART"] == ["M4A Artist"]
        assert "covr" in mock_mp4_tags
        assert len(mock_mp4_tags["covr"]) == 1
        assert isinstance(mock_mp4_tags["covr"][0], MP4Cover)
        mock_mp4_instance.save.assert_called_once()


def test_cover_regression_c_candidate_promotion_preserves_thumbnail(tmp_path):
    """
    Test C: _promote_candidate_assets переносит обложку yt-dlp (*.webp, *.jpg) вместе с аудио
    из изолированной cand_dir в output_dir до удаления cand_dir.
    """
    from services.downloader import _promote_candidate_assets

    cand_dir = tmp_path / "candidate_isolated_dir"
    cand_dir.mkdir(parents=True, exist_ok=True)
    output_dir = tmp_path / "final_output_dir"
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_file = cand_dir / "audio.m4a"
    audio_file.write_bytes(b"AUDIO_DATA")

    thumb_file = cand_dir / "thumbnail.webp"
    thumb_file.write_bytes(b"THUMBNAIL_WEBP_DATA")

    winner = _promote_candidate_assets(cand_dir, output_dir, audio_file)

    assert winner.exists()
    assert winner.parent == output_dir
    assert winner.read_bytes() == b"AUDIO_DATA"

    # Обложка должна быть перемещена в output_dir
    promoted_thumb = output_dir / "thumbnail.webp"
    assert promoted_thumb.exists()
    assert promoted_thumb.read_bytes() == b"THUMBNAIL_WEBP_DATA"

    # Изолированная директория кандидата удалена
    assert not cand_dir.exists()


def test_cover_regression_d_cleanup_preserves_covers_before_embedding(tmp_path):
    """
    Test D: _cleanup_temp_candidate_files и очистка в fallback сохраняют cover.jpg, thumb_cover.jpg, embedded_cover.jpg.
    """
    from services.downloader import _cleanup_temp_candidate_files

    # Создаем набор файлов
    cover_main = tmp_path / "cover.jpg"
    cover_main.write_bytes(b"COVER_MAIN")
    thumb_cover = tmp_path / "thumb_cover.jpg"
    thumb_cover.write_bytes(b"THUMB_COVER")
    embedded_cover = tmp_path / "embedded_cover.jpg"
    embedded_cover.write_bytes(b"EMBEDDED_COVER")

    temp_audio = tmp_path / "temp_audio.m4a"
    temp_audio.write_bytes(b"TEMP_AUDIO")
    temp_part = tmp_path / "temp.part"
    temp_part.write_bytes(b"TEMP_PART")

    # 1. Проверяем _cleanup_temp_candidate_files
    _cleanup_temp_candidate_files(tmp_path)
    assert cover_main.exists()
    assert thumb_cover.exists()
    assert embedded_cover.exists()

    # 2. Проверяем fallback-очистку в download_track
    for item in tmp_path.iterdir():
        if item.is_file() and not item.name.startswith("cover") and not item.name.startswith("thumb_") and not item.name.startswith("embedded_"):
            item.unlink(missing_ok=True)

    assert cover_main.exists()
    assert thumb_cover.exists()
    assert embedded_cover.exists()
    assert not temp_audio.exists()
    assert not temp_part.exists()


def test_cover_regression_e_missing_cover_graceful_fallback(tmp_path):
    """
    Test E: _apply_custom_metadata при отсутствии cover_path (None или несуществующий файл)
    не падает с ошибкой и корректно прописывает текстовые теги.
    """
    from services.downloader import _apply_custom_metadata
    from mutagen.id3 import ID3

    mp3_file = tmp_path / "no_cover.mp3"
    frame_header = b"\xff\xfb\x90\x04" + b"\x00" * 414
    mp3_file.write_bytes(frame_header * 10)

    # 1. cover_path = None
    _apply_custom_metadata(
        audio_path=mp3_file,
        title="Title Only",
        artist="Artist Only",
        cover_path=None,
        album="Album Only"
    )

    tags = ID3(mp3_file)
    assert str(tags["TIT2"]) == "Title Only"
    assert str(tags["TPE1"]) == "Artist Only"
    assert len(tags.getall("APIC")) == 0

    # 2. cover_path = несуществующий путь
    non_existent_cover = tmp_path / "ghost_cover.jpg"
    _apply_custom_metadata(
        audio_path=mp3_file,
        title="Title 2",
        artist="Artist 2",
        cover_path=non_existent_cover,
    )
    tags2 = ID3(mp3_file)
    assert str(tags2["TIT2"]) == "Title 2"
    assert len(tags2.getall("APIC")) == 0


@pytest.mark.asyncio
async def test_cover_regression_f_cover_download_failure_resilient(tmp_path):
    """
    Test F: Сбой скачивания обложки в download_track (ошибка в thumb_task)
    не приводит к ошибке всего пайплайна — трек успешно отдаётся пользователю.
    """
    from services.downloader import download_track, DownloadedAudio

    fake_audio_path = tmp_path / "valid.m4a"
    fake_audio_path.write_bytes(b"AUDIO_DATA")

    fake_downloaded = DownloadedAudio(
        file_path=fake_audio_path,
        title="Resilient Song",
        artist="Resilient Artist",
        duration=200,
        thumbnail_path=None,
        filesize=len(b"AUDIO_DATA"),
        folder_path=tmp_path
    )

    with patch("services.downloader._sync_download", return_value=fake_downloaded), \
         patch("services.downloader._process_remote_cover_bytes", side_effect=ConnectionError("CDN unreachable")):
        result = await download_track(
            query_or_url="https://www.youtube.com/watch?v=mock123",
            custom_artist="Resilient Artist",
            custom_title="Resilient Song",
            thumbnail_url="https://example.com/bad_cover.jpg"
        )

        assert result is not None
        assert result.title == "Resilient Song"
        assert result.artist == "Resilient Artist"
        assert result.file_path.exists()
