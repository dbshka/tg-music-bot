import pytest
from services.database import (
    init_db,
    normalize_cache_key,
    build_variant_cache_key,
    get_cached_track,
    save_cached_track,
    delete_cached_track,
    invalidate_cached_file_id,
)


def test_cache_key_case_preservation():
    init_db()
    # YouTube video IDs are 11 chars case-sensitive
    k1 = normalize_cache_key("https://www.youtube.com/watch?v=AbCdEfGhIjK")
    assert k1 == "youtube:AbCdEfGhIjK"

    # Spotify IDs are 22 chars Base62 case-sensitive
    k2 = normalize_cache_key("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
    assert k2 == "spotify:4cOdK2wGLETKBW3PvgPWqT"


def test_variant_isolation_in_cache():
    init_db()
    base_key = "queen - bohemian rhapsody"
    key_orig = build_variant_cache_key(base_key, "original")
    key_slow = build_variant_cache_key(base_key, "slowed")

    assert key_orig == base_key
    assert key_slow == "queen - bohemian rhapsody#var=slowed"

    save_cached_track(base_key, "fid_orig", "Bohemian Rhapsody", "Queen", 354, "original")
    save_cached_track(base_key, "fid_slow", "Bohemian Rhapsody (Slowed)", "Queen", 410, "slowed")

    assert get_cached_track(base_key, "original")["file_id"] == "fid_orig"
    assert get_cached_track(base_key, "slowed")["file_id"] == "fid_slow"


def test_single_word_non_authoritative_query_protection():
    init_db()
    # 'creep' alone should NOT be cached as a standalone key
    save_cached_track("creep", "fid_creep_bad", "Creep", "Radiohead", 236)
    assert get_cached_track("creep") is None

    # Canonical entry must be cached
    assert get_cached_track("Radiohead - Creep") is not None


def test_dead_file_id_invalidation():
    init_db()
    test_key = "test_artist - test_track"
    save_cached_track(test_key, "dead_fid_999", "Test Track", "Test Artist", 180)
    assert get_cached_track(test_key) is not None

    count = invalidate_cached_file_id("dead_fid_999")
    assert count >= 1
    assert get_cached_track(test_key) is None
