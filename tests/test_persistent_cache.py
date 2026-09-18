import asyncio
import time
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineQuery, ChosenInlineResult, Message, Audio

from services.persistent_cache import (
    build_source_key,
    compute_metadata_hash,
    normalize_search_query_key,
    PersistentTrackCacheItem,
    D1Client,
    get_persistent_track_async,
    save_persistent_track_async,
    invalidate_persistent_track_async,
    get_persistent_search_results_async,
    save_persistent_search_results_async,
    check_metadata_match,
    LAST_USED_UPDATE_INTERVAL_SECONDS,
    _PERSISTENT_TRACK_L1,
    _PERSISTENT_SEARCH_L1,
)
from handlers.inline import (
    process_inline_download,
    is_unusable_file_id_error,
    _in_flight_downloads,
    _in_flight_lock,
)


@pytest.fixture(autouse=True)
def clean_memory_caches():
    """Очищает L1 in-memory кэши перед и после каждого теста."""
    _PERSISTENT_TRACK_L1._cache.clear()
    _PERSISTENT_SEARCH_L1._cache.clear()
    _in_flight_downloads.clear()
    yield
    _PERSISTENT_TRACK_L1._cache.clear()
    _PERSISTENT_SEARCH_L1._cache.clear()
    _in_flight_downloads.clear()


# ============================================================================
# 1. ТЕСТЫ ДЕТЕРМИНИРОВАННОСТИ И КЛЮЧЕЙ
# ============================================================================

def test_build_source_key_deterministic():
    """Тест 1: build_source_key() абсолютно детерминирован при повторных вызовах."""
    url = "https://www.youtube.com/watch?v=1NI14HDF7h0"
    k1, t1, id1 = build_source_key(url)
    k2, t2, id2 = build_source_key(url)
    assert k1 == k2 == "youtube:1NI14HDF7h0"
    assert t1 == t2 == "youtube"
    assert id1 == id2 == "1NI14HDF7h0"


def test_build_source_key_youtube_spelling_variants():
    """
    Тест 2: Одинаковый YouTube video ID при различных форматах URL
    (watch?v=, youtu.be, shorts, embed, music.youtube.com, готовый префикс)
    всегда резолвится в единый source_key.
    """
    variants = [
        "https://www.youtube.com/watch?v=1NI14HDF7h0",
        "https://youtu.be/1NI14HDF7h0",
        "https://music.youtube.com/watch?v=1NI14HDF7h0&feature=share",
        "https://www.youtube.com/shorts/1NI14HDF7h0",
        "https://www.youtube.com/embed/1NI14HDF7h0",
        "http://youtube.com/watch?v=1NI14HDF7h0",
        "youtube:1NI14HDF7h0",
    ]
    for v in variants:
        key, stype, sid = build_source_key(v)
        assert key == "youtube:1NI14HDF7h0", f"Failed for variant: {v}"
        assert stype == "youtube"
        assert sid == "1NI14HDF7h0"


def test_build_source_key_studio_vs_live():
    """
    Тест 3: Studio и Live версии трека имеют разные video ID,
    поэтому гарантированно получают разные source_key.
    """
    studio_url = "https://www.youtube.com/watch?v=studioVideo11"
    live_url = "https://www.youtube.com/watch?v=liveVideo222"

    key_studio, _, _ = build_source_key(studio_url)
    key_live, _, _ = build_source_key(live_url)

    assert key_studio == "youtube:studioVideo11"
    assert key_live == "youtube:liveVideo222"
    assert key_studio != key_live


def test_compute_metadata_hash_deterministic():
    """
    Тест 11: metadata_hash детерминирован, нормализует регистр и Unicode,
    различает изменения в метаданных.
    """
    h1 = compute_metadata_hash("RSAC", "Я всё ещё по тебе скучаю", "Я всё ещё по тебе скучаю", 233)
    # Тот же трек с другим регистром и пробелами
    h2 = compute_metadata_hash("  rsac  ", "я всё ещё по тебе скучаю", "я всё ещё по тебе скучаю", 233)
    assert h1 == h2

    # Изменение альбома меняет хэш
    h3 = compute_metadata_hash("RSAC", "Я всё ещё по тебе скучаю", "Другой Альбом", 233)
    assert h1 != h3


def test_search_cache_ttl_and_normalization():
    """
    Тест 12: Search cache уважает нормализацию запроса и TTL.
    """
    q1 = "  Пошлая   Молли  —  Даже моя бэйби   "
    q2 = "пошлая молли - даже моя бэйби"
    assert normalize_search_query_key(q1) == normalize_search_query_key(q2)


# ============================================================================
# 2. ТЕСТЫ ПОВЕДЕНИЯ КЭША (HIT / MISS / REDOWNLOAD)
# ============================================================================

@pytest.mark.asyncio
async def test_persistent_cache_miss_calls_downloader_once():
    """
    Тест 4: При CACHE MISS downloader вызывается ровно один раз,
    аудио загружается в хранилище, и результат сохраняется в D1.
    """
    mock_candidate = {
        "id": "cand_miss_1",
        "target": "https://www.youtube.com/watch?v=missVideo1",
        "title": "Title 1",
        "artist": "Artist 1",
        "album": "Album 1",
        "duration": 180,
        "thumbnail_url": "https://img.com/1.jpg"
    }

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    
    mock_uploaded = MagicMock()
    mock_uploaded.audio = MagicMock()
    mock_uploaded.audio.file_id = "BAACAgIAAxkBAAE12345678901234567890"
    mock_uploaded.audio.file_unique_id = "uniq_123"
    mock_uploaded.message_id = 999
    mock_bot.send_audio = AsyncMock(return_value=mock_uploaded)

    mock_downloaded = MagicMock()
    mock_downloaded.title = "Title 1"
    mock_downloaded.artist = "Artist 1"
    mock_downloaded.album = "Album 1"
    mock_downloaded.duration = 180
    mock_downloaded.filesize = 5000000
    mock_downloaded.file_path = MagicMock()
    mock_downloaded.thumbnail_path = None
    mock_downloaded.cleanup = MagicMock()

    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    mock_d1.execute_query = AsyncMock(return_value=None)  # Miss

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", return_value=None), \
         patch("handlers.inline.get_cached_track_async", return_value=None), \
         patch("handlers.inline.download_track", new_callable=AsyncMock) as mock_dl, \
         patch("handlers.inline.save_persistent_track_async", new_callable=AsyncMock) as mock_save_p, \
         patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

        mock_dl.return_value = mock_downloaded

        file_id = await process_inline_download(
            cand_id="cand_miss_1",
            inline_message_id="inl_msg_1",
            bot=mock_bot
        )

        assert file_id == "BAACAgIAAxkBAAE12345678901234567890"
        mock_dl.assert_called_once()
        mock_bot.send_audio.assert_called_once()
        mock_save_p.assert_called_once()
        mock_bot.edit_message_media.assert_called_once()


@pytest.mark.asyncio
async def test_persistent_cache_hit_does_not_call_downloader_or_upload():
    """
    Тест 5 и 6: При CACHE HIT:
    - downloader НЕ вызывается (0 вызовов download_track)
    - Telegram upload в storage channel НЕ вызывается (0 вызовов send_audio)
    - файл сразу отдаётся через edit_message_media
    """
    mock_candidate = {
        "id": "cand_hit_1",
        "target": "https://www.youtube.com/watch?v=hitVideo1",
        "title": "Hit Title",
        "artist": "Hit Artist",
        "album": "Hit Album",
        "duration": 200,
        "thumbnail_url": None
    }

    cached_hash = compute_metadata_hash("Hit Artist", "Hit Title", "Hit Album", 200)
    cached_item = PersistentTrackCacheItem(
        source_key="youtube:hitVideo1",
        source_type="youtube",
        source_id="hitVideo1",
        artist="Hit Artist",
        title="Hit Title",
        album="Hit Album",
        duration=200,
        metadata_hash=cached_hash,
        telegram_file_id="BAACAgIAAxkBAAE99999999999999999999",
        storage_message_id=777,
        file_size=4000000,
        created_at=1000,
        last_used_at=1000
    )

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    mock_bot.send_audio = AsyncMock()

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", return_value=cached_item), \
         patch("handlers.inline.download_track", new_callable=AsyncMock) as mock_dl:

        file_id = await process_inline_download(
            cand_id="cand_hit_1",
            inline_message_id="inl_msg_hit",
            bot=mock_bot
        )

        assert file_id == "BAACAgIAAxkBAAE99999999999999999999"
        mock_dl.assert_not_called()
        mock_bot.send_audio.assert_not_called()
        mock_bot.edit_message_media.assert_called_once()


@pytest.mark.asyncio
async def test_persistent_cache_hit_after_local_sqlite_loss():
    """
    Тест 7: Симуляция рестарта / переразвёртывания Render:
    Локальный L1 пуст, локальный SQLite пуст.
    Но Cloudflare D1 содержит запись -> file_id успешно возвращается без скачивания.
    """
    # Гарантируем, что локальный L1 пуст
    _PERSISTENT_TRACK_L1._cache.clear()

    d1_row = {
        "source_key": "youtube:redeployVid1",
        "source_type": "youtube",
        "source_id": "redeployVid1",
        "artist": "Persistent Artist",
        "title": "Persistent Title",
        "album": "Persistent Album",
        "duration": 210,
        "metadata_hash": "hash123",
        "telegram_file_id": "BAACAgIAAxkBAAEREDEPLOY12345678901234",
        "telegram_file_unique_id": "u_redeploy",
        "storage_message_id": 501,
        "file_size": 3500000,
        "created_at": 100,
        "last_used_at": 100
    }

    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    mock_d1.execute_query = AsyncMock(return_value=[d1_row])

    item = await get_persistent_track_async("youtube:redeployVid1", client=mock_d1)
    assert item is not None
    assert item.telegram_file_id == "BAACAgIAAxkBAAEREDEPLOY12345678901234"
    assert item.artist == "Persistent Artist"

    # Проверяем, что L1 прогрелся из D1
    assert _PERSISTENT_TRACK_L1.get("youtube:redeployVid1") is not None


@pytest.mark.asyncio
async def test_d1_unavailable_on_read_controlled_fallback():
    """
    Тест 8: Недоступность Cloudflare D1 при чтении:
    Бот не падает, а плавно переходит к скачиванию через fallback.
    """
    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    # Симуляция сетевого сбоя или 500 ошибки Cloudflare
    mock_d1.execute_query = AsyncMock(side_effect=Exception("Cloudflare API 500 Internal Error"))

    res = await get_persistent_track_async("youtube:errVid", client=mock_d1)
    assert res is None  # Controlled fallback to None


@pytest.mark.asyncio
async def test_d1_unavailable_on_write_does_not_break_user_result():
    """
    Тест 9: Недоступность D1 при записи:
    Telegram upload завершился успешно, ошибка записи в D1 логируется,
    но результат пользователю всё равно успешно отдаётся.
    """
    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    mock_d1.execute_query = AsyncMock(return_value=None)  # D1 write failed

    success = await save_persistent_track_async(
        source_key="youtube:writeErr",
        source_type="youtube",
        source_id="writeErr",
        artist="Artist",
        title="Title",
        album=None,
        duration=120,
        telegram_file_id="BAACAgIAAxkBAAEWRITE_ERR1234567890",
        client=mock_d1
    )
    # Возвращает False (так как D1 не записал), но не падает с исключением
    assert success is False
    # При этом локальный L1 всё равно прогрет
    assert _PERSISTENT_TRACK_L1.get("youtube:writeErr") is not None


@pytest.mark.asyncio
async def test_invalid_file_id_invalidates_cache_and_redownloads():
    """
    Тест 10: Если Telegram отклоняет старый file_id (Bad Request: wrong file identifier),
    кэш инвалидируется, выполняется повторное скачивание и загрузка нового file_id.
    """
    mock_candidate = {
        "id": "cand_inv_1",
        "target": "https://www.youtube.com/watch?v=invVid1",
        "title": "Title Inv",
        "artist": "Artist Inv",
        "album": "Album Inv",
        "duration": 190,
        "thumbnail_url": None
    }

    inv_hash = compute_metadata_hash("Artist Inv", "Title Inv", "Album Inv", 190)
    old_cached = PersistentTrackCacheItem(
        source_key="youtube:invVid1",
        source_type="youtube",
        source_id="invVid1",
        artist="Artist Inv",
        title="Title Inv",
        album="Album Inv",
        duration=190,
        metadata_hash=inv_hash,
        telegram_file_id="BAACAgIAAxkBAAE_OLD_EXPIRED_FILE_ID",
        storage_message_id=1,
        file_size=100,
        created_at=1,
        last_used_at=1
    )

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()

    # Первый вызов edit_message_media бросает ошибку невалидного file_id
    # Второй вызов (после перескачивания) проходит успешно
    mock_bot.edit_message_media = AsyncMock(
        side_effect=[
            Exception("TelegramBadRequest: Bad Request: wrong file identifier / file is unusable"),
            None
        ]
    )

    mock_fresh_uploaded = MagicMock()
    mock_fresh_uploaded.audio = MagicMock()
    mock_fresh_uploaded.audio.file_id = "BAACAgIAAxkBAAE_NEW_FRESH_FILE_ID_12345678"
    mock_fresh_uploaded.audio.file_unique_id = "fresh_u"
    mock_fresh_uploaded.message_id = 999
    mock_bot.send_audio = AsyncMock(return_value=mock_fresh_uploaded)

    mock_downloaded = MagicMock()
    mock_downloaded.title = "Title Inv"
    mock_downloaded.artist = "Artist Inv"
    mock_downloaded.album = "Album Inv"
    mock_downloaded.duration = 190
    mock_downloaded.filesize = 4500000
    mock_downloaded.file_path = MagicMock()
    mock_downloaded.thumbnail_path = None
    mock_downloaded.cleanup = MagicMock()

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", side_effect=[old_cached, None]), \
         patch("handlers.inline.invalidate_persistent_track_async", new_callable=AsyncMock) as mock_inv, \
         patch("handlers.inline.download_track", new_callable=AsyncMock) as mock_dl, \
         patch("handlers.inline.save_persistent_track_async", new_callable=AsyncMock) as mock_save, \
         patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

        mock_dl.return_value = mock_downloaded

        file_id = await process_inline_download(
            cand_id="cand_inv_1",
            inline_message_id="inl_inv_msg",
            bot=mock_bot
        )

        assert file_id == "BAACAgIAAxkBAAE_NEW_FRESH_FILE_ID_12345678"
        # Инвалидация была вызвана для старого file_id
        mock_inv.assert_called_once_with("youtube:invVid1", "BAACAgIAAxkBAAE_OLD_EXPIRED_FILE_ID")
        # Скачивание было запущено для получения нового файла
        mock_dl.assert_called_once()
        # Новый file_id сохранён
        mock_save.assert_called_once()


@pytest.mark.asyncio
async def test_in_flight_downloads_deduplication():
    """
    Тест 13: _in_flight_downloads предотвращает параллельное повторное скачивание
    одного и того же трека разными пользователями.
    """
    mock_candidate = {
        "id": "cand_inflight_1",
        "target": "https://www.youtube.com/watch?v=inFlightVid1",
        "title": "Parallel Title",
        "artist": "Parallel Artist",
        "album": None,
        "duration": 200,
        "thumbnail_url": None
    }

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()

    mock_uploaded = MagicMock()
    mock_uploaded.audio = MagicMock()
    mock_uploaded.audio.file_id = "BAACAgIAAxkBAAE_IN_FLIGHT_FILE_ID_12345"
    mock_uploaded.audio.file_unique_id = "u_inf"
    mock_uploaded.message_id = 888
    mock_bot.send_audio = AsyncMock(return_value=mock_uploaded)

    mock_downloaded = MagicMock()
    mock_downloaded.title = "Parallel Title"
    mock_downloaded.artist = "Parallel Artist"
    mock_downloaded.album = None
    mock_downloaded.duration = 200
    mock_downloaded.filesize = 4000000
    mock_downloaded.file_path = MagicMock()
    mock_downloaded.thumbnail_path = None
    mock_downloaded.cleanup = MagicMock()

    async def slow_download(*args, **kwargs):
        await asyncio.sleep(0.1)
        return mock_downloaded

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", return_value=None), \
         patch("handlers.inline.get_cached_track_async", return_value=None), \
         patch("handlers.inline.download_track", side_effect=slow_download) as mock_dl, \
         patch("handlers.inline.save_persistent_track_async", new_callable=AsyncMock), \
         patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

        # Запускаем два параллельных скачивания одного трека
        task1 = asyncio.create_task(
            process_inline_download("cand_inflight_1", "msg_1", mock_bot)
        )
        task2 = asyncio.create_task(
            process_inline_download("cand_inflight_1", "msg_2", mock_bot)
        )

        res1, res2 = await asyncio.gather(task1, task2)

        assert res1 == "BAACAgIAAxkBAAE_IN_FLIGHT_FILE_ID_12345"
        assert res2 == "BAACAgIAAxkBAAE_IN_FLIGHT_FILE_ID_12345"
        # Ровно один вызов реального downloader-а!
        assert mock_dl.call_count == 1
        # Ровно одна отправка в Telegram storage channel!
        assert mock_bot.send_audio.call_count == 1


def test_canonical_metadata_structure_preserved():
    """
    Тест 14: Канонические метаданные (artist, title, album, duration)
    корректно сохраняются в PersistentTrackCacheItem.
    """
    item = PersistentTrackCacheItem(
        source_key="youtube:1NI14HDF7h0",
        source_type="youtube",
        source_id="1NI14HDF7h0",
        artist="RSAC",
        title="Я всё ещё по тебе скучаю",
        album="Я всё ещё по тебе скучаю",
        duration=233,
        metadata_hash="hash",
        telegram_file_id="file123"
    )

    d = item.to_dict()
    assert d["artist"] == "RSAC"
    assert d["title"] == "Я всё ещё по тебе скучаю"
    assert d["album"] == "Я всё ещё по тебе скучаю"
    assert d["duration"] == 233

    restored = PersistentTrackCacheItem.from_dict(d)
    assert restored.artist == "RSAC"
    assert restored.title == "Я всё ещё по тебе скучаю"
    assert restored.album == "Я всё ещё по тебе скучаю"
    assert restored.duration == 233


@pytest.mark.asyncio
async def test_search_cache_ttl_expiry():
    """
    Тест 15: Search cache корректно отбрасывает записи с истекшим TTL.
    """
    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    
    # 1. Запись с неистёкшим TTL
    future_exp = int(time.time()) + 3600
    mock_d1.execute_query = AsyncMock(return_value=[{
        "results_json": json.dumps([{"title": "Track Fresh"}]),
        "expires_at": future_exp
    }])
    fresh_res = await get_persistent_search_results_async("Fresh Query", client=mock_d1)
    assert fresh_res is not None
    assert fresh_res[0]["title"] == "Track Fresh"

    # 2. Запись с истёкшим TTL
    _PERSISTENT_SEARCH_L1._cache.clear()
    past_exp = int(time.time()) - 10
    mock_d1.execute_query = AsyncMock(return_value=[{
        "results_json": json.dumps([{"title": "Track Expired"}]),
        "expires_at": past_exp
    }])
    expired_res = await get_persistent_search_results_async("Expired Query", client=mock_d1)
    assert expired_res is None


@pytest.mark.asyncio
async def test_d1_client_batch_schema_init():
    """
    Тест 16: Инициализация схемы D1 запускает batch-вызов с таблицами и индексами.
    """
    from services.persistent_cache import init_d1_schema_async

    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    mock_d1.execute_batch = AsyncMock(return_value=True)

    success = await init_d1_schema_async(client=mock_d1)
    assert success is True
    mock_d1.execute_batch.assert_called_once()
    statements = mock_d1.execute_batch.call_args[0][0]
    assert len(statements) >= 5
    assert any("CREATE TABLE IF NOT EXISTS track_cache" in s["sql"] for s in statements)
    assert any("CREATE TABLE IF NOT EXISTS search_cache" in s["sql"] for s in statements)


@pytest.mark.asyncio
async def test_search_cache_survives_local_sqlite_wipe_and_remains_selectable():
    """
    Тест 17: Полный сценарий Render restart:
    1. Поисковый запрос сохранён в persistent search cache (D1).
    2. Локальная база данных кандидатов SQLite и память полностью очищены (симуляция рестарта Render).
    3. Повторный запрос того же поискового запроса отдаётся из search cache.
    4. Кандидаты заново материализуются в локальной базе для текущего процесса.
    5. Выбранный пользователем результат (ChosenInlineResult -> process_inline_download)
       успешно находит кандидата и завершает загрузку без ошибок.
    """
    from services.database import get_db_connection, _INLINE_MEM_CACHE, get_inline_candidate_async
    from handlers.inline import handle_inline_query, process_inline_download

    query_str = "Restart Query Band - Song"
    candidates_data = [{
        "url": "https://www.youtube.com/watch?v=wipeTest123",
        "title": "Wipe Song",
        "artist": "Wipe Band",
        "album": "Wipe Album",
        "duration": 210,
        "thumbnail": None,
        "formatted_duration": "3:30"
    }]

    # 1. Результат сохранен в search_cache
    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    future_exp = int(time.time()) + 3600
    mock_d1.execute_query = AsyncMock(return_value=[{
        "results_json": json.dumps(candidates_data),
        "expires_at": future_exp
    }])

    # 2. Локальная база кандидатов и L1 полностью очищаются
    _INLINE_MEM_CACHE._cache.clear()
    _PERSISTENT_TRACK_L1._cache.clear()
    _PERSISTENT_SEARCH_L1._cache.clear()
    with get_db_connection() as conn:
        conn.execute("DELETE FROM inline_candidates")

    # 3. Пользователь вводит тот же запрос в перезапущенном контейнере
    mock_query = MagicMock()
    mock_query.query = query_str
    mock_query.answer = AsyncMock()

    with patch("services.persistent_cache.get_d1_client", return_value=mock_d1), \
         patch("handlers.inline.search_cached_tracks_async", return_value=[]), \
         patch("handlers.inline.search_tracks_async") as mock_search_tracks:

        await handle_inline_query(mock_query)

        # Поиск по сети НЕ производился, взят из search cache
        mock_search_tracks.assert_not_called()
        assert mock_query.answer.called
        results = mock_query.answer.call_args[1]["results"]
        assert len(results) == 1
        article = results[0]
        assert "Wipe Song" in article.title
        cand_id = article.id.replace("art_", "")

        # 4. Проверяем, что кандидат материализован в текущем процессе
        cand = await get_inline_candidate_async(cand_id)
        assert cand is not None
        assert cand["title"] == "Wipe Song"
        assert cand["artist"] == "Wipe Band"
        assert cand["album"] == "Wipe Album"
        assert cand["duration"] == 210

        # 5. Пользователь нажимает на результат (ChosenInlineResult -> process_inline_download)
        mock_bot = MagicMock()
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.edit_message_media = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.audio.file_id = "BAACAgIAAxkBAAEWIPESUCCESS1234567890"
        mock_msg.message_id = 111
        mock_bot.send_audio = AsyncMock(return_value=mock_msg)

        mock_dl = MagicMock()
        mock_dl.title = "Wipe Song"
        mock_dl.artist = "Wipe Band"
        mock_dl.album = "Wipe Album"
        mock_dl.duration = 210
        mock_dl.filesize = 5000000
        mock_dl.file_path = MagicMock()
        mock_dl.thumbnail_path = None
        mock_dl.cleanup = MagicMock()

        with patch("handlers.inline.get_persistent_track_async", return_value=None), \
             patch("handlers.inline.download_track", AsyncMock(return_value=mock_dl)), \
             patch("handlers.inline.save_persistent_track_async", AsyncMock()), \
             patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

            file_id = await process_inline_download(
                cand_id=cand_id,
                inline_message_id="inl_msg_wipe",
                bot=mock_bot
            )

            assert file_id == "BAACAgIAAxkBAAEWIPESUCCESS1234567890"
            assert mock_bot.edit_message_media.called


@pytest.mark.asyncio
async def test_same_source_key_same_metadata_hash_is_hit():
    """
    Тест 17: same source_key + same metadata_hash -> CACHE HIT.
    Различие только в регистре ('RSAC' vs 'RsAC') не считается несовпадением
    благодаря unicodedata NFKC + casefold().
    """
    mock_candidate = {
        "id": "cand_meta_hit",
        "target": "https://www.youtube.com/watch?v=hitMetaVid1",
        "title": "Я всё ещё по тебе скучаю",
        "artist": "RsAC",  # в запросе регистр RsAC
        "album": "Не мешай",
        "duration": 233,
        "thumbnail_url": None
    }

    # В кэше D1 каноническое написание RSAC
    cached_hash = compute_metadata_hash("RSAC", "Я всё ещё по тебе скучаю", "Не мешай", 233)
    cached_track = PersistentTrackCacheItem(
        source_key="youtube:hitMetaVid1",
        source_type="youtube",
        source_id="hitMetaVid1",
        artist="RSAC",
        title="Я всё ещё по тебе скучаю",
        album="Не мешай",
        duration=233,
        metadata_hash=cached_hash,
        telegram_file_id="BAACAgIAAxkBAAE_META_HIT_FILE_ID"
    )

    # 1. Проверяем check_metadata_match и matches_metadata
    is_match, c_hash, cur_hash = check_metadata_match(
        cached_track,
        artist=mock_candidate["artist"],
        title=mock_candidate["title"],
        album=mock_candidate["album"],
        duration=mock_candidate["duration"]
    )
    assert is_match is True
    assert c_hash == cur_hash
    assert cached_track.matches_metadata(
        mock_candidate["artist"],
        mock_candidate["title"],
        mock_candidate["album"],
        mock_candidate["duration"]
    ) is True

    # 2. Проверяем в process_inline_download: CACHE HIT, без вызова downloader
    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", return_value=cached_track), \
         patch("handlers.inline.download_track") as mock_dl, \
         patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

        file_id = await process_inline_download(
            cand_id="cand_meta_hit",
            inline_message_id="inl_meta_hit",
            bot=mock_bot
        )

        assert file_id == "BAACAgIAAxkBAAE_META_HIT_FILE_ID"
        mock_dl.assert_not_called()
        assert mock_bot.edit_message_media.called


@pytest.mark.asyncio
async def test_same_source_key_changed_metadata_hash_mismatch_detected():
    """
    Тест 18: same source_key + different metadata_hash:
    cached:
      artist=A, title=B, album=C, duration=D, file_id=X
    current:
      artist=A, title=B, album=E, duration=D

    Ожидается:
      metadata mismatch
      -> cache treated as MISS
      -> downloader called
      -> new file_id uploaded to storage & saved to D1
    """
    mock_candidate = {
        "id": "cand_meta_mismatch",
        "target": "https://www.youtube.com/watch?v=mismatchVid1",
        "title": "TitleB",
        "artist": "ArtistA",
        "album": "AlbumE",
        "duration": 200,
        "thumbnail_url": None
    }

    # В кэше старая запись с album=C
    old_hash = compute_metadata_hash("ArtistA", "TitleB", "AlbumC", 200)
    cached_track = PersistentTrackCacheItem(
        source_key="youtube:mismatchVid1",
        source_type="youtube",
        source_id="mismatchVid1",
        artist="ArtistA",
        title="TitleB",
        album="AlbumC",
        duration=200,
        metadata_hash=old_hash,
        telegram_file_id="FILE_ID_X"
    )

    # 1. Проверяем check_metadata_match и matches_metadata
    is_match, c_hash, cur_hash = check_metadata_match(
        cached_track,
        artist=mock_candidate["artist"],
        title=mock_candidate["title"],
        album=mock_candidate["album"],
        duration=mock_candidate["duration"]
    )
    assert is_match is False
    assert c_hash != cur_hash
    assert cached_track.matches_metadata(
        mock_candidate["artist"],
        mock_candidate["title"],
        mock_candidate["album"],
        mock_candidate["duration"]
    ) is False

    # 2. Проверяем, что в process_inline_download несовпадение приводит к CACHE MISS и вызову downloader
    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()

    mock_msg = MagicMock()
    mock_msg.audio = MagicMock()
    mock_msg.audio.file_id = "FILE_ID_NEW_FRESH"
    mock_msg.audio.file_unique_id = "u_fresh"
    mock_msg.message_id = 1234
    mock_bot.send_audio = AsyncMock(return_value=mock_msg)

    mock_downloaded = MagicMock()
    mock_downloaded.title = "TitleB"
    mock_downloaded.artist = "ArtistA"
    mock_downloaded.album = "AlbumE"
    mock_downloaded.duration = 200
    mock_downloaded.filesize = 4500000
    mock_downloaded.file_path = MagicMock()
    mock_downloaded.thumbnail_path = None
    mock_downloaded.cleanup = MagicMock()

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", return_value=cached_track), \
         patch("handlers.inline.download_track", AsyncMock(return_value=mock_downloaded)) as mock_dl, \
         patch("handlers.inline.save_persistent_track_async", AsyncMock()) as mock_save, \
         patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

        file_id = await process_inline_download(
            cand_id="cand_meta_mismatch",
            inline_message_id="inl_meta_mismatch",
            bot=mock_bot
        )

        # Несовпадение метаданных вызвало повторное скачивание и получение нового file_id
        mock_dl.assert_called_once()
        assert file_id == "FILE_ID_NEW_FRESH"
        mock_save.assert_called_once()
        assert mock_bot.send_audio.called


@pytest.mark.asyncio
async def test_repeated_hit_does_not_cause_unnecessary_download_or_upload():
    """
    Тест 19: Повторные cache HIT для одного и того же трека
    гарантированно выполняют 0 вызовов download_track и 0 вызовов bot.send_audio.
    """
    mock_candidate = {
        "id": "cand_rep_hit",
        "target": "https://www.youtube.com/watch?v=repeatedVid1",
        "title": "Persistent Song",
        "artist": "Persistent Band",
        "album": "Persistent Album",
        "duration": 210,
        "thumbnail_url": None
    }

    cached_hash = compute_metadata_hash("Persistent Band", "Persistent Song", "Persistent Album", 210)
    cached_track = PersistentTrackCacheItem(
        source_key="youtube:repeatedVid1",
        source_type="youtube",
        source_id="repeatedVid1",
        artist="Persistent Band",
        title="Persistent Song",
        album="Persistent Album",
        duration=210,
        metadata_hash=cached_hash,
        telegram_file_id="BAACAgIAAxkBAAE_REPEATED_HIT_FILE_ID_999"
    )

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.edit_message_media = AsyncMock()
    mock_bot.send_audio = AsyncMock()

    with patch("handlers.inline.get_inline_candidate_async", return_value=mock_candidate), \
         patch("handlers.inline.get_persistent_track_async", return_value=cached_track), \
         patch("handlers.inline.download_track") as mock_dl, \
         patch("handlers.inline.get_effective_storage_channel_id", return_value=-100123456789):

        # Вызываем 3 раза подряд (симуляция 3 разных пользователей)
        for i in range(3):
            file_id = await process_inline_download(
                cand_id="cand_rep_hit",
                inline_message_id=f"inl_msg_rep_{i}",
                bot=mock_bot
            )
            assert file_id == "BAACAgIAAxkBAAE_REPEATED_HIT_FILE_ID_999"

        # Ни одного вызова скачивания и ни одной отправки в storage
        mock_dl.assert_not_called()
        mock_bot.send_audio.assert_not_called()
        # Сообщения всем пользователям обновлены
        assert mock_bot.edit_message_media.call_count == 3


@pytest.mark.asyncio
async def test_last_used_at_update_is_throttled():
    """
    Тест 20: Обновление last_used_at в Cloudflare D1 троттлится:
    - Если запись использовалась недавно (< 1 часа), D1 UPDATE не вызывается (экономия квоты writes).
    - Если с момента последнего использования прошло >= 1 часа, D1 UPDATE выполняется.
    """
    now = int(time.time())

    # Сценарий A: запись использовалась 10 минут назад (600 сек < 3600 сек)
    mock_d1 = MagicMock()
    mock_d1.is_configured = True
    recent_row = [{
        "source_key": "youtube:recentVid",
        "source_type": "youtube",
        "source_id": "recentVid",
        "artist": "Artist",
        "title": "Title",
        "album": None,
        "duration": 180,
        "metadata_hash": "hash_recent",
        "telegram_file_id": "file_recent",
        "telegram_file_unique_id": "u_rec",
        "storage_message_id": 10,
        "file_size": 3000000,
        "created_at": now - 7200,
        "last_used_at": now - 600  # 10 мин назад
    }]
    mock_d1.execute_query = AsyncMock(return_value=recent_row)

    with patch("services.persistent_cache.get_d1_client", return_value=mock_d1), \
         patch("services.persistent_cache._update_last_used_at_safe") as mock_update:

        item = await get_persistent_track_async("youtube:recentVid")
        assert item is not None
        assert item.telegram_file_id == "file_recent"
        # Троттлинг сработал: обновление не инициировано
        mock_update.assert_not_called()

    # Сценарий B: запись использовалась 2 часа назад (7200 сек >= 3600 сек)
    _PERSISTENT_TRACK_L1._cache.clear()
    mock_d1_stale = MagicMock()
    mock_d1_stale.is_configured = True
    stale_row = [{
        "source_key": "youtube:staleVid",
        "source_type": "youtube",
        "source_id": "staleVid",
        "artist": "Artist",
        "title": "Title",
        "album": None,
        "duration": 180,
        "metadata_hash": "hash_stale",
        "telegram_file_id": "file_stale",
        "telegram_file_unique_id": "u_stale",
        "storage_message_id": 11,
        "file_size": 3000000,
        "created_at": now - 14400,
        "last_used_at": now - 7200  # 2 часа назад
    }]
    mock_d1_stale.execute_query = AsyncMock(return_value=stale_row)

    with patch("services.persistent_cache.get_d1_client", return_value=mock_d1_stale), \
         patch("services.persistent_cache._update_last_used_at_safe") as mock_update_stale:

        item = await get_persistent_track_async("youtube:staleVid")
        assert item is not None
        # Прошло >= 1 часа: обновление запущено
        mock_update_stale.assert_called_once()
        assert mock_update_stale.call_args[0][0] == "youtube:staleVid"
