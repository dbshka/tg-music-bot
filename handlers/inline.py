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

from config import ADMIN_ID
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
from handlers.tag_editor import get_audio_edit_keyboard

logger = logging.getLogger(__name__)
router = Router(name="inline_router")

# Защита от одновременных дублирующих скачиваний одного и того же трека при быстром вводе текста
_IN_FLIGHT_DOWNLOADS: Dict[str, asyncio.Task] = {}
_IN_FLIGHT_LOCK = asyncio.Lock()


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
        await inline_query.answer([hint_article], cache_time=300, is_personal=True)
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

    # Формируем InlineQueryResultCachedAudio для найденных треков из кэша
    for idx, c in enumerate(cached_candidates[:5]):
        file_id = c["file_id"]
        title = c.get("title") or "Unknown Track"
        artist = c.get("artist") or "Unknown Artist"
        caption = f"🎧 <b>{html.escape(artist)} — {html.escape(title)}</b>\nvia @musicAutoSaver_bot"
        results.append(
            InlineQueryResultCachedAudio(
                id=f"cached_{file_id[:16]}_{idx}",
                audio_file_id=file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_audio_edit_keyboard()
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
        await inline_query.answer([short_hint], cache_time=10, is_personal=True)
        return

    # Защита от параллельного запуска нескольких скачиваний одного и того же трека (дедупликация)
    task = None
    async with _IN_FLIGHT_LOCK:
        if cache_key in _IN_FLIGHT_DOWNLOADS:
            task = _IN_FLIGHT_DOWNLOADS[cache_key]
        else:
            req_id = uuid.uuid4().hex[:6]

            async def _run_download():
                async with DOWNLOAD_SEMAPHORE:
                    if url:
                        track_info = await resolve_track_url(url)
                        return await download_track(
                            query_or_url=track_info.target,
                            custom_title=track_info.title,
                            custom_artist=track_info.artist,
                            thumbnail_url=track_info.thumbnail_url,
                            expected_duration=track_info.duration,
                            request_id=f"in_{req_id}"
                        )
                    else:
                        return await download_track(
                            query_or_url=f"ytsearch1:{raw_query}",
                            request_id=f"in_{req_id}"
                        )

            task = asyncio.create_task(_run_download())
            _IN_FLIGHT_DOWNLOADS[cache_key] = task

    downloaded_audio: Optional[DownloadedAudio] = None
    try:
        # Жесткий таймаут 7.5 секунд, чтобы уложиться в лимит Telegram API на ответ
        downloaded_audio = await asyncio.wait_for(asyncio.shield(task), timeout=7.5)
    except asyncio.TimeoutError:
        logger.warning("Inline скачивание превысило таймаут 7.5с для '%s'", raw_query[:40])
    except Exception as err:
        logger.warning("Inline ошибка скачивания '%s': %s", raw_query[:40], err)
    finally:
        async with _IN_FLIGHT_LOCK:
            if cache_key in _IN_FLIGHT_DOWNLOADS and _IN_FLIGHT_DOWNLOADS[cache_key].done():
                _IN_FLIGHT_DOWNLOADS.pop(cache_key, None)

    # Если скачивание завершилось успешно — загружаем в Telegram для получения file_id
    if downloaded_audio:
        try:
            audio_path = Path(downloaded_audio.file_path)
            thumb_path = downloaded_audio.thumbnail_path
            thumb_file = (
                FSInputFile(thumb_path)
                if (thumb_path and thumb_path.exists() and thumb_path.is_file() and thumb_path.stat().st_size > 0)
                else None
            )

            # Для получения file_id отправляем аудио пользователю или админу
            sent_msg = None
            try:
                sent_msg = await inline_query.bot.send_audio(
                    chat_id=user_id,
                    audio=FSInputFile(audio_path),
                    title=downloaded_audio.title,
                    performer=downloaded_audio.artist,
                    duration=downloaded_audio.duration,
                    thumbnail=thumb_file,
                    reply_markup=get_audio_edit_keyboard()
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                # Если пользователь не писал боту в ЛС, используем чат ADMIN_ID для получения file_id
                if ADMIN_ID and ADMIN_ID != user_id:
                    try:
                        sent_msg = await inline_query.bot.send_audio(
                            chat_id=ADMIN_ID,
                            audio=FSInputFile(audio_path),
                            title=downloaded_audio.title,
                            performer=downloaded_audio.artist,
                            duration=downloaded_audio.duration,
                            thumbnail=thumb_file
                        )
                    except Exception as admin_send_err:
                        logger.warning("Не удалось отправить аудио в чат администратора: %s", admin_send_err)

            if sent_msg and sent_msg.audio:
                file_id = sent_msg.audio.file_id
                await save_cached_track_async(
                    query=cache_key,
                    file_id=file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )
                caption = f"🎧 <b>{html.escape(downloaded_audio.artist)} — {html.escape(downloaded_audio.title)}</b>\nvia @musicAutoSaver_bot"
                results.append(
                    InlineQueryResultCachedAudio(
                        id=f"new_{file_id[:16]}",
                        audio_file_id=file_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=get_audio_edit_keyboard()
                    )
                )
        except Exception as upload_err:
            logger.error("Ошибка загрузки аудио в Telegram для inline file_id: %s", upload_err)
        finally:
            downloaded_audio.cleanup()

    # 5. Если результаты есть — отдаем в Telegram
    if results:
        await inline_query.answer(results, cache_time=30, is_personal=True)
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
    await inline_query.answer([not_found_article], cache_time=10, is_personal=True)


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
