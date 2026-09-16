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
    # If DSP effects are requested on an invalid/corrupted file, it must raise RuntimeError and NOT return original!
    fake_file = tmp_path / "broken.mp3"
    fake_file.write_text("corrupted content")
    with pytest.raises(RuntimeError):
        _apply_audio_modifier_if_needed(fake_file, requested_modifiers={"slowed"}, cand_modifiers=set())
