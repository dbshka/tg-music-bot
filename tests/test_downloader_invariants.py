import pytest
from services.downloader import compute_candidate_penalty


def test_candidate_penalty_artist_mismatch():
    cand = {
        "title": "Creep",
        "uploader": "Random Cover Singer",
        "channel": "Random Cover Singer",
        "duration": 236,
        "_source": "youtube"
    }
    penalty = compute_candidate_penalty(
        candidate=cand,
        custom_artist="Radiohead",
        custom_title="Creep",
        expected_duration=236,
        is_text_input=True
    )
    assert penalty >= 4000, f"Expected penalty >= 4000 for artist mismatch, got {penalty}"


def test_candidate_penalty_topic_cannot_override_artist_mismatch():
    cand_wrong_topic = {
        "title": "Creep",
        "uploader": "Korn - Topic",
        "channel": "Korn - Topic",
        "duration": 236,
        "_source": "youtube"
    }
    penalty_wrong_topic = compute_candidate_penalty(
        candidate=cand_wrong_topic,
        custom_artist="Radiohead",
        custom_title="Creep",
        expected_duration=236,
        is_text_input=True
    )
    # 4000 (artist mismatch) - 350 (topic) = 3650, which is still >= 3000!
    assert penalty_wrong_topic >= 3000, f"Topic should NOT override artist mismatch! Got {penalty_wrong_topic}"

    cand_correct_topic = {
        "title": "Creep",
        "uploader": "Radiohead - Topic",
        "channel": "Radiohead - Topic",
        "duration": 236,
        "_source": "youtube"
    }
    penalty_correct_topic = compute_candidate_penalty(
        candidate=cand_correct_topic,
        custom_artist="Radiohead",
        custom_title="Creep",
        expected_duration=236,
        is_text_input=True
    )
    assert penalty_correct_topic < 100, f"Correct topic should have low penalty! Got {penalty_correct_topic}"
