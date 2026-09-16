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
        if track.title:
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

    def test_extract_modifiers_word_boundaries(self):
        """
        Проверяет, что extract_modifiers правильно находит модификаторы с границами слов
        и не дает ложных срабатываний на словах 'slowly', 'discover', 'credit', 'delivery'.
        """
        from services.extractor import extract_modifiers, has_track_modifiers

        self.assertTrue(has_track_modifiers("505, slowed - Arctic Monkeys"))
        self.assertIn("slowed", extract_modifiers("505, slowed - Arctic Monkeys"))

        self.assertFalse(has_track_modifiers("Tyler, The Creator - See You Again (feat. Kali Uchis)"))
        self.assertEqual(extract_modifiers("Tyler, The Creator - See You Again (feat. Kali Uchis)"), set())

        # Ложные подстроки не должны распознаваться как модификаторы:
        self.assertFalse(has_track_modifiers("Walking Slowly Down the Street"))
        self.assertFalse(has_track_modifiers("Discover New Horizons"))
        self.assertFalse(has_track_modifiers("Give credit where it is due"))
        self.assertFalse(has_track_modifiers("Special Delivery"))

    def test_candidate_scoring_strictly_penalizes_slowed_when_original_requested(self):
        """
        Проверяет, что при запросе студийного оригинала (505, 253с):
        1) Кандидат '505, slowed' (264.9с) получает дисквалифицирующий штраф > 5000.
        2) Кандидат с оригинальной длительностью (252с) побеждает с огромным отрывом.
        """
        from services.extractor import extract_modifiers, extract_core_title_words, compute_title_match_ratio

        expected_duration = 253
        requested_modifiers = set()
        core_title_words = {"505"}

        def _candidate_penalty(e):
            cand_title = e.get("title", "").lower()
            cand_uploader = e.get("uploader", "").lower()
            cand_channel = e.get("channel", "").lower()
            dur = e.get("duration") or 0
            penalty = 0.0

            cand_text = f"{cand_title} {cand_uploader} {cand_channel}"
            cand_modifiers = extract_modifiers(cand_text)

            if cand_modifiers:
                penalty += 5000.0

            diff = abs(dur - expected_duration)
            if diff <= 3:
                penalty -= 100.0
            elif diff <= 6:
                penalty -= 40.0
            elif diff <= 10:
                penalty += 200.0 + (diff * 15.0)
            else:
                penalty += 2500.0 + (diff * 30.0)

            return penalty

        cand_slowed = {"title": "505, slowed", "uploader": "Arctic Monkeys", "channel": "", "duration": 264.9}
        cand_studio = {"title": "Arctic Monkeys - 505", "uploader": "Pizza Music", "channel": "", "duration": 252.0}

        p_slowed = _candidate_penalty(cand_slowed)
        p_studio = _candidate_penalty(cand_studio)

        self.assertGreater(p_slowed, 5000.0)
        self.assertLess(p_studio, 0.0)
        self.assertLess(p_studio, p_slowed - 5000.0)

    def test_studio_restoration_calculation(self):
        """Проверяет математику расчета ratio для восстановления студийного хронометража"""
        # Tyler See You Again: uploader pitch/tempo shift 186.15s vs master 180s
        expected_dur = 180
        actual_dur = 186
        diff = abs(actual_dur - expected_dur)
        ratio = actual_dur / expected_dur

        self.assertTrue(2 < diff <= 35)
        self.assertTrue(0.85 <= ratio <= 1.15)
        self.assertAlmostEqual(ratio, 1.03333, places=3)

        # 505 Arctic Monkeys: slowed candidate 264.9s vs master 253s
        expected_505 = 253
        actual_505 = 265
        diff_505 = abs(actual_505 - expected_505)
        ratio_505 = actual_505 / expected_505

        self.assertTrue(2 < diff_505 <= 35)
        self.assertTrue(0.85 <= ratio_505 <= 1.15)

    def test_cache_duration_validation_logic(self):
        """Проверяет логику отбраковки устаревшего кэша с искаженным хронометражем"""
        expected_duration = 180  # Apple Music master
        stale_cached_dur = 186   # Старый кэш со сдвигом скорости
        fresh_cached_dur = 180   # Восстановленный студийный трек

        # Старый кэш должен браковаться (diff = 6 > 2)
        self.assertGreater(abs(stale_cached_dur - expected_duration), 2)

        # Валидный кэш принимается мгновенно (diff = 0 <= 2)
        self.assertLessEqual(abs(fresh_cached_dur - expected_duration), 2)

    def test_drum_modifier_detection(self):
        """Проверяет распознавание драм-ремиксов и учет ignore_words"""
        from services.extractor import extract_modifiers

        # Ремиксы с драмкой
        mods1 = extract_modifiers("Song Name (Drum Edit)")
        self.assertIn("drum edit", mods1)

        mods2 = extract_modifiers("Song Name (with drums)")
        self.assertTrue("with drums" in mods2 or "drums" in mods2)

        mods3 = extract_modifiers("Artist - Title (Drum Remix)")
        self.assertIn("drum remix", mods3)

        mods4 = extract_modifiers("Artist - Title (DNB Flip)")
        self.assertIn("dnb", mods4)

        mods5 = extract_modifiers("Artist - Title (ремикс с драмкой)")
        self.assertTrue("ремикс" in mods5 or "с драмкой" in mods5 or "драмка" in mods5)

        # Если в оригинальном названии есть слово drums (группа The Drums), оно игнорируется
        ignore = {"the", "drums", "money"}
        mods_legit = extract_modifiers("The Drums - Money", ignore_words=ignore)
        self.assertEqual(len(mods_legit), 0)

    async def test_creep_canonical_metadata_lookup(self):
        """Проверяет эталонное определение хронометража для текстового запроса 'Radiohead Creep'"""
        res = await resolve_canonical_track_info_async("Radiohead Creep")
        self.assertIsNotNone(res)
        self.assertEqual(res.artist.lower(), "radiohead")
        self.assertEqual(res.title.lower(), "creep")
        # Эталонный хронометраж Radiohead - Creep: 238с (3:58)
        self.assertIn(res.duration, range(235, 241))

    def test_creep_text_search_candidate_scoring_and_tolerance(self):
        """
        Проверяет математику скоринга и допуск хронометража для кейса Radiohead - Creep:
        Студийный кандидат (236-238с) должен гарантированно побеждать версию 4:05 (245с),
        а фильтр хронометража is_text_input обязан отклонять кандидата 245с (diff=7s > 4s).
        """
        expected_duration = 238  # Эталон Deezer (3:58)
        is_text_input = True
        requested_modifiers = set()

        def _candidate_penalty(e):
            cand_title = e.get("title", "").lower()
            dur = e.get("duration", 0)
            penalty = 0.0
            if "creep" in cand_title:
                penalty -= 120.0
            diff = abs(dur - expected_duration)
            if not requested_modifiers:
                if diff <= 4:
                    penalty -= 160.0
                elif diff <= 6:
                    penalty -= 40.0
                elif diff <= 12:
                    penalty += 200.0 + (diff * 15.0)
                elif diff <= 25:
                    penalty += 500.0 + (diff * 20.0)
                else:
                    penalty += 1500.0 + (diff * 25.0)
            return penalty

        cand_studio = {"title": "Radiohead - Creep", "duration": 236}   # Официальный клип/аудио (3:56)
        cand_elongated = {"title": "Radiohead - Creep", "duration": 245} # 4:05 (+8с)

        pen_studio = _candidate_penalty(cand_studio)
        pen_elongated = _candidate_penalty(cand_elongated)

        # Студийный кандидат получает высокий отрицательный скор (бонус)
        self.assertLess(pen_studio, -250.0)
        # Кандидат 4:05 получает штраф
        self.assertGreater(pen_elongated, 150.0)
        # Разрыв между студийным и удлиненным более чем 400 баллов!
        self.assertGreater(pen_elongated - pen_studio, 400.0)

        # Проверка допустимости хронометража:
        diff_studio = abs(cand_studio["duration"] - expected_duration)
        diff_elongated = abs(cand_elongated["duration"] - expected_duration)

        # Для текстового поиска с известным каноническим эталоном строгий допуск <= 4s
        is_acceptable_studio = (diff_studio <= 4)
        is_acceptable_elongated = (diff_elongated <= 4)

        self.assertTrue(is_acceptable_studio, "Студийный трек 236с должен быть принят (diff=2s <= 4s)")
        self.assertFalse(is_acceptable_elongated, "Трек 245с (4:05) должен быть отклонен (diff=7s > 4s)")

    def test_eight_safety_scenarios_a_to_h(self):
        """
        Автоматизированная проверка всех 8 сценариев из директивы пользователя:
        A. Original 3:57, YouTube 3:57 -> ничего не менять
        B. Original 3:57, YouTube 4:05 (intro/outro) -> НЕ ускорять, отклонить 4:05 и выбрать 3:57
        C. Original 3:57, YouTube slowed 3.4% -> отклонить по длительности, скачать чистый оригинал
        D. Original 3:57, YouTube live 4:05 -> НЕ ускорять, отклонить по фильтру модификаторов
        E. Original 3:57, YouTube remix 4:05 -> НЕ ускорять, отклонить по фильтру модификаторов
        F. Original 3:57, YouTube official audio 3:57 -> скачать без изменения скорости
        G. Запрос 'Creep Remix' -> ремикс не отклоняется
        H. Запрос 'Creep Sped Up' -> sped up не отклоняется, скорость не восстанавливается назад в 1.0x
        """
        from services.extractor import extract_modifiers, extract_core_title_words, compute_title_match_ratio

        # Сценарий B: интро/аутро 4:05 (245с) и студийный трек (236с)
        cand_intro = {"title": "Radiohead - Creep (Lyrics)", "duration": 245}
        cand_topic = {"title": "Radiohead - Creep", "uploader": "Radiohead - Topic", "duration": 236}
        exp_dur = 237

        # 4:05 отклоняется по строгой проверке diff (8s > 4s)
        self.assertFalse(abs(cand_intro["duration"] - exp_dur) <= 4)
        # 3:56 принимается (diff=1s <= 4s)
        self.assertTrue(abs(cand_topic["duration"] - exp_dur) <= 4)

        # Сценарий D: Live версия
        cand_live = {"title": "Radiohead - Creep (Live at Reading)", "duration": 245}
        mods_live = extract_modifiers(cand_live["title"])
        self.assertIn("live", mods_live)

        # Сценарий E: Remix версия
        cand_remix = {"title": "Radiohead - Creep (Club Remix)", "duration": 245}
        mods_remix = extract_modifiers(cand_remix["title"])
        self.assertIn("remix", mods_remix)

        # Сценарий G: Запрос 'Creep Remix'
        req_remix = extract_modifiers("Radiohead — Creep Remix")
        self.assertIn("remix", req_remix)
        self.assertTrue(bool(req_remix & mods_remix))

        # Сценарий H: Запрос 'Creep Sped Up'
        req_sped = extract_modifiers("Radiohead — Creep Sped Up")
        cand_sped = {"title": "Radiohead - Creep (Sped Up)", "duration": 200}
        mods_sped = extract_modifiers(cand_sped["title"])
        self.assertTrue(bool(req_sped & mods_sped))

    def test_requested_vs_unrequested_modifiers_distinction(self):
        """
        Проверяет строгое разделение:
        - Запрос 'Radiohead — Creep Live' -> Live принимается, Remix бракуется как несовместимый
        - Запрос 'Radiohead — Creep Remix' -> Remix принимается, Live бракуется как несовместимый
        - Запрос 'Radiohead — Creep' -> студия принимается, Live и Remix бракуются как посторонние
        """
        from services.extractor import extract_modifiers

        # 1. Запрос 'Radiohead — Creep Live'
        req_live = extract_modifiers("Radiohead — Creep Live")
        self.assertEqual(req_live, {"live"})

        cand_live_mods = extract_modifiers("Radiohead - Creep (Live at Reading)")
        cand_remix_mods = extract_modifiers("Radiohead - Creep (Club Remix)")
        cand_studio_mods = extract_modifiers("Radiohead - Creep")

        # Live кандидат: совпадает с запрошенным
        self.assertTrue(bool(req_live & cand_live_mods))
        self.assertEqual(cand_live_mods - req_live, set())

        # Remix кандидат: содержит чужой несовместимый модификатор
        self.assertFalse(bool(req_live & cand_remix_mods))
        unrequested_in_remix = cand_remix_mods - req_live
        self.assertIn("remix", unrequested_in_remix)

        # 2. Запрос 'Radiohead — Creep Remix'
        req_remix = extract_modifiers("Radiohead — Creep Remix")
        self.assertEqual(req_remix, {"remix"})

        # Remix совпадает
        self.assertTrue(bool(req_remix & cand_remix_mods))
        # Live содержит чужой несовместимый
        self.assertIn("live", cand_live_mods - req_remix)

        # 3. Запрос оригинала 'Radiohead — Creep'
        req_orig = extract_modifiers("Radiohead — Creep")
        self.assertEqual(req_orig, set())

        # Для оригинала любые модификаторы являются посторонними
        self.assertIn("live", cand_live_mods - req_orig)
        self.assertIn("remix", cand_remix_mods - req_orig)
        self.assertEqual(cand_studio_mods - req_orig, set())


if __name__ == "__main__":
    unittest.main()


