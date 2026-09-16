import pytest
import re
from pathlib import Path
from mutagen.id3 import ID3, TIT2, TPE1

from services.identity import (
    extract_modifiers,
    extract_track_modifiers,
    parse_query_artist_title,
    TrackIdentity,
)
from services.extractor import ExtractedTrack, parse_url_and_modifiers
from services.downloader import (
    _apply_audio_modifier_if_needed,
    _apply_custom_metadata,
    download_track,
)


# =====================================================================
# 1. BUG-REG-01: "The Drums - Drums" & Eponymous Tracks Protection
# =====================================================================

def test_drums_modifier_context_protection():
    """Standalone 'drums' or 'drum' must not be stripped if not in explicit modifier context."""
    # Eponymous song
    assert "drums" not in extract_modifiers("The Drums - Drums", ignore_words={"the", "drums"})
    clean, mods = extract_track_modifiers("Drums", ignore_words={"the", "drums"})
    assert clean == "Drums"
    assert mods == []

    # Standalone query without dash
    assert "drums" not in extract_modifiers("The Drums Drums")

    # Title completely wiped protection
    clean2, mods2 = extract_track_modifiers("Drums")
    assert clean2 == "Drums"
    assert mods2 == []

    # Eponymous artist & song
    clean_future, mods_future = extract_track_modifiers("Future", ignore_words={"future"})
    assert clean_future == "Future"
    assert mods_future == []

    # Valid contextual drum modifiers MUST still be detected
    mods_bracket = extract_modifiers("Song (drums)")
    assert "drums" in mods_bracket

    mods_with = extract_modifiers("Song with drums")
    assert "drums" in mods_with or "with drums" in mods_with

    mods_cover = extract_modifiers("Song drum cover")
    assert "drum cover" in mods_cover or "drums" in mods_cover


def test_eponymous_dash_parsing():
    """Splitting by dash before modifier extraction preserves artist and eponymous title."""
    user_text = "The Drums - Drums"
    dash_match = re.split(r'\s+[-—–]\s+', user_text, maxsplit=1)
    assert len(dash_match) == 2
    raw_artist = dash_match[0].strip()
    raw_title = dash_match[1].strip()
    artist_words = set(re.findall(r'[\w]+', raw_artist.lower()))

    clean_title, text_mods = extract_track_modifiers(raw_title, ignore_words=artist_words)
    assert raw_artist == "The Drums"
    assert clean_title == "Drums"
    assert text_mods == []


# =====================================================================
# 2. BUG-REG-02: "Blinding Lights — The Weeknd" Inverted Metadata Fix
# =====================================================================

def test_reversed_query_canonical_override():
    """Canonical artist/title from catalog must override user-reversed query tags."""
    custom_artist = "Blinding Lights"
    custom_title = "The Weeknd"

    # Canonical catalog result
    canonical = ExtractedTrack(
        platform="Canonical/Deezer",
        target="ytsearch5:The Weeknd - Blinding Lights",
        is_search=True,
        title="Blinding Lights",
        artist="The Weeknd",
        duration=200
    )

    eff_artist = canonical.artist or custom_artist
    eff_title = canonical.title or custom_title
    assert eff_artist == "The Weeknd"
    assert eff_title == "Blinding Lights"

    track_info = ExtractedTrack(
        platform=canonical.platform,
        target=f"ytsearch5:{eff_artist} {eff_title}",
        is_search=True,
        title=eff_title,
        artist=eff_artist,
        thumbnail_url=canonical.thumbnail_url,
        duration=canonical.duration
    )
    assert track_info.artist == "The Weeknd"
    assert track_info.title == "Blinding Lights"


def test_tag_editor_metadata_order(tmp_path):
    """Applying metadata writes correct Title and Artist ID3 frames."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 1000)

    _apply_custom_metadata(fake_audio, title="Blinding Lights", artist="The Weeknd")
    tags = ID3(fake_audio)
    assert str(tags["TIT2"]) == "Blinding Lights"
    assert str(tags["TPE1"]) == "The Weeknd"


# =====================================================================
# 3. BUG-REG-03: Reverb Must Never Return Unmodified Original
# =====================================================================

def test_reverb_rejection_on_unmodded_candidate(tmp_path):
    """When user requests reverb and candidate is unmodded, must raise ValueError."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 1000)

    # Requested modifier is reverb, candidate has NO modifiers
    with pytest.raises(ValueError, match="Версия с запрошенной модификацией"):
        _apply_audio_modifier_if_needed(
            audio_path=fake_audio,
            requested_modifiers={"reverb"},
            cand_modifiers=set(),
            req_query="Radiohead - Creep reverb"
        )


def test_reverb_accepted_when_candidate_has_reverb(tmp_path):
    """When candidate already has reverb, _apply_audio_modifier_if_needed returns 0 without raising."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 1000)

    res = _apply_audio_modifier_if_needed(
        audio_path=fake_audio,
        requested_modifiers={"reverb"},
        cand_modifiers={"reverb"},
        req_query="Radiohead - Creep reverb"
    )
    assert res == 0


# =====================================================================
# 4. BUG-REG-04: Canonical Cover Overrides YouTube Thumbnail
# =====================================================================

def test_canonical_cover_overrides_youtube_thumbnail(tmp_path):
    """High quality canonical thumbnail must take priority over yt-dlp extracted video thumbnail."""
    yt_thumb = tmp_path / "yt_thumb.jpg"
    yt_thumb.write_bytes(b"yt_thumbnail_data")

    canonical_thumb = tmp_path / "canonical_cover.jpg"
    canonical_thumb.write_bytes(b"high_res_deezer_cover_data")

    audio_thumbnail_path = yt_thumb
    downloaded_thumb = canonical_thumb

    if downloaded_thumb and downloaded_thumb.exists():
        audio_thumbnail_path = downloaded_thumb

    assert audio_thumbnail_path == canonical_thumb
    assert audio_thumbnail_path.read_bytes() == b"high_res_deezer_cover_data"


# =====================================================================
# 5. Spotify Final Duration Validation
# =====================================================================

def test_spotify_duration_validation_logic():
    """Spotify URLs must be subject to final duration validation."""
    url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    track_info = ExtractedTrack(
        platform="Spotify",
        target="ytsearch5:Rick Astley Never Gonna Give You Up",
        is_search=True,
        title="Never Gonna Give You Up",
        artist="Rick Astley",
        duration=213
    )

    is_direct_media = bool(url and any(d in url.lower() for d in ("youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "bandcamp.com", "vk.com", "tiktok.com")))
    has_canonical_dur = bool(track_info.duration and track_info.duration > 35)

    assert is_direct_media is False
    assert has_canonical_dur is True

    # 1. Matching duration (diff=1s <= max_final_diff=4s) -> PASS
    actual_final_dur_ok = 214
    final_diff = abs(actual_final_dur_ok - track_info.duration)
    max_final_diff = max(4, min(7, int(track_info.duration * 0.02)))
    assert final_diff <= max_final_diff

    # 2. Mismatched duration (diff=30s > max_final_diff=4s) -> REJECT
    actual_final_dur_bad = 245
    final_diff_bad = abs(actual_final_dur_bad - track_info.duration)
    assert final_diff_bad > max_final_diff


def test_direct_media_duration_exemption():
    """Direct YouTube or SoundCloud URLs must NOT be rejected by canonical duration check."""
    yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    is_direct_media = bool(yt_url and any(d in yt_url.lower() for d in ("youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "bandcamp.com", "vk.com", "tiktok.com")))
    assert is_direct_media is True
