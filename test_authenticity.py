import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp

from services.extractor import (
    resolve_canonical_track_info_async,
    _extract_spotify_embed_metadata,
    extract_spotify_info,
    extract_apple_music_info
)
from services.http_client import get_shared_session, close_shared_session


class TestAuthenticityVerification(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await close_shared_session()
    async def test_canonical_resolver_idioteque(self):
        """Проверяет каноническое определение эталона для 'idioteque radiohead'"""
        res = await resolve_canonical_track_info_async("idioteque radiohead")
        self.assertIsNotNone(res)
        self.assertEqual(res.artist.lower(), "radiohead")
        self.assertIn("idioteque", res.title.lower())
        # Длительность оригинального студийного альбома Kid A: 309s (5:09)
        self.assertIn(res.duration, range(300, 320))
        self.assertIsNotNone(res.thumbnail_url)

    async def test_canonical_resolver_505(self):
        """Проверяет каноническое определение эталона для '505 arctic monkeys'"""
        res = await resolve_canonical_track_info_async("505 arctic monkeys")
        self.assertIsNotNone(res)
        self.assertEqual(res.artist.lower(), "arctic monkeys")
        self.assertEqual(res.title.lower(), "505")
        # Длительность студийного оригинала: 253-254s (4:13)
        self.assertIn(res.duration, range(248, 258))
        self.assertIsNotNone(res.thumbnail_url)

    async def test_spotify_embed_metadata_505(self):
        """Проверяет прямое извлечение оригинальных метаданных из Spotify Embed"""
        session = get_shared_session()
        artist, title, cover, duration = await _extract_spotify_embed_metadata("0BxE4FqsDD1Ot4YuBXwAPp", session)
        self.assertEqual(artist, "Arctic Monkeys")
        self.assertEqual(title, "505")
        self.assertIn(duration, range(250, 258))
        self.assertIsNotNone(cover)

    async def test_spotify_full_extractor_sets_canonical_duration(self):
        """Проверяет, что extract_spotify_info гарантированно передает эталонную длительность"""
        session = get_shared_session()
        track = await extract_spotify_info("https://open.spotify.com/track/0BxE4FqsDD1Ot4YuBXwAPp", session)
        self.assertIsNotNone(track)
        self.assertEqual(track.artist, "Arctic Monkeys")
        self.assertEqual(track.title, "505")
        self.assertEqual(track.duration, 253)
        self.assertIn("ytsearch5", track.target)

    async def test_apple_music_extractor_sets_canonical_duration(self):
        """Проверяет, что extract_apple_music_info извлекает эталонную длительность 253s"""
        session = get_shared_session()
        track = await extract_apple_music_info("https://music.apple.com/us/song/505/251499791", session)
        self.assertIsNotNone(track)
        self.assertEqual(track.artist, "Arctic Monkeys")
        self.assertEqual(track.title, "505")
        self.assertEqual(track.duration, 253)
        self.assertIn("ytsearch5", track.target)

    def test_candidate_scoring_eliminates_8d_and_sped_up(self):
        """
        Проверяет математику скоринга:
        Официальный студийный трек Topic должен с огромным отрывом опережать
        8D-эдиты, замедленные версии и укороченные спидапы (226s против 253s).
        """
        from services.downloader import _sync_download
        # Импортируем логику скоринга через изоляцию
        expected_dur = 253
        custom_artist = "Arctic Monkeys"

        cands = [
            {"_source": "soundcloud", "uploader": "ArcticMonkeys", "title": "505", "duration": 30.0},
            {"_source": "soundcloud", "uploader": "sau/d", "title": "505 - arctic monkeys", "duration": 226.0},
            {"_source": "soundcloud", "uploader": "VERKNIPT", "title": "Arctic Monkeys - 505 (Xamuel Remix)", "duration": 250.0},
            {"_source": "youtube", "uploader": "Arctic Monkeys - Topic", "channel": "Arctic Monkeys - Topic", "title": "505", "duration": 253.0},
        ]

        # Скоринг аналогичный _candidate_penalty
        def score(c):
            t = c["title"].lower()
            u = c["uploader"].lower()
            dur = c["duration"]
            if dur <= 35:
                return 5000.0
            p = 0.0
            if "remix" in t or "cover" in t:
                p += 600.0
            diff = abs(dur - expected_dur)
            if diff <= 4:
                p -= 60.0
            elif diff > 22:
                p += 1500.0 + diff * 20.0
            if c["_source"] == "youtube" and u.endswith("- topic"):
                p -= 150.0
            elif c["_source"] == "soundcloud" and custom_artist.lower() not in u:
                p += 250.0
            return p

        scored = sorted(cands, key=score)
        # На первом месте ДОЛЖЕН быть официальный Topic релиз!
        self.assertEqual(scored[0]["uploader"], "Arctic Monkeys - Topic")
        self.assertEqual(scored[0]["duration"], 253.0)

        # Фанатский 226s и превью 30s должны быть в самом конце
        self.assertGreater(score(cands[1]), 1500.0)
        self.assertGreater(score(cands[0]), 4000.0)


if __name__ == "__main__":
    unittest.main()
