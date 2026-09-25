import pytest
from services.identity import (
    extract_modifiers,
    has_track_modifiers,
    extract_track_modifiers,
    parse_speed_multiplier,
    DSP_SUPPORTED_MODIFIERS,
    SEMANTIC_MODIFIERS,
)
from services.downloader import _apply_audio_modifier_if_needed


def test_modifier_context_protection():
    # SLOW DANCING IN THE DARK shouldn't be detected as slow
    mods1 = extract_modifiers("Joji - SLOW DANCING IN THE DARK")
    assert "slow" not in mods1

    # Live to Rise shouldn't be detected as live
    mods2 = extract_modifiers("Soundgarden - Live to Rise")
    assert "live" not in mods2

    # Real modifier should be detected
    mods3 = extract_modifiers("Radiohead - Creep (Slowed + Reverb)")
    assert "slowed" in mods3
    assert "reverb" in mods3


def test_speed_multipliers():
    assert parse_speed_multiplier("track 0.75x") == 0.75
    assert parse_speed_multiplier("track 1.25x") == 1.25
    assert parse_speed_multiplier("track 85%") == 0.85
    assert parse_speed_multiplier("track 115%") == 1.15

    # Out of range multipliers ignored
    assert parse_speed_multiplier("track 0.3x") is None
    assert parse_speed_multiplier("track 3.0x") is None


def test_extract_track_modifiers_stripping():
    clean, mods = extract_track_modifiers("Queen - Bohemian Rhapsody (Slowed)")
    assert "slowed" in mods
    assert "slowed" not in clean.lower()
    assert "Queen - Bohemian Rhapsody" in clean


def test_dsp_fatal_error_handling(tmp_path):
    # When modifiers are requested on a candidate without them, must raise ValueError and NOT return original or use DSP
    fake_file = tmp_path / "broken.mp3"
    fake_file.write_text("corrupted content")
    with pytest.raises(ValueError, match="Возникла ошибка 44"):
        _apply_audio_modifier_if_needed(fake_file, requested_modifiers={"slowed"}, cand_modifiers=set())


# =====================================================================
# 8 REQUIRED REGRESSION TESTS FOR READY-MADE VARIANTS ARCHITECTURE
# =====================================================================

from unittest.mock import patch, MagicMock
from services.downloader import compute_candidate_penalty
from services.identity import is_candidate_matching_modifiers, SUPER_SLOWED_GROUP
from services.database import build_variant_cache_key, save_cached_track, get_cached_track, init_db


def test_1_original_request_selects_original():
    """1. Запрос оригинала: Blinding Lights -> кандидат-оригинал выбирается, candidate title/target не требует slowed."""
    custom_artist = "The Weeknd"
    custom_title = "Blinding Lights"
    req_mods = extract_modifiers(f"{custom_artist} {custom_title}")
    assert not req_mods

    cand_original = {
        "title": "The Weeknd - Blinding Lights",
        "uploader": "The Weeknd - Topic",
        "channel": "The Weeknd - Topic",
        "duration": 200,
        "_source": "youtube"
    }
    cand_slowed = {
        "title": "The Weeknd - Blinding Lights (Slowed)",
        "uploader": "Slowed Vibes",
        "channel": "Slowed Vibes",
        "duration": 235,
        "_source": "youtube"
    }

    penalty_orig = compute_candidate_penalty(
        candidate=cand_original,
        custom_artist=custom_artist,
        custom_title=custom_title,
        expected_duration=200,
        requested_modifiers=req_mods,
        is_text_input=True
    )
    penalty_slowed = compute_candidate_penalty(
        candidate=cand_slowed,
        custom_artist=custom_artist,
        custom_title=custom_title,
        expected_duration=200,
        requested_modifiers=req_mods,
        is_text_input=True
    )

    # Оригинал должен иметь наилучший (отрицательный) штраф
    assert penalty_orig < 0, f"Оригинал должен иметь низкий penalty, получено {penalty_orig}"
    # Slowed-кандидат получает штраф +5000 за нежелательные модификаторы
    assert penalty_slowed >= 4000, f"Slowed-кандидат должен быть оштрафован, получено {penalty_slowed}"
    assert penalty_orig < penalty_slowed


def test_2_slowed_request_queries_and_selects_slowed():
    """2. Запрос Slowed: Blinding Lights Slowed -> ищется готовая slowed-версия, query/candidate содержит Slowed."""
    raw_query = "The Weeknd — Blinding Lights Slowed"
    clean_title, mods = extract_track_modifiers("Blinding Lights Slowed")
    assert clean_title == "Blinding Lights"
    assert "slowed" in mods

    req_mods = extract_modifiers(raw_query)
    assert "slowed" in req_mods

    cand_original = {
        "title": "The Weeknd - Blinding Lights",
        "uploader": "The Weeknd - Topic",
        "channel": "The Weeknd - Topic",
        "duration": 200,
        "_source": "youtube"
    }
    cand_slowed = {
        "title": "The Weeknd - Blinding Lights (Slowed)",
        "uploader": "Slowed Vibes",
        "channel": "Slowed Vibes",
        "duration": 235,
        "_source": "youtube"
    }

    penalty_orig = compute_candidate_penalty(
        candidate=cand_original,
        custom_artist="The Weeknd",
        custom_title=clean_title,
        requested_modifiers=req_mods,
        is_text_input=True
    )
    penalty_slowed = compute_candidate_penalty(
        candidate=cand_slowed,
        custom_artist="The Weeknd",
        custom_title=clean_title,
        requested_modifiers=req_mods,
        is_text_input=True
    )

    # При запросе Slowed: готовый Slowed релиз побеждает студийный оригинал
    assert penalty_slowed < 0, f"Slowed-кандидат должен получить бонус, получено {penalty_slowed}"
    assert penalty_orig >= 4000, f"Оригинал должен получить высокий штраф из-за отсутствия slowed, получено {penalty_orig}"
    assert penalty_slowed < penalty_orig


def test_3_original_candidate_rejected_for_slowed(tmp_path):
    """3. Кандидат-оригинал для Slowed: если попался оригинал -> FAIL / отклонён, оригинал не подменяет slowed."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 500)

    req_mods = {"slowed"}
    cand_orig_mods = set()

    # Семантическая проверка соответствия кандидатов
    assert not is_candidate_matching_modifiers(req_mods, cand_orig_mods)

    # Проверка вызова: должна выбросить исключение, а не подменить оригинал
    with pytest.raises(ValueError, match="Возникла ошибка 44"):
        _apply_audio_modifier_if_needed(
            audio_path=fake_audio,
            requested_modifiers=req_mods,
            cand_modifiers=cand_orig_mods
        )


def test_4_slowed_candidate_accepted_for_slowed(tmp_path):
    """4. Кандидат Slowed для Slowed: если найден готовый slowed candidate -> PASS / принят."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 500)

    req_mods = {"slowed"}
    cand_slowed_mods = {"slowed"}

    assert is_candidate_matching_modifiers(req_mods, cand_slowed_mods)

    # Готовая slowed-версия принимается без ошибок и без изменений (возвращает 0)
    res = _apply_audio_modifier_if_needed(
        audio_path=fake_audio,
        requested_modifiers=req_mods,
        cand_modifiers=cand_slowed_mods
    )
    assert res == 0


def test_5_super_slowed_strict_match():
    """5. Super Slowed: кандидат с обычным Slowed или оригиналом не считается совпадением, выбирается именно Super Slowed."""
    req_mods = extract_modifiers("The Weeknd - Blinding Lights Super Slowed")
    assert "super slowed" in req_mods

    cand_orig_mods = set()
    cand_slowed_mods = {"slowed"}
    cand_super_slowed_mods = {"super slowed", "slowed"}

    # 1. Оригинал не подходит
    assert not is_candidate_matching_modifiers(req_mods, cand_orig_mods)
    # 2. Обычный Slowed НЕ подходит для Super Slowed!
    assert not is_candidate_matching_modifiers(req_mods, cand_slowed_mods)
    # 3. Готовый Super Slowed подходит
    assert is_candidate_matching_modifiers(req_mods, cand_super_slowed_mods)

    # Проверка ранжирования кандидатов
    cand_super = {
        "title": "The Weeknd - Blinding Lights (Super Slowed)",
        "uploader": "SuperSlowed Vibe",
        "channel": "SuperSlowed Vibe",
        "duration": 270,
        "_source": "youtube"
    }
    cand_slow = {
        "title": "The Weeknd - Blinding Lights (Slowed)",
        "uploader": "Slowed Vibe",
        "channel": "Slowed Vibe",
        "duration": 230,
        "_source": "youtube"
    }
    cand_orig = {
        "title": "The Weeknd - Blinding Lights",
        "uploader": "The Weeknd - Topic",
        "channel": "The Weeknd - Topic",
        "duration": 200,
        "_source": "youtube"
    }

    p_super = compute_candidate_penalty(cand_super, "The Weeknd", "Blinding Lights", requested_modifiers=req_mods, is_text_input=True)
    p_slow = compute_candidate_penalty(cand_slow, "The Weeknd", "Blinding Lights", requested_modifiers=req_mods, is_text_input=True)
    p_orig = compute_candidate_penalty(cand_orig, "The Weeknd", "Blinding Lights", requested_modifiers=req_mods, is_text_input=True)

    assert p_super < 0, f"Super Slowed должен иметь отрицательный штраф, получено {p_super}"
    assert p_slow >= 4000, f"Обычный Slowed должен быть оштрафован, получено {p_slow}"
    assert p_orig >= 4000, f"Оригинал должен быть оштрафован, получено {p_orig}"
    assert p_super < p_slow < p_orig or p_super < p_slow


def test_6_reverb_no_dsp_processing(tmp_path):
    """6. Reverb: проверяем запрос с Reverb и гарантируем отсутствие вызовов DSP/FFmpeg обработки аудио."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 500)

    req_mods = {"reverb"}

    with patch("subprocess.run") as mock_subproc:
        # A. Готовый релиз с Reverb принимается напрямую без вызова FFmpeg
        res = _apply_audio_modifier_if_needed(
            audio_path=fake_audio,
            requested_modifiers=req_mods,
            cand_modifiers={"reverb"}
        )
        assert res == 0
        mock_subproc.assert_not_called()

        # B. Кандидат без Reverb отклоняется с ошибкой без попытки вызова FFmpeg
        with pytest.raises(ValueError):
            _apply_audio_modifier_if_needed(
                audio_path=fake_audio,
                requested_modifiers=req_mods,
                cand_modifiers=set()
            )
        mock_subproc.assert_not_called()


def test_7_cache_partitioning_original_vs_slowed():
    """7. Cache-разделение: оригинал и slowed имеют изолированные ключи; повторный запрос Slowed возвращает Slowed."""
    init_db()

    base_query = "the weeknd - blinding lights"
    orig_key = build_variant_cache_key(base_query, "original")
    slowed_key = build_variant_cache_key(base_query, "slowed")

    assert orig_key == "the weeknd - blinding lights"
    assert slowed_key == "the weeknd - blinding lights#var=slowed"
    assert orig_key != slowed_key

    # Сохраняем оригинал и slowed
    save_cached_track(
        query=base_query,
        file_id="tg_file_id_original",
        title="Blinding Lights",
        artist="The Weeknd",
        duration=200,
        variant="original"
    )
    save_cached_track(
        query=base_query,
        file_id="tg_file_id_slowed",
        title="Blinding Lights (Slowed)",
        artist="The Weeknd",
        duration=235,
        variant="slowed"
    )

    cached_orig = get_cached_track(base_query, variant="original")
    cached_slowed = get_cached_track(base_query, variant="slowed")

    assert cached_orig is not None
    assert cached_orig["file_id"] == "tg_file_id_original"
    assert cached_orig["variant"] == "original"

    assert cached_slowed is not None
    assert cached_slowed["file_id"] == "tg_file_id_slowed"
    assert cached_slowed["variant"] == "slowed"


def test_8_regular_youtube_download_flow(tmp_path):
    """8. YouTube download: обычный трек с YouTube скачивается штатно без ошибок."""
    from services.downloader import _sync_download

    fake_ydl_result = {
        "title": "Blinding Lights",
        "uploader": "The Weeknd - Topic",
        "duration": 200,
        "webpage_url": "https://www.youtube.com/watch?v=fHI8X483mQw",
        "_source": "youtube"
    }

    def fake_extract_info(url, download=False):
        if download:
            af = tmp_path / "Blinding Lights.m4a"
            af.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 2000)
        return fake_ydl_result

    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__.return_value = mock_ydl_instance
    mock_ydl_instance.extract_info.side_effect = fake_extract_info

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl_instance), \
         patch("services.downloader._apply_custom_metadata"):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=fHI8X483mQw",
            output_dir=tmp_path,
            custom_title="Blinding Lights",
            custom_artist="The Weeknd",
            expected_duration=200,
            requested_variant="original"
        )
        assert downloaded is not None
        assert downloaded.title == "Blinding Lights"
        assert downloaded.artist == "The Weeknd"
        assert downloaded.file_path.exists()


# =====================================================================
# REGRESSION TESTS FOR SOURCE METADATA & FINAL VALIDATION
# =====================================================================

def test_regression_1_slowed_request_with_slowed_source_title_passes():
    """1. Blinding Lights Slowed + source title Blinding Lights (Slowed) -> PASS."""
    target_mods = extract_modifiers("slowed")
    source_title = "The Weeknd - Blinding Lights (Slowed)"
    source_mods = extract_modifiers(source_title)
    assert "slowed" in source_mods
    assert is_candidate_matching_modifiers(target_mods, source_mods) is True


def test_regression_2_slowed_request_with_slowed_down_source_title_passes():
    """2. Blinding Lights Slowed + source title Blinding Lights (Slowed Down) -> PASS."""
    target_mods = extract_modifiers("slowed")
    source_title = "The Weeknd - Blinding Lights (Slowed Down)"
    source_mods = extract_modifiers(source_title)
    assert "slowed" in source_mods
    assert is_candidate_matching_modifiers(target_mods, source_mods) is True


def test_regression_3_slowed_request_with_original_source_title_fails():
    """3. Blinding Lights Slowed + source title Blinding Lights -> FAIL."""
    target_mods = extract_modifiers("slowed")
    source_title = "The Weeknd - Blinding Lights"
    source_mods = extract_modifiers(source_title)
    assert not source_mods
    assert is_candidate_matching_modifiers(target_mods, source_mods) is False


def test_regression_4_custom_title_does_not_destroy_source_title(tmp_path):
    """4. custom_title='Blinding Lights' не должен уничтожать source_title."""
    from services.downloader import DownloadedAudio
    audio = DownloadedAudio(
        file_path=tmp_path / "track.m4a",
        title="Blinding Lights",
        artist="The Weeknd",
        duration=225,
        thumbnail_path=None,
        filesize=1000,
        folder_path=tmp_path,
        source_title="The Weeknd - Blinding Lights (Slowed Down)",
        source_modifiers={"slowed"}
    )
    assert audio.title == "Blinding Lights"
    assert audio.source_title == "The Weeknd - Blinding Lights (Slowed Down)"


def test_regression_5_source_modifiers_preserved_on_downloaded_audio(tmp_path):
    """5. source_modifiers сохраняются после создания DownloadedAudio."""
    from services.downloader import DownloadedAudio
    audio = DownloadedAudio(
        file_path=tmp_path / "track.m4a",
        title="Blinding Lights",
        artist="The Weeknd",
        duration=225,
        thumbnail_path=None,
        filesize=1000,
        folder_path=tmp_path,
        source_title="The Weeknd - Blinding Lights (Slowed + Reverb)",
        source_modifiers={"slowed", "reverb"}
    )
    assert audio.source_modifiers == {"slowed", "reverb"}


def test_regression_6_regular_blinding_lights_has_no_variant():
    """6. Обычный Blinding Lights остаётся без variant."""
    clean_title, mods = extract_track_modifiers("Blinding Lights")
    assert clean_title == "Blinding Lights"
    assert not mods

    full_clean, full_mods = extract_track_modifiers("The Weeknd — Blinding Lights")
    assert not full_mods
    variant = ", ".join(full_mods) if full_mods else "original"
    assert variant == "original"


def test_regression_7_super_slowed_does_not_turn_into_slowed():
    """7. Super Slowed не превращается автоматически в Slowed."""
    target_super_slowed = extract_modifiers("super slowed")
    assert "super slowed" in target_super_slowed

    # Кандидат только с обычным slowed НЕ должен удовлетворять запросу super slowed
    cand_slowed_only = {"slowed"}
    assert is_candidate_matching_modifiers(target_super_slowed, cand_slowed_only) is False

    # Кандидат с super slowed удовлетворяет
    cand_super_slowed = {"super slowed"}
    assert is_candidate_matching_modifiers(target_super_slowed, cand_super_slowed) is True


def test_sync_download_populates_source_metadata(tmp_path):
    """Интеграционный тест: _sync_download корректно заполняет source_title и source_modifiers."""
    from services.downloader import _sync_download

    fake_ydl_result = {
        "title": "The Weeknd - Blinding Lights (Slowed Down)",
        "uploader": "Slowed Vibes",
        "duration": 235,
        "webpage_url": "https://www.youtube.com/watch?v=mock123",
        "_source": "youtube"
    }

    def fake_extract_info(url, download=False):
        if download:
            af = tmp_path / "Blinding Lights.m4a"
            af.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 2000)
        return fake_ydl_result

    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__.return_value = mock_ydl_instance
    mock_ydl_instance.extract_info.side_effect = fake_extract_info

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl_instance), \
         patch("services.downloader._apply_custom_metadata"):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=mock123",
            output_dir=tmp_path,
            custom_title="Blinding Lights",
            custom_artist="The Weeknd",
            expected_duration=None,
            requested_variant="slowed"
        )
        assert downloaded is not None
        assert downloaded.title == "Blinding Lights"
        assert downloaded.source_title == "The Weeknd - Blinding Lights (Slowed Down)"
        assert "slowed" in downloaded.source_modifiers
        assert is_candidate_matching_modifiers({"slowed"}, downloaded.source_modifiers) is True
