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
    process_inline_download,
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
        assert btn.callback_data.startswith("inl_status:")
        assert btn.text == "⏳ Подготавливается..."


# ============================================================================
# 4. ТЕСТЫ ДЛЯ INLINE DOWNLOAD & ДЕДУПЛИКАЦИИ
# ============================================================================

@pytest.mark.asyncio
async def test_inline_download_deduplication():
    """
    Проверяет защиту от дублирующих загрузок:
    Параллельный выбор одного и того же трека вызывает download_track ровно 1 раз,
    а оба inline-сообщения получают готовый file_id через edit_message_media.
    """
    from handlers.inline import process_inline_download
    import uuid
    init_db()
    unique_suffix = uuid.uuid4().hex[:8]
    cand_id = f"dedup_{unique_suffix}"
    target_url = f"https://www.youtube.com/watch?v=dedup_{unique_suffix}"
    title_str = f"Dedup Track {unique_suffix}"
    artist_str = f"Dedup Artist {unique_suffix}"

    save_inline_candidate(
        cand_id=cand_id,
        target=target_url,
        title=title_str,
        artist=artist_str,
        album=None,
        duration=180,
        thumbnail_url=None
    )

    mock_downloaded = MagicMock()
    mock_downloaded.title = title_str
    mock_downloaded.artist = artist_str
    mock_downloaded.duration = 180
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
    mock_msg.audio.file_id = "CQACAgIAAxkBAAIBZ2f1234567890abcdefghijklmnopqrstuvwxyz"
    mock_msg.message_id = 777
    mock_bot.send_audio = AsyncMock(return_value=mock_msg)
    mock_bot.delete_message = AsyncMock()

    with patch("handlers.inline.STORAGE_CHANNEL_ID", -1001234567890), \
         patch("handlers.inline.download_track", side_effect=slow_download):
        task1 = asyncio.create_task(
            process_inline_download(
                cand_id=cand_id,
                inline_message_id="inline_msg_1",
                bot=mock_bot
            )
        )
        await asyncio.sleep(0.02) # Небольшая фора первому запросу
        task2 = asyncio.create_task(
            process_inline_download(
                cand_id=cand_id,
                inline_message_id="inline_msg_2",
                bot=mock_bot
            )
        )

        res1, res2 = await asyncio.gather(task1, task2)

        # download_track должен быть вызван РОВНО 1 раз!
        assert dl_call_count == 1
        assert res1 == "CQACAgIAAxkBAAIBZ2f1234567890abcdefghijklmnopqrstuvwxyz"
        assert res2 == "CQACAgIAAxkBAAIBZ2f1234567890abcdefghijklmnopqrstuvwxyz"

        # Проверяем, что edit_message_media вызван для обоих сообщений
        edited_inline_ids = [call.kwargs.get("inline_message_id") for call in mock_bot.edit_message_media.call_args_list]
        assert "inline_msg_1" in edited_inline_ids
        assert "inline_msg_2" in edited_inline_ids


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
async def test_chosen_inline_result_triggers_download_task():
    """
    Проверяет, что ChosenInlineResult запускает фоновую загрузку,
    редактирует inline-сообщение и НЕ отправляет аудио в ЛС пользователю.
    """
    from handlers.inline import handle_chosen_inline_result
    from aiogram.types import ChosenInlineResult

    import uuid
    unique_suffix = uuid.uuid4().hex[:8]
    cand_id = f"chosen_{unique_suffix}"
    target_url = f"https://www.youtube.com/watch?v=chosen_{unique_suffix}"
    title_str = f"Chosen Track {unique_suffix}"
    artist_str = f"Chosen Artist {unique_suffix}"

    save_inline_candidate(
        cand_id=cand_id,
        target=target_url,
        title=title_str,
        artist=artist_str,
        album=None,
        duration=200,
        thumbnail_url=None
    )

    chosen = MagicMock(spec=ChosenInlineResult)
    chosen.result_id = f"art_{cand_id}"
    chosen.from_user = MagicMock(id=987654321)
    chosen.query = f"{artist_str} {title_str}"
    chosen.inline_message_id = "inl_msg_chosen_123"

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.audio.file_id = "BQACAgQAAxkBAAICaW_valid_length_telegram_file_id_123456"
    mock_msg.message_id = 888
    mock_bot.send_audio = AsyncMock(return_value=mock_msg)
    mock_bot.delete_message = AsyncMock()

    mock_dl = MagicMock()
    mock_dl.title = title_str
    mock_dl.artist = artist_str
    mock_dl.duration = 200
    mock_dl.file_path = MagicMock()
    mock_dl.file_path.exists.return_value = True
    mock_dl.cleanup = MagicMock()
    mock_dl.thumbnail_path = None

    with patch("handlers.inline.STORAGE_CHANNEL_ID", -1001234567890), \
         patch("handlers.inline.download_track", AsyncMock(return_value=mock_dl)) as dl_mock, \
         patch("handlers.inline.resolve_canonical_track_info_async", AsyncMock(return_value=None)):
        await handle_chosen_inline_result(chosen, mock_bot)
        # Даем фоновой задаче выполниться
        await asyncio.sleep(0.1)

        # 1. download_track был вызван ровно 1 раз
        assert dl_mock.called

        # 2. КРИТИЧНО: bot.send_audio отправлял ТОЛЬКО в STORAGE_CHANNEL_ID, не в ЛС пользователя и не в ADMIN_ID
        assert mock_bot.send_audio.called
        for call in mock_bot.send_audio.call_args_list:
            chat_id = call.kwargs.get("chat_id") or call.args[0]
            assert chat_id == -1001234567890, "Должен использоваться только STORAGE_CHANNEL_ID!"
            assert chat_id != 987654321, "Запрещено отправлять аудио в ЛС пользователя!"
            assert chat_id != 12345678, "Запрещено отправлять аудио администратору!"

        # 3. Сообщение в чате было отредактировано через edit_message_media
        assert mock_bot.edit_message_media.called
        call_media = mock_bot.edit_message_media.call_args
        assert call_media.kwargs.get("inline_message_id") == "inl_msg_chosen_123"
        media_obj = call_media.kwargs.get("media")
        assert media_obj.media == "BQACAgQAAxkBAAICaW_valid_length_telegram_file_id_123456"


@pytest.mark.asyncio
async def test_inline_query_with_unsupported_url():
    """Проверяет, что при вводе неподдерживаемой ссылки в inline поиске возвращается понятная подсказка без кнопок."""
    mock_query = MagicMock()
    mock_query.query = "https://unknown.domain/track/123"
    mock_query.answer = AsyncMock()

    with patch("handlers.inline.resolve_track_url", side_effect=UnsupportedUrlError(UNSUPPORTED_URL_FALLBACK_TEXT)):
        await handle_inline_query(mock_query)

        assert mock_query.answer.called
        call_args = mock_query.answer.call_args
        results = call_args.kwargs.get("results") or call_args.args[0]
        assert len(results) == 1
        art = results[0]
        assert art.id == "unsupported_url_result"
        assert "Не удалось распознать" in art.title
        assert "Отправьте название трека" in art.description
        assert art.reply_markup is None # Не содержит кнопок скачивания!


# ============================================================================
# 4. ТЕСТЫ ДЛЯ УЛУЧШЕННОГО INLINE SEARCH, SCORING И STORAGE ИЗОЛЯЦИИ
# ============================================================================

from services.search import (
    SearchItem,
    detect_is_live,
    _score_inline_candidate,
    search_inline_tracks_sync,
)


def test_detect_is_live_accurate_and_safe():
    """
    Проверяет точное распознавание живых записей и концертов,
    включая паттерны площадок вроде (Sentrum, Kiev),
    без ложных срабатываний на треки с городами в названиях.
    """
    # 1. Живые записи должны детектироваться как live
    assert detect_is_live("Пошлая Молли — Даже моя бэйби не знает (Sentrum, Kiev)") is True
    assert detect_is_live("Пошлая Молли — Даже моя бэйби не знает (Live @ Sentrum)") is True
    assert detect_is_live("Пошлая Молли — Даже моя бэйби не знает [LIVE]") is True
    assert detect_is_live("Пошлая Молли - Даже моя бэйби не знает (концерт в клубе)") is True
    assert detect_is_live("Radiohead - Creep (Live at Glastonbury)") is True
    assert detect_is_live("Track Title (Moscow, 2021 live)") is True

    # 2. Обычные названия треков с городами НЕ должны считаться live
    assert detect_is_live("Midnight in Paris") is False
    assert detect_is_live("London Calling") is False
    assert detect_is_live("Walking in Memphis") is False
    assert detect_is_live("Moscow Calling") is False
    assert detect_is_live("Пошлая Молли - Даже моя бэйби не знает") is False


def test_candidate_scoring_prefers_studio_over_live_and_sentrum():
    """
    Студийный оригинал всегда побеждает live-версии и записи с концертов (Sentrum, Kiev).
    """
    item_studio = SearchItem(
        index=0,
        title="Даже моя бэйби не знает",
        uploader="Пошлая Молли",
        duration=208,
        url="https://www.youtube.com/watch?v=Lp_euDcDQ40",
        source="ytmusic"
    )
    item_sentrum = SearchItem(
        index=0,
        title="Пошлая Молли — Даже моя бэйби не знает (Sentrum, Kiev)",
        uploader="Concert Fan",
        duration=215,
        url="https://www.youtube.com/watch?v=ogeP8z5lwYo",
        source="youtube"
    )
    item_live = SearchItem(
        index=0,
        title="Пошлая Молли - Даже моя бэйби не знает [ LIVE ]",
        uploader="kvvalerka",
        duration=220,
        url="https://www.youtube.com/watch?v=IS_kxmuLZGM",
        source="youtube"
    )

    score_studio = _score_inline_candidate(
        item_studio,
        query_artist="Пошлая Молли",
        query_title="Даже моя бэйби не знает",
        requested_modifiers=set()
    )
    score_sentrum = _score_inline_candidate(
        item_sentrum,
        query_artist="Пошлая Молли",
        query_title="Даже моя бэйби не знает",
        requested_modifiers=set()
    )
    score_live = _score_inline_candidate(
        item_live,
        query_artist="Пошлая Молли",
        query_title="Даже моя бэйби не знает",
        requested_modifiers=set()
    )

    assert score_studio > 1000.0, "Студийный трек из YT Music должен получить высокий положительный балл"
    assert score_sentrum < 0.0, "Запись (Sentrum, Kiev) должна получить штраф и отрицательный балл"
    assert score_live < 0.0, "Live-версия должна получить штраф и отрицательный балл"
    assert score_studio > score_sentrum
    assert score_studio > score_live


def test_candidate_scoring_honors_live_modifier_when_requested():
    """
    Если пользователь явно запросил live, live-версия поощряется и побеждает студийную.
    """
    item_studio = SearchItem(
        index=0,
        title="Даже моя бэйби не знает",
        uploader="Пошлая Молли",
        duration=208,
        url="https://www.youtube.com/watch?v=Lp_euDcDQ40",
        source="ytmusic"
    )
    item_live = SearchItem(
        index=0,
        title="Пошлая Молли - Даже моя бэйби не знает (Live)",
        uploader="kvvalerka",
        duration=220,
        url="https://www.youtube.com/watch?v=IS_kxmuLZGM",
        source="youtube"
    )

    requested = {"live"}
    score_studio = _score_inline_candidate(
        item_studio,
        query_artist="Пошлая Молли",
        query_title="Даже моя бэйби не знает",
        requested_modifiers=requested
    )
    score_live = _score_inline_candidate(
        item_live,
        query_artist="Пошлая Молли",
        query_title="Даже моя бэйби не знает",
        requested_modifiers=requested
    )

    assert score_live > score_studio, "При запросе 'live' живая версия должна побеждать студийную"
    assert score_live > 1000.0


def test_inline_search_ytm_primary_and_fallback_logic():
    """
    Проверяет, что search_inline_tracks_sync использует ytmsearch как primary,
    и вызывает ytsearch/scsearch ТОЛЬКО при недостатке подходящих результатов.
    """
    # 1. Сценарий: YTM вернул качественные результаты -> fallback yt/sc НЕ вызывается
    ytm_item = SearchItem(
        index=0,
        title="Даже моя бэйби не знает",
        uploader="Пошлая Молли",
        duration=208,
        url="https://www.youtube.com/watch?v=Lp_euDcDQ40",
        source="ytmusic"
    )

    called_sources = []
    def mock_extract(src, query, limit):
        called_sources.append(src)
        if src == "ytm":
            return [ytm_item, ytm_item, ytm_item]
        return []

    with patch("services.search._extract_source_items", side_effect=mock_extract):
        items, _ = search_inline_tracks_sync("Пошлая Молли - Даже моя бэйби не знает", limit=3)
        assert len(items) >= 1
        assert "ytm" in called_sources
        assert "yt" not in called_sources, "Fallback к обычному YouTube не должен вызываться при успешном YTM"

    # 2. Сценарий: YTM вернул 0 результатов -> fallback yt и sc вызываются
    called_sources.clear()
    yt_fallback_item = SearchItem(
        index=0,
        title="Пошлая Молли - Даже моя бэйби не знает",
        uploader="Пошлая Молли - Topic",
        duration=208,
        url="https://www.youtube.com/watch?v=fallback_yt_123",
        source="youtube"
    )
    def mock_extract_empty_ytm(src, query, limit):
        called_sources.append(src)
        if src == "ytm":
            return []
        elif src == "yt":
            return [yt_fallback_item]
        return []

    with patch("services.search._extract_source_items", side_effect=mock_extract_empty_ytm):
        items, _ = search_inline_tracks_sync("Пошлая Молли - Даже моя бэйби не знает", limit=3)
        assert len(items) >= 1
        assert "ytm" in called_sources
        assert "yt" in called_sources, "Fallback к обычному YouTube обязан вызваться, если YTM пуст"
        assert items[0].url == "https://www.youtube.com/watch?v=fallback_yt_123"


def test_inline_search_release_authoritative_protection():
    """
    Проверяет, что uploader 'Release' или 'Release - Topic' никогда не становится артистом.
    """
    candidate_item = SearchItem(
        index=0,
        title="Запястья",
        uploader="Release - Topic",
        duration=180,
        url="https://www.youtube.com/watch?v=release_topic_123",
        source="ytmusic"
    )

    with patch("services.search._extract_source_items", return_value=[candidate_item]):
        items, _ = search_inline_tracks_sync("Макулатура -- Запястья", limit=1)
        assert len(items) == 1
        assert items[0].clean_artist == "Макулатура"
        assert "Release" not in items[0].clean_artist


@pytest.mark.asyncio
async def test_inline_download_missing_storage_channel_id_fails_gracefully():
    """
    Проверяет, что при отсутствии STORAGE_CHANNEL_ID:
    1. Пользователю выводится сообщение '⚠️ Не удалось подготовить аудио.'
    2. Никаких сообщений НЕ отправляется ни в ЛС пользователя, ни администратору.
    """
    from handlers.inline import process_inline_download
    import uuid

    cand_id = f"missing_storage_{uuid.uuid4().hex[:6]}"
    save_inline_candidate(
        cand_id=cand_id,
        target="https://www.youtube.com/watch?v=missing_storage_test",
        title="Test Track",
        artist="Test Artist",
        album=None,
        duration=180,
        thumbnail_url=None
    )

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.send_audio = AsyncMock()

    mock_dl = MagicMock()
    mock_dl.title = "Test Track"
    mock_dl.artist = "Test Artist"
    mock_dl.duration = 180
    mock_dl.file_path = MagicMock()
    mock_dl.file_path.exists.return_value = True
    mock_dl.cleanup = MagicMock()
    mock_dl.thumbnail_path = None

    with patch("handlers.inline.STORAGE_CHANNEL_ID", None), \
         patch("handlers.inline.download_track", AsyncMock(return_value=mock_dl)):
        res = await process_inline_download(
            cand_id=cand_id,
            inline_message_id="inl_msg_missing_storage",
            bot=mock_bot
        )

        assert res is None
        # bot.send_audio НЕ должен быть вызван вообще!
        assert not mock_bot.send_audio.called

        # Сообщение должно быть отредактировано с дружелюбным текстом
        assert mock_bot.edit_message_text.called
        call_text = mock_bot.edit_message_text.call_args.kwargs.get("text") or mock_bot.edit_message_text.call_args.args[0]
        assert "⚠️ Не удалось подготовить аудио." in call_text


@pytest.mark.asyncio
async def test_inline_download_strictly_uses_storage_channel_and_never_admin_or_user():
    """
    Проверяет, что при скачивании трека в Inline Mode bot.send_audio вызывается СТРОГО
    в STORAGE_CHANNEL_ID, и НИКОГДА в ADMIN_ID или from_user.id.
    """
    from handlers.inline import process_inline_download
    import uuid

    uid = uuid.uuid4().hex[:8]
    cand_id = f"strict_storage_{uid}"
    title_str = f"Strict Track {uid}"
    artist_str = f"Strict Artist {uid}"
    target_url = f"https://www.youtube.com/watch?v=strict_storage_{uid}"

    save_inline_candidate(
        cand_id=cand_id,
        target=target_url,
        title=title_str,
        artist=artist_str,
        album=None,
        duration=200,
        thumbnail_url=None
    )

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.audio.file_id = "BQACAgQAAxkBAAICaW_strict_file_id_123456"
    mock_msg.message_id = 999
    mock_bot.send_audio = AsyncMock(return_value=mock_msg)

    mock_dl = MagicMock()
    mock_dl.title = title_str
    mock_dl.artist = artist_str
    mock_dl.duration = 200
    mock_dl.file_path = MagicMock()
    mock_dl.file_path.exists.return_value = True
    mock_dl.cleanup = MagicMock()
    mock_dl.thumbnail_path = None

    test_storage_id = -100999888777
    admin_id = 12345678
    user_id = 987654321

    with patch("handlers.inline.STORAGE_CHANNEL_ID", test_storage_id), \
         patch("handlers.inline.download_track", AsyncMock(return_value=mock_dl)):
        res = await process_inline_download(
            cand_id=cand_id,
            inline_message_id="inl_msg_strict_storage",
            bot=mock_bot
        )

        assert res == "BQACAgQAAxkBAAICaW_strict_file_id_123456"
        assert mock_bot.send_audio.called

        for call in mock_bot.send_audio.call_args_list:
            chat_id = call.kwargs.get("chat_id") or call.args[0]
            assert chat_id == test_storage_id, "Файл обязан отправляться строго в STORAGE_CHANNEL_ID!"
            assert chat_id != admin_id, "Категорически запрещено отправлять в чат администратора!"
            assert chat_id != user_id, "Категорически запрещено отправлять в ЛС пользователя!"


# ============================================================================
# 6. ТЕСТЫ ДЛЯ ДЕТЕКЦИИ И ШТРАФОВАНИЯ ПОДОЗРИТЕЛЬНОГО ХРОНОМЕТРАЖА (DURATION SCORING)
# ============================================================================

def test_duration_scoring_tiered_deltas_for_expected_208s():
    """
    Проверяет градуированную систему штрафов/бонусов за хронометраж при эталоне 208с (3:28):
    - 208s (diff 0)  -> PASS (бонус +200)
    - 209s (diff 1)  -> PASS (бонус +200)
    - 212s (diff 4)  -> PASS / допустимо (бонус +100)
    - 215s (diff 7)  -> заметный penalty (-470)
    - 180s (diff 28) -> сильный penalty (-3280)
    - 125s (diff 83, 2:05) -> REJECT / катастрофический штраф (-9240)
    """
    from services.search import SearchItem, _score_inline_candidate

    canonical_dur = 208

    # Создаем базовые элементы с одинаковыми текстовыми данными
    def make_item(dur: int) -> SearchItem:
        return SearchItem(
            index=0,
            title="Даже моя бэйби не знает",
            artist="Пошлая Молли",
            uploader="Пошлая Молли - Topic",
            duration=dur,
            url=f"https://www.youtube.com/watch?v=dur_{dur}",
            source="ytmusic",
        )

    # 1. 208s (diff 0) -> PASS (+200)
    item_208 = make_item(208)
    score_208 = _score_inline_candidate(item_208, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)

    # 2. 209s (diff 1) -> PASS (+200)
    item_209 = make_item(209)
    score_209 = _score_inline_candidate(item_209, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)
    assert score_208 == score_209

    # 3. 212s (diff 4) -> PASS / допустимо (+100)
    item_212 = make_item(212)
    score_212 = _score_inline_candidate(item_212, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)
    assert score_208 - score_212 == 100.0  # +200 vs +100 = разница ровно 100

    # 4. 215s (diff 7) -> заметный penalty (-470)
    item_215 = make_item(215)
    score_215 = _score_inline_candidate(item_215, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)
    # diff=7: -350 - (7-4)*40 = -470. Разница с идеальным (+200): 670
    assert score_208 - score_215 == 670.0

    # 5. 180s (diff 28) -> сильный penalty (-3280)
    item_180 = make_item(180)
    score_180 = _score_inline_candidate(item_180, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)
    # diff=28: -2500 - (28-15)*60 = -3280. Разница с идеальным (+200): 3480
    assert score_208 - score_180 == 3480.0

    # 6. 125s (2:05, diff 83) -> REJECT (-9240)
    item_125 = make_item(125)
    score_125 = _score_inline_candidate(item_125, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)
    # diff=83: -5000 - (83-30)*80 = -9240. Разница с идеальным (+200): 9440
    assert score_208 - score_125 == 9440.0
    assert score_125 < -5000.0, "Кандидат с длительностью 2:05 обязан получить катастрофический штраф и статус REJECT"


def test_duration_scoring_rejects_suspicious_205_candidate_in_ranking():
    """
    Проверяет сценарий пользователя:
    #1 3:28 (208s) -> score ~1900-2350
    #2 3:30 (210s) -> score ~1900-2350
    #5 2:05 (125s) -> score < -5000 (REJECT), не попадает в подходящие кандидаты
    """
    from services.search import SearchItem, _score_inline_candidate

    canonical_dur = 208
    candidates = [
        SearchItem(index=0, title="Даже моя бэйби не знает", artist="Пошлая Молли", uploader="Пошлая Молли - Topic", duration=208, url="https://ytm/1", source="ytmusic"),
        SearchItem(index=0, title="Даже моя бэйби не знает", artist="Пошлая Молли", uploader="Пошлая Молли - Topic", duration=210, url="https://ytm/2", source="ytmusic"),
        SearchItem(index=0, title="Даже моя бэйби не знает", artist="Пошлая Молли", uploader="Пошлая Молли - Topic", duration=209, url="https://ytm/3", source="ytmusic"),
        SearchItem(index=0, title="Даже моя бэйби не знает", artist="Пошлая Молли", uploader="Пошлая Молли - Topic", duration=207, url="https://ytm/4", source="ytmusic"),
        SearchItem(index=0, title="Даже моя бэйби не знает", artist="Пошлая Молли", uploader="Пошлая Молли", duration=125, url="https://yt/clip", source="youtube"),
    ]

    for c in candidates:
        _score_inline_candidate(c, "Пошлая Молли", "Даже моя бэйби не знает", set(), canonical_dur)

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)

    # Кандидат 3:28 (208s) на 1 месте
    assert ranked[0].duration == 208
    assert ranked[0].score > 1500.0

    # Кандидат 2:05 (125s) на последнем месте с глубоко отрицательным скором
    assert ranked[-1].duration == 125
    assert ranked[-1].score < -5000.0

    # Проверяем фильтрацию suitable_ytm (score >= 500.0)
    suitable = [c for c in candidates if c.score >= 500.0]
    assert all(c.duration != 125 for c in suitable)
    assert len(suitable) == 4


def test_duration_scoring_tempo_modifiers_bypass_penalty():
    """
    Проверяет, что при запросе с модификатором темпа (sped up, slowed, nightcore)
    штраф за отличие длительности от эталона НЕ начисляется.
    """
    from services.search import SearchItem, _score_inline_candidate

    canonical_dur = 208
    item_sped_up = SearchItem(
        index=0,
        title="Даже моя бэйби не знает (Sped Up)",
        artist="Пошлая Молли",
        uploader="Various Artists",
        duration=150,
        url="https://yt/spedup",
        source="youtube"
    )

    req_mods = {"sped up"}
    score = _score_inline_candidate(item_sped_up, "Пошлая Молли", "Даже моя бэйби не знает", req_mods, canonical_dur)
    assert score > 1000.0


def test_duration_scoring_live_modifier_allows_wider_tolerance():
    """
    Проверяет, что при явном запросе live-версии кандидат с отличием хронометража до 15 секунд
    получает бонус, а не штраф.
    """
    from services.search import SearchItem, _score_inline_candidate

    canonical_dur = 208
    item_live = SearchItem(
        index=0,
        title="Даже моя бэйби не знает (Live в Москве)",
        artist="Пошлая Молли",
        uploader="Пошлая Молли",
        duration=220,
        url="https://yt/live",
        source="youtube"
    )

    req_mods = {"live"}
    score = _score_inline_candidate(item_live, "Пошлая Молли", "Даже моя бэйби не знает", req_mods, canonical_dur)
    assert score > 1500.0


def test_find_studio_reference_duration():
    """
    Проверяет извлечение эталонного хронометража:
    1. Приоритет YT Music студийного трека над Topic и обычным видео.
    2. Fallback на Topic-канал, если YT Music отсутствует.
    3. Игнорирование live-треков при поиске эталона.
    """
    from services.search import SearchItem, _find_studio_reference_duration

    # Случай 1: есть студийный трек YT Music
    cands1 = [
        SearchItem(index=0, title="Даже моя бэйби не знает (Live)", duration=240, source="ytmusic", is_live=True, url="1"),
        SearchItem(index=0, title="Даже моя бэйби не знает", artist="Пошлая Молли", duration=208, source="ytmusic", is_live=False, url="2"),
        SearchItem(index=0, title="Даже моя бэйби не знает", duration=210, uploader="Пошлая Молли - Topic", source="youtube", url="3"),
    ]
    assert _find_studio_reference_duration(cands1, "Пошлая Молли", "Даже моя бэйби не знает") == 208

    # Случай 2: нет YT Music, но есть YouTube Topic
    cands2 = [
        SearchItem(index=0, title="Даже моя бэйби не знает", duration=209, uploader="Пошлая Молли - Topic", source="youtube", url="1"),
        SearchItem(index=0, title="Даже моя бэйби не знает (Клип)", duration=125, uploader="Пошлая Молли", source="youtube", url="2"),
    ]
    assert _find_studio_reference_duration(cands2, "Пошлая Молли", "Даже моя бэйби не знает") == 209

    # Случай 3: только сторонние неофициальные клипы
    cands3 = [
        SearchItem(index=0, title="Даже моя бэйби не знает (Клип)", duration=125, uploader="User123", source="youtube", url="1"),
    ]
    assert _find_studio_reference_duration(cands3, "Пошлая Молли", "Даже моя бэйби не знает") is None
