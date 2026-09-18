import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from services.extractor import (
    resolve_track_url,
    UnsupportedUrlError,
    UNSUPPORTED_URL_FALLBACK_TEXT,
)
from handlers.inline import (
    _parse_candidate_title_artist,
    handle_inline_query,
    handle_inline_download,
    _in_flight_downloads,
    _in_flight_lock,
)
from services.database import (
    init_db,
    save_inline_candidate,
    get_inline_candidate,
    get_inline_candidate_async,
    search_cached_tracks,
    save_cached_track,
)
import config


# ============================================================================
# 1. ТЕСТЫ ДЛЯ УНИВЕРСАЛЬНОГО FALLBACK НЕПОДДЕРЖИВАЕМЫХ ССЫЛОК И VK
# ============================================================================

@pytest.mark.asyncio
async def test_unsupported_domains_raise_unsupported_url_error():
    """Проверяет, что любые сторонние / неизвестные домены вызывают UnsupportedUrlError."""
    unsupported_urls = [
        "https://example.com/music/test.mp3",
        "https://instagram.com/p/C12345678",
        "https://twitter.com/artist/status/123456789",
        "https://x.com/artist/status/123456789",
        "https://rutube.ru/video/123456789",
        "https://vimeo.com/123456789",
    ]
    with patch("services.extractor.is_safe_url", return_value=(True, "")), \
         patch("services.extractor._unshorten_url", side_effect=lambda u, s=None: u):
        for u in unsupported_urls:
            with pytest.raises(UnsupportedUrlError) as exc_info:
                await resolve_track_url(u)
            assert str(exc_info.value) == UNSUPPORTED_URL_FALLBACK_TEXT
            assert "⚠️ Не удалось распознать эту ссылку" in str(exc_info.value)
            assert "💡 Отправьте название трека или исполнителя текстом — я найду его на YouTube" in str(exc_info.value)


@pytest.mark.asyncio
async def test_vk_audio_without_token_raises_friendly_fallback():
    """
    Проверяет, что при отсутствии VK_TOKEN или ошибке VK API
    пользователю НЕ выдаются технические ошибки (VK_TOKEN, audio.getById, error 15),
    а возвращается дружелюбный универсальный fallback.
    """
    vk_url = "https://vk.com/audio-2001878815_33878815"
    with patch.object(config, "VK_TOKEN", None):
        with pytest.raises(UnsupportedUrlError) as exc_info:
            await resolve_track_url(vk_url)

        msg = str(exc_info.value)
        assert msg == UNSUPPORTED_URL_FALLBACK_TEXT
        assert "VK_TOKEN" not in msg
        assert "audio.getById" not in msg
        assert "error 15" not in msg
        assert "OAuth" not in msg
        assert "⚠️ Не удалось распознать эту ссылку" in msg


@pytest.mark.asyncio
async def test_vk_non_audio_non_video_raises_unsupported():
    """VK-ссылки на профили/стены/группы отсекаются как неподдерживаемые."""
    with pytest.raises(UnsupportedUrlError):
        await resolve_track_url("https://vk.com/wall-12345_6789")


# ============================================================================
# 2. ТЕСТЫ ДЛЯ CANDIDATE PARSER & INLINE DATABASE
# ============================================================================

def test_parse_candidate_title_artist():
    """Проверяет корректное разделение исполнителя и названия из YouTube-заголовков."""
    # 1. "Artist - Title"
    a1, t1 = _parse_candidate_title_artist("The Weeknd - Blinding Lights (Official Video)", "The Weeknd")
    assert a1 == "The Weeknd"
    assert t1 == "Blinding Lights"

    # 2. "Artist — Title" (длинное тире)
    a2, t2 = _parse_candidate_title_artist("MiyaGi & Эндшпиль — Captain", "MiyaGi")
    assert "MiyaGi" in a2
    assert "Captain" in t2

    # 3. Без тире с Topic-каналом
    a3, t3 = _parse_candidate_title_artist("Starboy", "The Weeknd - Topic")
    assert a3 == "The Weeknd"
    assert t3 == "Starboy"


def test_inline_candidate_db_operations():
    """Проверяет сохранение и извлечение кандидатов inline-поиска."""
    init_db()
    save_inline_candidate(
        cand_id="test_cand_1",
        target="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        artist="Rick Astley",
        album="Whenever You Need Somebody",
        duration=213,
        thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    )

    cand = get_inline_candidate("test_cand_1")
    assert cand is not None
    assert cand["title"] == "Never Gonna Give You Up"
    assert cand["artist"] == "Rick Astley"
    assert cand["duration"] == 213


def test_search_cached_tracks():
    """Проверяет поиск по кэшированным трекам в SQLite."""
    init_db()
    save_cached_track(
        query="queen - bohemian rhapsody",
        file_id="tg_file_id_queen_123",
        title="Bohemian Rhapsody",
        artist="Queen",
        duration=354,
        variant="original"
    )

    results = search_cached_tracks("bohemian", limit=5)
    assert len(results) >= 1
    matched = next((r for r in results if r["file_id"] == "tg_file_id_queen_123"), None)
    assert matched is not None
    assert matched["title"] == "Bohemian Rhapsody"
    assert matched["artist"] == "Queen"


# ============================================================================
# 3. ТЕСТЫ ДЛЯ INLINE QUERY HANDLER
# ============================================================================

@pytest.mark.asyncio
async def test_inline_query_empty_returns_prompt():
    """Пустой запрос в inline возвращает обучающую подсказку."""
    mock_query = MagicMock()
    mock_query.query = "   "
    mock_query.answer = AsyncMock()

    await handle_inline_query(mock_query)

    assert mock_query.answer.called
    call_args = mock_query.answer.call_args
    results = call_args.kwargs.get("results") or call_args.args[0]
    assert len(results) == 1
    prompt = results[0]
    assert prompt.id == "empty_query_prompt"
    assert "Введите исполнителя и название" in prompt.title


@pytest.mark.asyncio
async def test_inline_query_never_downloads_audio():
    """
    КРИТИЧЕСКИЙ ТЕСТ: убеждаемся, что при inline search
    ни при каких обстоятельствах НЕ вызывается download_track.
    """
    mock_query = MagicMock()
    mock_query.query = "Daft Punk Get Lucky"
    mock_query.answer = AsyncMock()

    fake_items = [
        MagicMock(
            title="Daft Punk - Get Lucky (Official Audio)",
            uploader="Daft Punk",
            duration=248,
            formatted_duration="4:08",
            url="https://www.youtube.com/watch?v=5NV6Rdv1a3I",
            thumbnail="https://i.ytimg.com/vi/thumb.jpg"
        )
    ]

    with patch("handlers.inline.search_tracks_async", return_value=(fake_items, None)), \
         patch("handlers.inline.download_track") as mock_dl:

        await handle_inline_query(mock_query)

        # download_track НЕ должен вызываться во время inline query!
        assert not mock_dl.called
        assert mock_query.answer.called

        call_args = mock_query.answer.call_args
        results = call_args.kwargs.get("results") or call_args.args[0]
        assert len(results) >= 1
        art = results[0]
        assert "Get Lucky" in art.title
        assert "Daft Punk" in art.description
        assert art.reply_markup is not None
        btn = art.reply_markup.inline_keyboard[0][0]
        assert btn.callback_data.startswith("inldl:")


# ============================================================================
# 4. ТЕСТЫ ДЛЯ INLINE DOWNLOAD & ДЕДУПЛИКАЦИИ
# ============================================================================

@pytest.mark.asyncio
async def test_inline_download_deduplication():
    """
    Проверяет защиту от дублирующих загрузок:
    Множественные быстрые клики по одной и той же кнопке вызывают download_track ровно 1 раз.
    """
    init_db()
    cand_id = "dedup_test_cand"
    save_inline_candidate(
        cand_id=cand_id,
        target="https://www.youtube.com/watch?v=deduptest",
        title="Dedup Track",
        artist="Dedup Artist",
        album=None,
        duration=180,
        thumbnail_url=None
    )

    mock_downloaded = MagicMock()
    mock_downloaded.title = "Dedup Track"
    mock_downloaded.artist = "Dedup Artist"
    mock_downloaded.file_path = MagicMock()
    mock_downloaded.file_path.exists.return_value = True
    mock_downloaded.cleanup = MagicMock()
    mock_downloaded.thumbnail_path = None

    # Имитируем медленную загрузку 0.2с
    dl_call_count = 0
    async def slow_download(*args, **kwargs):
        nonlocal dl_call_count
        dl_call_count += 1
        await asyncio.sleep(0.2)
        return mock_downloaded

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.audio.file_id = "file_id_dedup_123"
    mock_bot.send_audio = AsyncMock(return_value=mock_msg)

    # Клиент 1 (первый клик)
    cb1 = MagicMock()
    cb1.data = f"inldl:{cand_id}"
    cb1.inline_message_id = "inline_msg_123"
    cb1.message = None
    cb1.from_user.id = 111111
    cb1.answer = AsyncMock()

    # Клиент 2 (быстрый повторный клик в той же группе)
    cb2 = MagicMock()
    cb2.data = f"inldl:{cand_id}"
    cb2.inline_message_id = "inline_msg_123"
    cb2.message = None
    cb2.from_user.id = 222222
    cb2.answer = AsyncMock()

    with patch("handlers.inline.download_track", side_effect=slow_download):
        # Запускаем два параллельных клика одновременно
        task1 = asyncio.create_task(handle_inline_download(cb1, mock_bot))
        await asyncio.sleep(0.02) # Небольшая фора первому клику
        task2 = asyncio.create_task(handle_inline_download(cb2, mock_bot))

        await asyncio.gather(task1, task2)

        # download_track должен быть вызван РОВНО 1 раз!
        assert dl_call_count == 1
        # Второму клику должно быть показано предупреждение об уже идущей загрузке
        cb2.answer.assert_called_with("⏳ Этот трек уже скачивается, пожалуйста подождите...", show_alert=False)


# ============================================================================
# 5. ДОПОЛНИТЕЛЬНЫЕ ТЕСТЫ ДИАГНОСТИКИ, СЕРИАЛИЗАЦИИ И ВАЛИДАЦИИ FILE_ID
# ============================================================================

@pytest.mark.asyncio
async def test_diagnostic_test_query():
    """Запрос 'test' возвращает диагностическую статью '🎵 Тест Inline'."""
    mock_query = MagicMock()
    mock_query.query = "test"
    mock_query.answer = AsyncMock()

    await handle_inline_query(mock_query)

    assert mock_query.answer.called
    call_args = mock_query.answer.call_args
    results = call_args.kwargs.get("results") or call_args.args[0]
    assert len(results) == 1
    art = results[0]
    assert art.id == "diag_test_ok"
    assert "Тест Inline" in art.title
    assert "Inline Mode работает" in art.description


def test_is_valid_telegram_file_id():
    """Проверяет валидатор Telegram audio file_id."""
    from handlers.inline import is_valid_telegram_file_id

    # Невалидные: mock, url, path, слишком короткие, пробелы
    assert is_valid_telegram_file_id(None) is False
    assert is_valid_telegram_file_id("") is False
    assert is_valid_telegram_file_id("mock_file_id_12345") is False
    assert is_valid_telegram_file_id("test_file_id") is False
    assert is_valid_telegram_file_id("https://youtube.com/watch?v=123") is False
    assert is_valid_telegram_file_id("/tmp/audio.mp3") is False
    assert is_valid_telegram_file_id("C:\\downloads\\song.mp3") is False
    assert is_valid_telegram_file_id("short_id") is False
    assert is_valid_telegram_file_id("has space inside here") is False

    # Валидные реальные base64url Telegram file_id
    assert is_valid_telegram_file_id("CQACAgIAAxkBAAIBZ2f1234567890abcdefghijklmnopqrstuvwxyz") is True
    assert is_valid_telegram_file_id("BQACAgQAAxkBAAICaW_fake_valid_length_telegram_file_id_123456") is True


@pytest.mark.asyncio
async def test_cached_audio_only_used_for_valid_file_id():
    """Убеждаемся, что фиктивные mock_file_id из кэша НЕ превращаются в InlineQueryResultCachedAudio."""
    mock_query = MagicMock()
    mock_query.query = "some_mock_cached_song"
    mock_query.answer = AsyncMock()

    # Имитируем запись в кэше с невалидным mock_file_id
    fake_cached = [{"file_id": "mock_file_id_invalid", "artist": "A", "title": "T"}]

    with patch("handlers.inline.search_cached_tracks_async", return_value=fake_cached), \
         patch("handlers.inline.search_tracks_async", return_value=([], None)):

        await handle_inline_query(mock_query)

        call_args = mock_query.answer.call_args
        results = call_args.kwargs.get("results") or call_args.args[0]
        # Не должно быть InlineQueryResultCachedAudio с mock_file_id!
        for r in results:
            assert getattr(r, "type", "") != "audio"


@pytest.mark.asyncio
async def test_search_failure_produces_empty_results_ux_not_exception():
    """При падении поиска YouTube/SoundCloud выдается карточка 'Ничего не найдено', а не падение."""
    mock_query = MagicMock()
    mock_query.query = "Broken Search"
    mock_query.answer = AsyncMock()

    with patch("handlers.inline.search_cached_tracks_async", return_value=[]), \
         patch("handlers.inline.search_tracks_async", side_effect=RuntimeError("Search network down")):

        await handle_inline_query(mock_query)

        assert mock_query.answer.called
        call_args = mock_query.answer.call_args
        results = call_args.kwargs.get("results") or call_args.args[0]
        assert len(results) == 1
        assert results[0].id == "no_results_found"
        assert "Ничего не найдено" in results[0].title


@pytest.mark.asyncio
async def test_chosen_inline_result_handler():
    """Проверяет обработчик chosen_inline_result."""
    from handlers.inline import handle_chosen_inline_result
    from aiogram.types import ChosenInlineResult

    chosen = MagicMock(spec=ChosenInlineResult)
    chosen.result_id = "art_123"
    chosen.from_user = MagicMock(id=999)
    chosen.query = "test query"
    chosen.inline_message_id = "inl_msg_999"

    # Не должен падать
    await handle_chosen_inline_result(chosen)

