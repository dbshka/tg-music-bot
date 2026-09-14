import asyncio
import html
import logging
import time
import uuid
from typing import Dict, List, Optional
from pathlib import Path

from aiogram import Router, F
from aiogram.types import (
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChosenInlineResult,
    FSInputFile
)
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramBadRequest

from config import ADMIN_ID, STORAGE_CHANNEL_ID
from services.database import (
    get_cached_track_async,
    save_cached_track_async,
    search_cached_tracks_async,
    log_user_activity_async,
    increment_user_download_async
)
from services.extractor import find_first_url, resolve_track_url
from services.downloader import download_track, DownloadedAudio
from handlers.music import DOWNLOAD_SEMAPHORE

logger = logging.getLogger(__name__)
router = Router(name="inline_router")

# Защита от одновременных дублирующих скачиваний одного и того же трека при быстром вводе текста
_IN_FLIGHT_DOWNLOADS: Dict[str, asyncio.Task] = {}
_IN_FLIGHT_LOCK = asyncio.Lock()
_PENDING_USER_QUERIES: Dict[int, str] = {}
_USER_QUERY_LOCK = asyncio.Lock()


async def _upload_audio_for_file_id(bot, downloaded_audio: DownloadedAudio) -> Optional[str]:
    """
    Загружает аудиофайл на сервер Telegram для получения постоянного file_id.
    Файл загружается в STORAGE_CHANNEL_ID (если указан) или в чат ADMIN_ID с мгновенным
    удалением сообщения. Личные сообщения пользователя (user_id) НИКОГДА не используются,
    чтобы исключить неожиданный спам в ЛС при поиске в чатах.
    """
    audio_path = Path(downloaded_audio.file_path)
    thumb_path = downloaded_audio.thumbnail_path
    thumb_file = (
        FSInputFile(thumb_path)
        if (thumb_path and thumb_path.exists() and thumb_path.is_file() and thumb_path.stat().st_size > 0)
        else None
    )

    targets = []
    if STORAGE_CHANNEL_ID:
        targets.append((STORAGE_CHANNEL_ID, False))
    if ADMIN_ID:
        targets.append((ADMIN_ID, True))

    for target_chat, should_delete in targets:
        try:
            sent_msg = await bot.send_audio(
                chat_id=target_chat,
                audio=FSInputFile(audio_path),
                title=downloaded_audio.title,
                performer=downloaded_audio.artist,
                duration=downloaded_audio.duration,
                thumbnail=thumb_file,
                disable_notification=True
            )
            if sent_msg and sent_msg.audio:
                file_id = sent_msg.audio.file_id
                # Удаляем буферное сообщение у администратора, чтобы не засорять чат.
                # Telegram сохраняет file_id на CDN навсегда даже после удаления сообщения.
                if should_delete:
                    try:
                        await bot.delete_message(chat_id=target_chat, message_id=sent_msg.message_id)
                    except Exception as del_err:
                        logger.debug("Не удалось удалить буферное сообщение у админа: %s", del_err)
                return file_id
        except Exception as upload_err:
            logger.warning("Не удалось получить file_id через целевой чат %s: %s", target_chat, upload_err)

    return None


async def _download_and_cache(cache_key: str, raw_query: str, url: Optional[str], bot) -> Optional[str]:
    """
    Скачивает трек, загружает в Telegram для получения file_id и сохраняет в БД.
    Работает как для быстрых inline-ответов, так и в фоне, если inline query истёк по таймауту.
    """
    req_id = uuid.uuid4().hex[:6]
    downloaded_audio: Optional[DownloadedAudio] = None
    try:
        async with DOWNLOAD_SEMAPHORE:
            if url:
                track_info = await resolve_track_url(url)
                try:
                    downloaded_audio = await download_track(
                        query_or_url=track_info.target,
                        custom_title=track_info.title,
                        custom_artist=track_info.artist,
                        thumbnail_url=track_info.thumbnail_url,
                        expected_duration=track_info.duration,
                        request_id=f"in_{req_id}"
                    )
                except Exception as direct_err:
                    if track_info.title and track_info.artist:
                        logger.info(
                            "Inline прямая ссылка не скачалась (%s), переключаемся на поиск %s - %s",
                            direct_err, track_info.artist, track_info.title
                        )
                        downloaded_audio = await download_track(
                            query_or_url=f"ytsearch1:{track_info.artist} - {track_info.title}",
                            custom_title=track_info.title,
                            custom_artist=track_info.artist,
                            thumbnail_url=track_info.thumbnail_url,
                            expected_duration=track_info.duration,
                            request_id=f"in_{req_id}_fb"
                        )
                    else:
                        raise
            else:
                downloaded_audio = await download_track(
                    query_or_url=f"ytsearch1:{raw_query}",
                    request_id=f"in_{req_id}"
                )

        if downloaded_audio:
            file_id = await _upload_audio_for_file_id(bot, downloaded_audio)
            if file_id:
                # Сохраняем исходный запрос
                await save_cached_track_async(
                    query=cache_key,
                    file_id=file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )
                # Сохраняем популярные комбинации имени и названия для мгновенных будущих поисков
                if downloaded_audio.artist and downloaded_audio.title:
                    art = downloaded_audio.artist.strip()
                    tit = downloaded_audio.title.strip()
                    await save_cached_track_async(f"{art} - {tit}", file_id, tit, art, downloaded_audio.duration)
                    await save_cached_track_async(f"{art} {tit}", file_id, tit, art, downloaded_audio.duration)
                    await save_cached_track_async(f"{tit} {art}", file_id, tit, art, downloaded_audio.duration)
                    await save_cached_track_async(tit, file_id, tit, art, downloaded_audio.duration)
                return file_id
    except Exception as e:
        logger.warning("Ошибка скачивания/кэширования inline '%s': %s", raw_query[:40], e)
    finally:
        if downloaded_audio:
            downloaded_audio.cleanup()
    return None



@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    """
    Обработчик Telegram Inline Mode:
    @musicAutoSaver_bot <поисковый запрос или ссылка>
    """
    raw_query = inline_query.query.strip()
    user_id = inline_query.from_user.id if inline_query.from_user else 0

    # 1. Если поисковый запрос пустой — показываем подсказку
    if not raw_query:
        hint_article = InlineQueryResultArticle(
            id="hint_empty_query",
            title="🎵 Найди музыку прямо здесь",
            description="@musicAutoSaver_bot название песни",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "🎵 <b>Поиск музыки прямо в Telegram</b>\n\n"
                    "Введите в любом чате:\n"
                    "<code>@musicAutoSaver_bot название песни или исполнитель</code>\n\n"
                    "<i>Например: @musicAutoSaver_bot The Weeknd Blinding Lights</i>"
                ),
                parse_mode="HTML"
            )
        )
        await inline_query.answer([hint_article], cache_time=1, is_personal=True)
        return

    logger.info("Inline запрос: user_id=%s query='%s'", user_id, raw_query[:60])
    if inline_query.from_user:
        await log_user_activity_async(
            inline_query.from_user.id,
            inline_query.from_user.username,
            inline_query.from_user.full_name
        )

    results = []

    # 2. Проверка ссылки (YouTube, Spotify, Apple Music, SoundCloud)
    url = find_first_url(raw_query)
    cache_key = url if url else raw_query

    # 3. Шаг 1: Мгновенная проверка кэша (L1 RAM + L2 SQLite)
    t0 = time.perf_counter()
    exact_cached = await get_cached_track_async(cache_key)
    cached_candidates = []
    if exact_cached:
        cached_candidates.append(exact_cached)

    # Если запрос текстовый, ищем также по частичным совпадениям в базе
    if not url:
        fuzzy_cached = await search_cached_tracks_async(raw_query, limit=5)
        for fc in fuzzy_cached:
            if not any(c["file_id"] == fc["file_id"] for c in cached_candidates):
                cached_candidates.append(fc)

    # Формируем InlineQueryResultCachedAudio для найденных треков из кэша (чистый вид без кнопок и лишних подписей)
    for idx, c in enumerate(cached_candidates[:5]):
        file_id = c["file_id"]
        results.append(
            InlineQueryResultCachedAudio(
                id=f"cached_{file_id[:16]}_{idx}",
                audio_file_id=file_id
            )
        )

    # Если треки найдены в кэше — отдаем мгновенно (< 10 мс)
    if results:
        dur_ms = (time.perf_counter() - t0) * 1000
        logger.info("Inline ответ из кэша (%d треков) за %.1f мс для '%s'", len(results), dur_ms, raw_query[:40])
        await inline_query.answer(results, cache_time=60, is_personal=True)
        return

    # 4. Шаг 2: Если трека нет в кэше — выполняем загрузку через существующий pipeline
    # Если запрос слишком короткий (< 3 символов), не запускаем тяжелый парсинг
    if len(raw_query) < 3 and not url:
        short_hint = InlineQueryResultArticle(
            id="hint_short_query",
            title="🔍 Введите более подробный запрос",
            description="Укажите хотя бы 3 символа для точного поиска",
            input_message_content=InputTextMessageContent(
                message_text="🔍 Пожалуйста, укажите более подробное название песни или имя исполнителя.",
                parse_mode="HTML"
            )
        )
        await inline_query.answer([short_hint], cache_time=1, is_personal=True)
        return

    # Дебаунс: если пользователь активно печатает, ждем 350 мс перед запуском тяжелой загрузки
    if user_id:
        async with _USER_QUERY_LOCK:
            _PENDING_USER_QUERIES[user_id] = raw_query

        await asyncio.sleep(0.35)

        async with _USER_QUERY_LOCK:
            if _PENDING_USER_QUERIES.get(user_id) != raw_query:
                # Пользователь успел напечатать следующий символ — отменяем промежуточный поиск
                return

    # Защита от параллельного запуска нескольких скачиваний одного и того же трека (дедупликация)
    task = None
    async with _IN_FLIGHT_LOCK:
        if cache_key in _IN_FLIGHT_DOWNLOADS:
            task = _IN_FLIGHT_DOWNLOADS[cache_key]
        else:
            task = asyncio.create_task(
                _download_and_cache(cache_key, raw_query, url, inline_query.bot)
            )
            _IN_FLIGHT_DOWNLOADS[cache_key] = task
            task.add_done_callback(lambda t: _IN_FLIGHT_DOWNLOADS.pop(cache_key, None))

    file_id = None
    try:
        # Лимит ожидания 4.5 секунды — гарантирует быстрый ответ клиенту Telegram до таймаута интерфейса
        file_id = await asyncio.wait_for(asyncio.shield(task), timeout=4.5)
    except asyncio.TimeoutError:
        logger.info("Inline скачивание '%s' продолжается в фоне", raw_query[:40])
        clean_q = raw_query[:25]
        pending_article = InlineQueryResultArticle(
            id=f"pending_{abs(hash(cache_key)) % 1000000}",
            title=f"⏳ Загрузка: {raw_query[:35]}...",
            description="Трек скачивается в базу. Нажмите сюда или повторите ввод через 5 сек!",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"⏳ <b>Трек обрабатывается:</b> <i>{html.escape(raw_query[:80])}</i>\n\n"
                    "Бот прямо сейчас загружает его в базу в высоком качестве.\n"
                    "Пожалуйста, повторите поиск через несколько секунд или откройте @musicAutoSaver_bot."
                ),
                parse_mode="HTML"
            )
        )
        await inline_query.answer(
            [pending_article],
            cache_time=2,
            is_personal=True,
            switch_pm_text=f"📥 Скачать «{clean_q}» в боте",
            switch_pm_parameter="search"
        )
        return
    except Exception as err:
        logger.warning("Inline ошибка скачивания '%s': %s", raw_query[:40], err)

    # 5. Если трек успешно скачан и получен file_id — отдаем чистый аудиотрек
    if file_id:
        results.append(
            InlineQueryResultCachedAudio(
                id=f"new_{file_id[:16]}",
                audio_file_id=file_id
            )
        )
        await inline_query.answer(results, cache_time=60, is_personal=True)
        return

    # 6. Если ничего не найдено или произошла ошибка
    not_found_article = InlineQueryResultArticle(
        id="not_found",
        title="😔 Ничего не нашёл. Попробуй изменить запрос.",
        description="Попробуйте указать другого исполнителя или точное название трека",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"😔 <b>По запросу ничего не нашлось:</b> <i>{html.escape(raw_query[:100])}</i>\n\n"
                "💡 Попробуйте указать точное название трека и исполнителя в @musicAutoSaver_bot."
            ),
            parse_mode="HTML"
        )
    )
    await inline_query.answer(
        [not_found_article],
        cache_time=3,
        is_personal=True,
        switch_pm_text="🎵 Открыть бота",
        switch_pm_parameter="help"
    )


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen_result: ChosenInlineResult):
    """
    Событие выбора inline-результата пользователем.
    Логирует активность и обновляет счетчик скачиваний.
    """
    user_id = chosen_result.from_user.id if chosen_result.from_user else 0
    logger.info(
        "Inline результат отправлен в чат: user_id=%s result_id=%s query='%s'",
        user_id,
        chosen_result.result_id,
        chosen_result.query[:50]
    )
    if chosen_result.from_user:
        await log_user_activity_async(
            chosen_result.from_user.id,
            chosen_result.from_user.username,
            chosen_result.from_user.full_name
        )
        await increment_user_download_async(chosen_result.from_user.id)
