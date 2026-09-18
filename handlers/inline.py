import asyncio
import hashlib
import html
import logging
import re
import uuid
from typing import Dict, Optional, Any, List

from aiogram import Router, F, Bot
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ChosenInlineResult,
    InputMediaAudio,
    FSInputFile
)

from config import STORAGE_CHANNEL_ID, ADMIN_ID
from services.database import (
    save_inline_candidate,
    get_inline_candidate_async,
    search_cached_tracks_async,
    get_cached_track_async,
    save_cached_track_async,
)
from services.search import search_tracks_async
from services.downloader import download_track, _clean_audio_branding
from services.extractor import clean_unicode_text

logger = logging.getLogger(__name__)

router = Router(name="inline")

# Реестр активных загрузок для предотвращения дублирования при быстрых повторных кликах
_in_flight_downloads: Dict[str, asyncio.Future] = {}
_in_flight_lock = asyncio.Lock()


def is_valid_telegram_file_id(file_id: Optional[str]) -> bool:
    """
    Проверяет, что file_id является реальным Telegram audio file_id,
    а не mock-строкой, URL, файловым путем или ключом кэша.
    """
    if not file_id or not isinstance(file_id, str):
        return False
    # Исключаем тестовые mock-файлы, ссылки и локальные пути
    if file_id.startswith(("mock_", "test_", "http://", "https://", "/", "\\", "C:", "file:", "yt_", "sc_")):
        return False
    # Настоящие Telegram audio file_id имеют достаточную длину (обычно 20-200 символов) и состоят из base64url
    if len(file_id) < 20 or len(file_id) > 200:
        return False
    if not re.match(r'^[A-Za-z0-9_-]+$', file_id):
        return False
    return True


def _extract_https_thumbnail(url: str, default_thumb: Optional[str]) -> Optional[str]:
    """Извлекает гарантированный HTTPS-URL обложки (для YouTube или переданного thumbnail)."""
    if default_thumb and default_thumb.startswith("https://"):
        return default_thumb
    if url:
        m = re.search(r'(?:v=|\/shorts\/|\/embed\/|youtu\.be\/)([a-zA-Z0-9_-]{11})', url)
        if m:
            return f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg"
    return None


def _parse_candidate_title_artist(raw_title: str, uploader: Optional[str]) -> tuple[str, str]:
    """Разделяет строку на исполнителя и название трека с удалением брендинга."""
    cleaned = clean_unicode_text(raw_title).strip()
    # Удаляем распространенные суффиксы клипов/аудио
    cleaned = re.sub(
        r'\s*[\(\[](?:Official\s*(?:Music\s*)?Video|Official\s*Audio|Lyric\s*Video|Video|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
        '',
        cleaned,
        flags=re.IGNORECASE
    ).strip()

    if " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        artist = _clean_audio_branding(parts[0].strip()) or parts[0].strip()
        title = _clean_audio_branding(parts[1].strip()) or parts[1].strip()
    elif " — " in cleaned:
        parts = cleaned.split(" — ", 1)
        artist = _clean_audio_branding(parts[0].strip()) or parts[0].strip()
        title = _clean_audio_branding(parts[1].strip()) or parts[1].strip()
    else:
        raw_uploader = (uploader or "").replace(" - Topic", "").replace("- Topic", "").strip()
        artist = _clean_audio_branding(raw_uploader) if raw_uploader else "Unknown Artist"
        title = _clean_audio_branding(cleaned) or cleaned

    return artist, title


@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    """
    Обработчик встроенного поиска (@musicAutoSaver_bot <запрос>).
    КРИТИЧНО: НЕ выполняет скачивание аудио во время поиска!
    Возвращает до 5 легковесных результатов для быстрого отклика Telegram.
    """
    query_text = inline_query.query.strip()
    logger.info("INLINE QUERY RECEIVED\nquery=%s", query_text)
    print(f"INLINE QUERY RECEIVED\nquery={query_text}", flush=True)

    # 1. Пустой запрос — выводим обучающую подсказку
    if not query_text:
        prompt_article = InlineQueryResultArticle(
            id="empty_query_prompt",
            title="🎵 Введите исполнителя и название трека",
            description="Например: @musicAutoSaver_bot The Weeknd Blinding Lights",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "🎵 <b>Поиск музыки</b>\n\n"
                    "Чтобы найти трек, введите его название или автора после имени бота:\n"
                    "<code>@musicAutoSaver_bot Исполнитель — Название</code>"
                ),
                parse_mode="HTML"
            )
        )
        try:
            ans = await inline_query.answer(results=[prompt_article], cache_time=1, is_personal=True)
            logger.info("TELEGRAM answer_inline_query (empty) SUCCESS: %s", ans)
        except Exception as err:
            logger.error("TELEGRAM answer_inline_query (empty) ERROR (%s): %s", type(err).__name__, err)
        return

    # 2. Диагностический запрос: "test", "тест", "ping"
    if query_text.lower() in ("test", "тест", "ping"):
        test_article = InlineQueryResultArticle(
            id="diag_test_ok",
            title="🎵 Тест Inline",
            description="Inline Mode работает",
            input_message_content=InputTextMessageContent(
                message_text="🎵 <b>Тест Inline</b>\nInline Mode работает!",
                parse_mode="HTML"
            )
        )
        logger.info("DIAGNOSTIC TEST QUERY MATCHED -> Returning minimal article")
        try:
            ans = await inline_query.answer(results=[test_article], cache_time=1, is_personal=True)
            logger.info("TELEGRAM answer_inline_query (test) SUCCESS: %s", ans)
        except Exception as err:
            logger.error("TELEGRAM answer_inline_query (test) ERROR (%s): %s", type(err).__name__, err)
        return

    results: List[Any] = []
    seen_keys = set()

    # 3. Быстрая проверка базы данных: уже сохраненные треки отдаем мгновенно через валидный file_id
    try:
        cached_tracks = await search_cached_tracks_async(query_text, limit=5)
        for c in cached_tracks:
            c_file_id = c.get("file_id")
            # Проверяем, что file_id реальный, а не mock/локальный путь
            if not is_valid_telegram_file_id(c_file_id):
                continue
            key = f"{c.get('artist', '')} - {c.get('title', '')}".lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            result_id = f"ca_{hashlib.md5(c_file_id.encode()).hexdigest()[:10]}"
            artist_name = c.get("artist") or "Unknown Artist"
            title_name = c.get("title") or "Unknown Track"
            results.append(
                InlineQueryResultCachedAudio(
                    id=result_id,
                    audio_file_id=c_file_id,
                    caption=f"🎵 <b>{html.escape(artist_name)} — {html.escape(title_name)}</b>",
                    parse_mode="HTML"
                )
            )
            if len(results) >= 5:
                break
    except Exception as e:
        logger.warning("Ошибка проверки кэша в inline query: %s", e)

    # 4. Если в кэше меньше 5 треков — выполняем плоский поиск кандидатов (extract_flat)
    remaining_slots = 5 - len(results)
    if remaining_slots > 0:
        try:
            search_items, _ = await search_tracks_async(query_text, limit=5)
            for item in search_items:
                if len(results) >= 5:
                    break
                artist, title = _parse_candidate_title_artist(item.title, item.uploader)
                title = title or item.title or "Unknown Track"
                artist = artist or item.uploader or "Unknown Artist"
                dur_str = item.formatted_duration or ""

                # Пропускаем, если такой трек уже добавлен из кэша
                sig = f"{artist} - {title}".lower()
                if sig in seen_keys:
                    continue
                seen_keys.add(sig)

                cand_id = uuid.uuid4().hex[:10]
                thumb_url = _extract_https_thumbnail(item.url, item.thumbnail)

                save_inline_candidate(
                    cand_id=cand_id,
                    target=item.url,
                    title=title,
                    artist=artist,
                    album=None,
                    duration=item.duration,
                    thumbnail_url=thumb_url
                )

                desc = f"{artist} • {dur_str}" if dur_str else artist
                btn_text = f"⬇️ Скачать MP3 ({dur_str})" if dur_str else "⬇️ Скачать MP3"

                article = InlineQueryResultArticle(
                    id=f"art_{cand_id}",
                    title=title,
                    description=desc,
                    thumbnail_url=thumb_url,
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>\n"
                            f"⏱ <b>Длительность:</b> {dur_str or '—'}\n\n"
                            f"<i>Нажмите кнопку ниже, чтобы скачать MP3:</i>"
                        ),
                        parse_mode="HTML"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text=btn_text,
                                callback_data=f"inldl:{cand_id}"
                            )
                        ]]
                    )
                )
                results.append(article)
        except Exception as search_err:
            logger.warning("Ошибка поиска YouTube/SoundCloud в inline query: %s", search_err)

    # 5. Если ничего не найдено — возвращаем заглушку
    if not results:
        results.append(
            InlineQueryResultArticle(
                id="no_results_found",
                title="🔍 Ничего не найдено",
                description=f"По запросу «{query_text[:40]}» подходящих треков не найдено",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"🔍 По запросу <b>«{html.escape(query_text)}»</b> ничего не найдено.\n\n"
                        f"💡 Попробуйте уточнить имя исполнителя или название трека."
                    ),
                    parse_mode="HTML"
                )
            )
        )

    # Логирование сформированных результатов в требуемом формате
    logger.info("SEARCH RESULT COUNT=%d", len(results))
    print(f"SEARCH RESULT COUNT={len(results)}", flush=True)
    for idx, r in enumerate(results[:5], start=1):
        r_type = getattr(r, "type", type(r).__name__)
        r_id = getattr(r, "id", "")
        r_title = getattr(r, "title", "")
        r_performer = getattr(r, "performer", "")
        r_desc = getattr(r, "description", "")
        r_thumb = getattr(r, "thumbnail_url", "")
        r_file_id = getattr(r, "audio_file_id", "")
        r_log = (
            f"RESULT #{idx}:\n"
            f"type={r_type}\n"
            f"id={r_id}\n"
            f"title={r_title}\n"
            f"performer={r_performer}\n"
            f"description={r_desc}\n"
            f"thumbnail_url={r_thumb}\n"
            f"audio_file_id={r_file_id}"
        )
        logger.info(r_log)
        print(r_log, flush=True)

    try:
        ans_res = await inline_query.answer(results=results[:5], cache_time=1, is_personal=True)
        logger.info("TELEGRAM answer_inline_query SUCCESS: %s", ans_res)
        print(f"TELEGRAM answer_inline_query SUCCESS: {ans_res}", flush=True)
    except Exception as api_err:
        logger.error("TELEGRAM answer_inline_query ERROR (%s): %s", type(api_err).__name__, api_err)
        print(f"TELEGRAM answer_inline_query ERROR ({type(api_err).__name__}): {api_err}", flush=True)


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult):
    """Логирует выбор пользователем конкретного inline-результата (Telegram feedback)."""
    logger.info(
        "CHOSEN INLINE RESULT: result_id=%s from_user=%s query='%s' inline_message_id=%s",
        chosen.result_id,
        chosen.from_user.id if chosen.from_user else None,
        chosen.query,
        chosen.inline_message_id
    )
    print(
        f"CHOSEN INLINE RESULT: result_id={chosen.result_id} from_user={chosen.from_user.id if chosen.from_user else None} query='{chosen.query}'",
        flush=True
    )


@router.callback_query(F.data.startswith("inldl:"))
async def handle_inline_download(callback: CallbackQuery, bot: Bot):
    """
    Обработчик нажатия кнопки «⬇️ Скачать MP3» под inline-сообщением.
    Запускает скачивание через существующий пайплайн download_track()
    с защитой от множественных повторных кликов (дедупликация).
    """
    cand_id = callback.data.split(":", 1)[1]
    candidate = await get_inline_candidate_async(cand_id)
    if not candidate:
        await callback.answer(
            "⚠️ Срок действия этой ссылки истёк. Пожалуйста, выполните поиск заново.",
            show_alert=True
        )
        return

    # Защита от дубликатов при быстрых кликах нескольких пользователей или одного пользователя
    async with _in_flight_lock:
        if cand_id in _in_flight_downloads:
            fut = _in_flight_downloads[cand_id]
            is_first = False
        else:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _in_flight_downloads[cand_id] = fut
            is_first = True

    if not is_first:
        await callback.answer("⏳ Этот трек уже скачивается, пожалуйста подождите...", show_alert=False)
        try:
            await fut
        except Exception:
            pass
        return

    downloaded = None
    try:
        await callback.answer("⏳ Скачивание начато...")

        # Обновляем текст сообщения, показывая статус скачивания
        if callback.inline_message_id:
            try:
                await bot.edit_message_text(
                    inline_message_id=callback.inline_message_id,
                    text=(
                        f"⏳ <b>Скачиваю:</b> {html.escape(candidate['artist'])} — {html.escape(candidate['title'])}...\n"
                        f"<i>Загрузка аудиопотока и сохранение тегов...</i>"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass

        # 1. Проверяем кэш базы данных: возможно, файл уже есть
        cached = await get_cached_track_async(candidate["target"])
        if not cached and candidate.get("artist") and candidate.get("title"):
            cached = await get_cached_track_async(f"{candidate['artist']} - {candidate['title']}")

        file_id = cached.get("file_id") if cached else None
        if file_id and not is_valid_telegram_file_id(file_id):
            file_id = None

        # 2. Если в кэше нет — скачиваем через download_track()
        if not file_id:
            req_id = f"inl_{uuid.uuid4().hex[:6]}"
            try:
                downloaded = await download_track(
                    query_or_url=candidate["target"],
                    custom_title=candidate["title"],
                    custom_artist=candidate["artist"],
                    thumbnail_url=candidate.get("thumbnail_url"),
                    expected_duration=candidate.get("duration"),
                    request_id=req_id,
                    custom_album=candidate.get("album")
                )
            except Exception as dl_err:
                logger.error("Inline download_track failed: %s", dl_err)
                err_text = (
                    f"⚠️ Не удалось скачать трек «{html.escape(candidate['artist'])} — {html.escape(candidate['title'])}».\n\n"
                    f"💡 Попробуйте отправить название трека в личный чат с ботом."
                )
                if callback.inline_message_id:
                    try:
                        await bot.edit_message_text(
                            inline_message_id=callback.inline_message_id,
                            text=err_text,
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                if not fut.done():
                    fut.set_exception(dl_err)
                return

        # 3. Отправка и обновление в чате
        if file_id:
            if callback.inline_message_id:
                try:
                    await bot.edit_message_media(
                        inline_message_id=callback.inline_message_id,
                        media=InputMediaAudio(
                            media=file_id,
                            title=candidate["title"],
                            performer=candidate["artist"],
                            duration=candidate.get("duration") or 0
                        )
                    )
                except Exception as e:
                    logger.warning("edit_message_media с кэшированным file_id не удался: %s", e)
            elif callback.message:
                await callback.message.answer_audio(
                    audio=file_id,
                    title=candidate["title"],
                    performer=candidate["artist"],
                    duration=candidate.get("duration") or 0
                )
            if not fut.done():
                fut.set_result(file_id)
            return

        # Если файл только что скачан
        if downloaded and downloaded.file_path and downloaded.file_path.exists():
            thumb_file = FSInputFile(downloaded.thumbnail_path) if downloaded.thumbnail_path and downloaded.thumbnail_path.exists() else None
            audio_file = FSInputFile(downloaded.file_path)

            uploaded_msg = None
            dest_chat_id = STORAGE_CHANNEL_ID or callback.from_user.id or ADMIN_ID
            try:
                uploaded_msg = await bot.send_audio(
                    chat_id=dest_chat_id,
                    audio=audio_file,
                    title=downloaded.title,
                    performer=downloaded.artist,
                    duration=downloaded.duration,
                    thumbnail=thumb_file
                )
            except Exception as up_err:
                logger.info("Upload to dest_chat_id=%s failed (%s), trying ADMIN_ID...", dest_chat_id, up_err)
                if ADMIN_ID and dest_chat_id != ADMIN_ID:
                    try:
                        uploaded_msg = await bot.send_audio(
                            chat_id=ADMIN_ID,
                            audio=audio_file,
                            title=downloaded.title,
                            performer=downloaded.artist,
                            duration=downloaded.duration,
                            thumbnail=thumb_file
                        )
                    except Exception:
                        pass

            if uploaded_msg and uploaded_msg.audio:
                file_id = uploaded_msg.audio.file_id
                # Сохраняем в кэш для будущих запросов
                await save_cached_track_async(
                    query=candidate["target"],
                    file_id=file_id,
                    title=downloaded.title,
                    artist=downloaded.artist,
                    duration=downloaded.duration
                )
                await save_cached_track_async(
                    query=f"{downloaded.artist} - {downloaded.title}",
                    file_id=file_id,
                    title=downloaded.title,
                    artist=downloaded.artist,
                    duration=downloaded.duration
                )

                if callback.inline_message_id:
                    try:
                        await bot.edit_message_media(
                            inline_message_id=callback.inline_message_id,
                            media=InputMediaAudio(
                                media=file_id,
                                title=downloaded.title,
                                performer=downloaded.artist,
                                duration=downloaded.duration
                            )
                        )
                    except Exception as edit_err:
                        logger.warning("edit_message_media failed: %s", edit_err)
                elif callback.message:
                    try:
                        await callback.message.answer_audio(
                            audio=file_id,
                            title=downloaded.title,
                            performer=downloaded.artist,
                            duration=downloaded.duration,
                            thumbnail=thumb_file
                        )
                    except Exception:
                        pass
                if not fut.done():
                    fut.set_result(file_id)
            else:
                if callback.inline_message_id:
                    try:
                        await bot.edit_message_text(
                            inline_message_id=callback.inline_message_id,
                            text=(
                                f"✅ Трек <b>{html.escape(downloaded.artist)} — {html.escape(downloaded.title)}</b> готов!\n\n"
                                f"💡 Откройте диалог с ботом для получения аудиофайла."
                            ),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                if not fut.done():
                    fut.set_result(None)

    except Exception as e:
        logger.exception("Ошибка при обработке inline-скачивания: %s", e)
        if not fut.done():
            fut.set_exception(e)
    finally:
        if not fut.done():
            fut.set_result(None)
        if downloaded:
            downloaded.cleanup()
        async with _in_flight_lock:
            _in_flight_downloads.pop(cand_id, None)
