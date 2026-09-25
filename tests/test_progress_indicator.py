import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from handlers.music import DownloadProgressUpdater, _execute_download_and_send
from services.extractor import ExtractedTrack


@pytest.mark.asyncio
async def test_progress_updater_throttling_interval_and_delta():
    """Тест 2: Проверяет соблюдение интервала троттлинга (1.2s) и минимальной дельты (5%)."""
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()

    updater = DownloadProgressUpdater(
        status_msg=status_msg,
        display_title="Test Artist — Test Title",
        platform_label="\nПлатформа: <b>Spotify</b>"
    )

    # Имитируем t0
    updater.last_edit_time = 0.0

    # 1. Первый апдейт на 10%
    updater.on_progress("downloading", 10)
    await asyncio.sleep(0.05)
    assert status_msg.edit_text.call_count == 1
    call1_text = status_msg.edit_text.call_args[0][0]
    assert "Прогресс: 10%" in call1_text

    # 2. Немедленный второй апдейт на 12% (дельта < 5% и времени прошло < 1.2s)
    # Должен быть проигнорирован
    updater.on_progress("downloading", 12)
    await asyncio.sleep(0.05)
    assert status_msg.edit_text.call_count == 1

    # 3. Апдейт на 30% через 0.1s (времени < 1.2s, хотя дельта 20%)
    # Должен быть отклонён из-за rate-limit по времени
    updater.on_progress("downloading", 30)
    await asyncio.sleep(0.05)
    assert status_msg.edit_text.call_count == 1

    # 4. Сдвигаем время на 1.3s вперед и отправляем 35%
    updater.last_edit_time = time.perf_counter() - 1.3
    updater.on_progress("downloading", 35)
    await asyncio.sleep(0.05)
    assert status_msg.edit_text.call_count == 2
    call2_text = status_msg.edit_text.call_args[0][0]
    assert "Прогресс: 35%" in call2_text

    updater.close()


@pytest.mark.asyncio
async def test_progress_updater_stages_transitions():
    """Тесты 1, 3, 4: Проверяет последовательность этапов (Загрузка -> Обработка аудио -> Отправляю трек)."""
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()

    updater = DownloadProgressUpdater(
        status_msg=status_msg,
        display_title="Queen — Bohemian Rhapsody",
        platform_label=""
    )
    updater.last_edit_time = 0.0

    # Загрузка
    updater.on_progress("downloading", 50)
    await asyncio.sleep(0.05)
    assert "Загрузка: <b>Queen — Bohemian Rhapsody</b>" in status_msg.edit_text.call_args[0][0]
    assert "Прогресс: 50%" in status_msg.edit_text.call_args[0][0]

    # Завершение загрузки (100%)
    updater.last_edit_time = 0.0
    updater.on_progress("downloading", 100)
    await asyncio.sleep(0.05)
    assert "Прогресс: 100%" in status_msg.edit_text.call_args[0][0]

    # Обработка аудио (FFmpeg)
    updater.last_edit_time = 0.0
    updater.on_progress("processing", 100)
    await asyncio.sleep(0.05)
    assert "Обработка аудио: <b>Queen — Bohemian Rhapsody</b>" in status_msg.edit_text.call_args[0][0]

    updater.close()


@pytest.mark.asyncio
async def test_progress_updater_error_resilience():
    """Тест 5: Проверяет, что ошибки Telegram edit_text (FloodWait, NotModified и др.) перехватываются без исключений."""
    status_msg = MagicMock()
    # edit_text бросает исключение
    status_msg.edit_text = AsyncMock(side_effect=Exception("TelegramBadRequest: message is not modified"))

    updater = DownloadProgressUpdater(
        status_msg=status_msg,
        display_title="Test — Error",
        platform_label=""
    )
    updater.last_edit_time = 0.0

    # Не должно бросать исключений
    updater.on_progress("downloading", 20)
    await asyncio.sleep(0.05)

    updater.on_progress("processing", 100)
    await asyncio.sleep(0.05)

    updater.close()


@pytest.mark.asyncio
async def test_progress_updater_isolation():
    """Тест 6: Проверяет полную изоляцию состояний двух разных параллельных запросов."""
    msg1 = MagicMock()
    msg1.edit_text = AsyncMock()
    msg2 = MagicMock()
    msg2.edit_text = AsyncMock()

    u1 = DownloadProgressUpdater(msg1, "Track 1", "")
    u2 = DownloadProgressUpdater(msg2, "Track 2", "")
    u1.last_edit_time = 0.0
    u2.last_edit_time = 0.0

    u1.on_progress("downloading", 25)
    u2.on_progress("downloading", 60)
    await asyncio.sleep(0.05)

    assert "Прогресс: 25%" in msg1.edit_text.call_args[0][0]
    assert "Прогресс: 60%" in msg2.edit_text.call_args[0][0]

    u1.close()
    u2.close()


@pytest.mark.asyncio
async def test_full_pipeline_progress_and_sending():
    """Интеграционный тест: полный пайплайн от Search -> Download -> Processing -> Sending."""
    message = MagicMock()
    message.from_user = MagicMock(id=111, username="test", full_name="Test")
    message.chat = MagicMock(id=111)
    message.bot = MagicMock()

    reply_msg = MagicMock()
    reply_msg.delete = AsyncMock()
    reply_msg.edit_text = AsyncMock()
    message.reply = AsyncMock(return_value=reply_msg)

    sent_audio_msg = MagicMock()
    sent_audio_msg.audio = MagicMock(file_id="FILE_OK")
    message.answer_audio = AsyncMock(return_value=sent_audio_msg)

    mock_track = ExtractedTrack(
        platform="Spotify",
        target="ytsearch5:Artist - Song",
        is_search=True,
        title="Song",
        artist="Artist",
        thumbnail_url=None,
        duration=180
    )

    async def mock_dl(*args, **kwargs):
        cb = kwargs.get("progress_callback")
        if cb:
            cb("downloading", 20)
            cb("downloading", 100)
            cb("processing", 100)
        audio = MagicMock()
        audio.title = "Song"
        audio.artist = "Artist"
        audio.duration = 180
        audio.filesize = 5000000
        audio.file_path = MagicMock()
        audio.thumbnail_path = None
        audio.album = None
        audio.perf_timings = {}
        audio.cleanup = MagicMock()
        return audio

    with patch("handlers.music.resolve_track_url", new=AsyncMock(return_value=mock_track)), \
         patch("handlers.music.download_track", side_effect=mock_dl), \
         patch("handlers.music.get_cached_track_async", new=AsyncMock(return_value=None)), \
         patch("handlers.music.save_cached_track_async", new=AsyncMock()):

        await _execute_download_and_send(
            message=message,
            raw_query="https://open.spotify.com/track/123",
            url="https://open.spotify.com/track/123",
            variant="original"
        )

    # Проверяем последовательность вызовов edit_text
    texts = [c.args[0] for c in reply_msg.edit_text.call_args_list if c.args]
    # Должен быть вызов "Загрузка:"
    assert any("Загрузка:" in t for t in texts)
    # Должен быть вызов "Отправляю трек..."
    assert any("Отправляю трек..." in t for t in texts)
    # Должен быть вызван answer_audio
    assert message.answer_audio.call_count == 1
    # Должен быть вызван delete статуса после отправки
    assert reply_msg.delete.call_count == 1
