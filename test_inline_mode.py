import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineQuery, User, InlineQueryResultCachedAudio, InlineQueryResultArticle
from handlers.inline import handle_inline_query, _upload_audio_for_file_id
from services.downloader import DownloadedAudio
from services.database import save_cached_track_async, init_db


class TestInlineModeV255(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        init_db()

    @patch.object(InlineQuery, "answer", new_callable=AsyncMock)
    async def test_empty_query_hint_cache_time_1(self, mock_answer):
        user = User(id=12345, is_bot=False, first_name="TestUser", username="testuser")
        iq = InlineQuery(id="iq_empty", from_user=user, query="", offset="", chat_type="group")

        await handle_inline_query(iq)

        mock_answer.assert_awaited_once()
        args, kwargs = mock_answer.await_args
        results = args[0]
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], InlineQueryResultArticle)
        self.assertEqual(results[0].id, "hint_empty_query")
        self.assertEqual(kwargs.get("cache_time"), 1)

    @patch.object(InlineQuery, "answer", new_callable=AsyncMock)
    async def test_cached_track_returns_pure_audio_instant(self, mock_answer):
        user = User(id=99999, is_bot=False, first_name="MusicFan", username="musicfan")
        iq = InlineQuery(id="iq_cached", from_user=user, query="Kavinsky Nightcall", offset="", chat_type="supergroup")

        await save_cached_track_async(
            query="Kavinsky Nightcall",
            file_id="CQACAgIAAxkBAAI_kavinsky123",
            title="Nightcall",
            artist="Kavinsky",
            duration=259
        )

        await handle_inline_query(iq)

        mock_answer.assert_awaited_once()
        args, kwargs = mock_answer.await_args
        results = args[0]
        self.assertGreaterEqual(len(results), 1)
        self.assertIsInstance(results[0], InlineQueryResultCachedAudio)
        self.assertEqual(results[0].audio_file_id, "CQACAgIAAxkBAAI_kavinsky123")
        self.assertIsNone(results[0].reply_markup)

    async def test_upload_never_touches_user_id_and_deletes_admin_buffer(self):
        bot = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 777
        mock_msg.audio = MagicMock()
        mock_msg.audio.file_id = "BUFFER_FILE_ID_999"
        bot.send_audio = AsyncMock(return_value=mock_msg)
        bot.delete_message = AsyncMock()

        fake_audio = DownloadedAudio(
            folder_path="downloads/fake",
            file_path="fake_song.mp3",
            title="Song",
            artist="Artist",
            duration=180,
            filesize=5000000,
            thumbnail_path=None
        )

        with patch("handlers.inline.STORAGE_CHANNEL_ID", None), \
             patch("handlers.inline.ADMIN_ID", 6874119454):
            file_id = await _upload_audio_for_file_id(bot, fake_audio)

            self.assertEqual(file_id, "BUFFER_FILE_ID_999")
            bot.send_audio.assert_awaited_once()
            call_kwargs = bot.send_audio.await_args.kwargs
            self.assertEqual(call_kwargs["chat_id"], 6874119454)
            self.assertTrue(call_kwargs.get("disable_notification"))
            bot.delete_message.assert_awaited_once_with(chat_id=6874119454, message_id=777)

    async def test_upload_with_storage_channel(self):
        bot = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 888
        mock_msg.audio = MagicMock()
        mock_msg.audio.file_id = "CHANNEL_FILE_ID_111"
        bot.send_audio = AsyncMock(return_value=mock_msg)
        bot.delete_message = AsyncMock()

        fake_audio = DownloadedAudio(
            folder_path="downloads/fake",
            file_path="fake_song.mp3",
            title="Channel Song",
            artist="Channel Artist",
            duration=200,
            filesize=6000000,
            thumbnail_path=None
        )

        with patch("handlers.inline.STORAGE_CHANNEL_ID", "-1001234567890"), \
             patch("handlers.inline.ADMIN_ID", 6874119454):
            file_id = await _upload_audio_for_file_id(bot, fake_audio)

            self.assertEqual(file_id, "CHANNEL_FILE_ID_111")
            bot.send_audio.assert_awaited_once()
            call_kwargs = bot.send_audio.await_args.kwargs
            self.assertEqual(call_kwargs["chat_id"], "-1001234567890")
            bot.delete_message.assert_not_awaited()

    @patch.object(InlineQuery, "answer", new_callable=AsyncMock)
    async def test_uncached_timeout_returns_pending_and_caches_in_background(self, mock_answer):
        bot = AsyncMock()
        user = User(id=55555, is_bot=False, first_name="LateUser", username="lateuser")
        iq = InlineQuery(id="iq_timeout", from_user=user, query="Rare Long Track 1234", offset="", chat_type="group")
        # Attach bot mock to inline query
        iq._bot = bot

        # Simulate slow download that exceeds the 4.5s client window
        async def slow_download_and_cache(cache_key, raw_query, url, b):
            await asyncio.sleep(0.2)
            await save_cached_track_async("Rare Long Track 1234", "FILE_ID_RARE_999", "Rare Long Track", "Artist", 120)
            return "FILE_ID_RARE_999"

        with patch("handlers.inline._download_and_cache", side_effect=slow_download_and_cache), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            await handle_inline_query(iq)

        mock_answer.assert_awaited_once()
        args, kwargs = mock_answer.await_args
        results = args[0]
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], InlineQueryResultArticle)
        self.assertTrue(results[0].id.startswith("pending_"))
        self.assertEqual(kwargs.get("cache_time"), 2)
        self.assertIn("Скачать", kwargs.get("switch_pm_text", ""))
        self.assertEqual(kwargs.get("switch_pm_parameter"), "search")
        # Ensure bot.send_audio was NEVER called with user.id
        bot.send_audio.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
