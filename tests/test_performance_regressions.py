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
    _is_candidate_promising
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
