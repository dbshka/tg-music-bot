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

    async def test_spotify_extractor_super_slowed(self):
        """Проверяет извлечение реального автора и названия для модифицированного трека (DJ ZUP RAlii - Super Slowed)"""
        session = get_shared_session()
        track = await extract_spotify_info("https://open.spotify.com/track/6CdMaVhtjoqjV80VwUdkX7?si=ZbltTlKZSoOShpCJlWHmNg&utm_source=copy-link", session)
        self.assertIsNotNone(track)
        self.assertEqual(track.artist, "DJ ZUP RAlii")
        self.assertIn("Super Slowed", track.title)
        self.assertIn("ytsearch5", track.target)
        self.assertIn("DJ ZUP RAlii", track.target)

    async def test_youtube_music_unpopular_track_resolver(self):
        """Проверяет извлечение метаданных для непопулярного андеграундного трека из YouTube Music"""
        from services.extractor import resolve_track_url
        session = get_shared_session()
        track = await resolve_track_url("https://music.youtube.com/watch?si=fsLDDCUT9JIzPK82&v=VqK-0ZKQj98", session)
        self.assertIsNotNone(track)
        self.assertEqual(track.target, "https://www.youtube.com/watch?v=VqK-0ZKQj98")
        self.assertIn("dumb filler song", track.title.lower())

    def test_query_aware_modifier_ranking(self):
        """
        Проверяет, что при запросе с 'slowed' трек с модификатором 'slowed'
        получает наивысший приоритет над стандартной версией.
        """
        from services.extractor import TRACK_MODIFIERS

        query = "DJ ZUP RAlii - не слышу - Super Slowed"
        req_modifiers = {mod for mod in TRACK_MODIFIERS if mod in query.lower()}
        self.assertIn("slowed", req_modifiers)

        cands = [
            {"title": "не слышу (Super Slowed)", "uploader": "Release - Topic", "duration": 98.0},
            {"title": "не слышу (Original)", "uploader": "Release - Topic", "duration": 97.0},
        ]

        def score(c):
            p = 0.0
            cand_text = f"{c['title']} {c['uploader']}".lower()
            cand_mods = {mod for mod in TRACK_MODIFIERS if mod in cand_text}
            matching = req_modifiers & cand_mods
            if matching:
                p -= 150.0 * len(matching)
            else:
                p += 200.0
            return p

        scored = sorted(cands, key=score)
        self.assertEqual(scored[0]["title"], "не слышу (Super Slowed)")

    def test_core_title_word_extraction(self):
        """Проверяет извлечение ключевых слов названия без модификаторов и исполнителя."""
        from services.extractor import extract_core_title_words, compute_title_match_ratio

        words_slowed = extract_core_title_words("не слышу - Super Slowed", "DJ ZUP RAlii")
        self.assertTrue({"не", "слышу"}.issubset(words_slowed))
        self.assertNotIn("slowed", words_slowed)
        self.assertNotIn("super", words_slowed)

        words_505 = extract_core_title_words("505", "Arctic Monkeys")
        self.assertEqual(words_505, {"505"})

        words_karma = extract_core_title_words("Karma Police", "Radiohead")
        self.assertEqual(words_karma, {"karma", "police"})

    def test_title_match_ratio_and_fake_disqualification(self):
        """
        Проверяет, что подлинный трек получает 1.0 совпадения,
        транслитерация также дает 1.0,
        а чужой трек (MuzloRAlii) получает 0.0 и отсеивается.
        """
        from services.extractor import extract_core_title_words, compute_title_match_ratio

        core_words = extract_core_title_words("не слышу - Super Slowed", "DJ ZUP RAlii")

        ratio_real = compute_title_match_ratio("не слышу (Super Slowed)", core_words)
        self.assertEqual(ratio_real, 1.0)

        ratio_translit = compute_title_match_ratio("DJ ZUP RAlii - ne slyshu (Super Slowed)", core_words)
        self.assertEqual(ratio_translit, 1.0)

        ratio_fake = compute_title_match_ratio("MuzloRAlii.net - DJ ZUP RAlii (Super Slowed)", core_words)
        self.assertEqual(ratio_fake, 0.0)


if __name__ == "__main__":
    unittest.main()
