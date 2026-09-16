import pytest
from services.identity import (
    clean_unicode_text,
    split_artist_names,
    validate_artist_match,
    extract_core_title_words,
    compute_title_match_ratio,
    parse_query_artist_title,
    extract_modifiers,
    has_track_modifiers,
    extract_track_modifiers,
    parse_speed_multiplier,
)


def test_clean_unicode_text():
    raw = "slow\u200bed\u200c \ufefftrack\u202e"
    cleaned = clean_unicode_text(raw)
    assert cleaned == "slowed track"
    assert "\u200b" not in cleaned
    assert "\u200c" not in cleaned
    assert "\ufeff" not in cleaned
    assert "\u202e" not in cleaned


def test_split_artist_names():
    artists = split_artist_names("Eminem feat. Rihanna & Skylar Grey / Dr. Dre")
    artists_lower = {a.lower() for a in artists}
    assert "eminem" in artists_lower
    assert "rihanna" in artists_lower
    assert "skylar grey" in artists_lower
    assert "dr. dre" in artists_lower

    # Hyphenated names should NOT be split into separate artists
    artists_hyphen = split_artist_names("A-Ha")
    assert {a.lower() for a in artists_hyphen} == {"a-ha"}

    artists_jayz = split_artist_names("Jay-Z")
    assert {a.lower() for a in artists_jayz} == {"jay-z"}


def test_validate_artist_match():
    # Exact and split match
    assert validate_artist_match("Radiohead", "Radiohead") is True

    # Featured artist match
    assert validate_artist_match("Eminem", "Eminem ft. Rihanna") is True

    # Substring collision prevention: "The The" vs "The Cure"
    assert validate_artist_match("The The", "The Cure") is False

    # Topic channel artist match
    assert validate_artist_match("Radiohead", "Radiohead - Topic") is True

    # Hyphenated artist preserved
    assert validate_artist_match("A-Ha", "A-Ha") is True


def test_extract_core_title_words_eponymous():
    # Eponymous protection: Black Sabbath - Black Sabbath
    words = extract_core_title_words("Black Sabbath", artist="Black Sabbath")
    assert "black" in words
    assert "sabbath" in words

    # Non-eponymous: artist name in title should be removed
    words2 = extract_core_title_words("Radiohead Creep Live", artist="Radiohead")
    assert "radiohead" not in words2
    assert "creep" in words2

    # Feature removal from title
    words3 = extract_core_title_words("Love The Way You Lie (feat. Rihanna)")
    assert "rihanna" not in words3
    assert "love" in words3


def test_compute_title_match_ratio():
    # Candidate title with artist prefix should match correctly
    words = extract_core_title_words("Creep", artist="Radiohead")
    ratio = compute_title_match_ratio("Radiohead - Creep (Official Music Video)", words)
    assert ratio >= 0.85

    # Conflicting title should fail
    ratio_bad = compute_title_match_ratio("Radiohead - Karma Police", words)
    assert ratio_bad < 0.5


def test_parse_query_artist_title():
    # Standard format
    art, tit = parse_query_artist_title("Queen - Bohemian Rhapsody")
    assert art.lower() == "queen"
    assert tit.lower() == "bohemian rhapsody"

    # Reversed query
    art2, tit2 = parse_query_artist_title("Bohemian Rhapsody by Queen")
    assert art2.lower() == "queen"
    assert tit2.lower() == "bohemian rhapsody"
