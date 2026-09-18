import asyncio
import hashlib
import html
import logging
import re
import traceback
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

from config import STORAGE_CHANNEL_ID
from services.database import (
    save_inline_candidate,
    get_inline_candidate_async,
    search_cached_tracks_async,
    get_cached_track_async,
    save_cached_track_async,
)
from services.search import (
    search_tracks_async,
    extract_artist_title_from_query,
    _parse_candidate_title_artist,
    resolve_canonical_candidate_metadata,
)
from services.downloader import download_track, _clean_audio_branding, is_generic_artist_name
from services.extractor import (
    find_first_url,
    resolve_track_url,
    resolve_canonical_track_info_async,
    UnsupportedUrlError,
    UNSUPPORTED_URL_FALLBACK_TEXT,
    clean_unicode_text,
)
from services.persistent_cache import (
    build_source_key,
    get_persistent_track_async,
    save_persistent_track_async,
    invalidate_persistent_track_async,
    get_persistent_search_results_async,
    save_persistent_search_results_async,
    check_metadata_match,
)

logger = logging.getLogger(__name__)

router = Router(name="inline")

# Реестр активных загрузок для предотвращения дублирования при быстрых повторных кликах
_in_flight_downloads: Dict[str, asyncio.Future] = {}
_in_flight_lock = asyncio.Lock()
_active_tasks: set = set()


def get_effective_storage_channel_id() -> Optional[Any]:
    """
    Возвращает очищенный и валидный chat_id канала-хранилища Telegram.
    Поддерживает целочисленные ID каналов (-100...), строки и юзернеймы (@channel).
    """
    if not STORAGE_CHANNEL_ID:
        return None
    raw = str(STORAGE_CHANNEL_ID).strip().strip("'\"")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


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
def is_unusable_file_id_error(err: Exception) -> bool:
    """
    Определяет, указывает ли ошибка Telegram на невалидный или устаревший file_id.
    """
    msg = str(err).lower()
    patterns = [
        "wrong file identifier",
        "invalid file id",
        "file_reference_expired",
        "wrong remote file identifier",
        "can't find file",
        "wrong type of the file",
        "file identifier is not",
        "unusable file_id",
    ]
    return any(p in msg for p in patterns)

@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    """
    Обработчик встроенного поиска (@musicAutoSaver_bot <запрос>).
    КРИТИЧНО: НЕ выполняет скачивание аудио во время поиска!
    Возвращает до 5 легковесных результатов для быстрого отклика Telegram.
    """
    query_text = inline_query.query.strip()

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
        logger.info("INLINE QUERY\nquery=%s\nresults_count=1", query_text)
        print(f"INLINE QUERY\nquery={query_text}\nresults_count=1", flush=True)
        try:
            await inline_query.answer(results=[prompt_article], cache_time=1, is_personal=True)
        except Exception as err:
            logger.error("TELEGRAM answer_inline_query (empty) ERROR: %s", err)
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
        logger.info("INLINE QUERY\nquery=%s\nresults_count=1", query_text)
        print(f"INLINE QUERY\nquery={query_text}\nresults_count=1", flush=True)
        try:
            await inline_query.answer(results=[test_article], cache_time=1, is_personal=True)
        except Exception as err:
            logger.error("TELEGRAM answer_inline_query (test) ERROR: %s", err)
        return

    # 3. Проверка на ввод прямой URL-ссылки
    url_found = find_first_url(query_text)
    if url_found:
        url_results: List[Any] = []
        try:
            track = await resolve_track_url(url_found)
            cand_id = uuid.uuid4().hex[:10]
            thumb_url = track.thumbnail_url or ""
            dur_str = ""
            if track.duration:
                m, s = divmod(int(track.duration), 60)
                dur_str = f"{m}:{s:02d}"

            save_inline_candidate(
                cand_id=cand_id,
                target=track.target,
                title=track.title or "Unknown Track",
                artist=track.artist or "Unknown Artist",
                album=track.album,
                duration=track.duration,
                thumbnail_url=thumb_url
            )

            desc = f"{track.artist} • {dur_str}" if dur_str else (track.artist or "")
            url_article = InlineQueryResultArticle(
                id=f"art_{cand_id}",
                title=track.title or "Unknown Track",
                description=desc,
                thumbnail_url=thumb_url or None,
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"🎵 <b>{html.escape(track.artist or 'Unknown Artist')} — {html.escape(track.title or 'Unknown Track')}</b>\n"
                        f"⏱ <b>Длительность:</b> {dur_str or '—'}\n\n"
                        f"⏳ <i>Подготовка к загрузке...</i>"
                    ),
                    parse_mode="HTML"
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="⏳ Подготавливается...",
                            callback_data=f"inl_status:{cand_id}"
                        )
                    ]]
                )
            )
            url_results.append(url_article)
        except Exception as url_err:
            logger.info("Unsupported or unresolvable URL in inline query: %s (%s)", url_found, url_err)
            fallback_article = InlineQueryResultArticle(
                id="unsupported_url_result",
                title="⚠️ Не удалось распознать эту ссылку",
                description="Отправьте название трека или исполнителя текстом",
                input_message_content=InputTextMessageContent(
                    message_text=UNSUPPORTED_URL_FALLBACK_TEXT,
                    parse_mode="HTML"
                )
            )
            url_results.append(fallback_article)

        logger.info("INLINE QUERY\nquery=%s\nresults_count=%d", query_text, len(url_results))
        print(f"INLINE QUERY\nquery={query_text}\nresults_count={len(url_results)}", flush=True)
        try:
            await inline_query.answer(results=url_results, cache_time=1, is_personal=True)
        except Exception as err:
            logger.error("TELEGRAM answer_inline_query (url) ERROR: %s", err)
        return

    results: List[Any] = []
    seen_keys = set()

    # 4. Быстрая проверка базы данных: уже сохраненные треки отдаем мгновенно через валидный file_id
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

    # 5. Если в кэше меньше 5 треков — выполняем поиск кандидатов
    q_artist, q_title = extract_artist_title_from_query(query_text)
    remaining_slots = 5 - len(results)
    if remaining_slots > 0:
        try:
            # 5a. Проверка persistent search cache (L1 + Cloudflare D1)
            cached_search_candidates = await get_persistent_search_results_async(query_text)
            candidates_to_process: List[Dict[str, Any]] = []

            if cached_search_candidates and isinstance(cached_search_candidates, list):
                candidates_to_process = cached_search_candidates
            else:
                search_items, _ = await search_tracks_async(query_text, limit=5)
                for item in search_items:
                    art, tit, alb, dur, thumb = resolve_canonical_candidate_metadata(
                        item,
                        query_artist=q_artist,
                        query_title=q_title
                    )
                    candidates_to_process.append({
                        "url": item.url,
                        "title": tit,
                        "artist": art,
                        "album": alb,
                        "duration": dur,
                        "thumbnail": thumb or item.thumbnail,
                        "formatted_duration": item.formatted_duration or (f"{dur // 60}:{dur % 60:02d}" if dur > 0 else "")
                    })

                # Сохраняем результаты в persistent search cache
                if candidates_to_process:
                    asyncio.create_task(
                        save_persistent_search_results_async(query_text, candidates_to_process)
                    )

            for cand_data in candidates_to_process:
                if len(results) >= 5:
                    break
                artist = cand_data.get("artist") or "Unknown Artist"
                title = cand_data.get("title") or "Unknown Track"
                album = cand_data.get("album")
                duration = int(cand_data.get("duration") or 0)
                cand_url = cand_data.get("url") or ""
                dur_str = cand_data.get("formatted_duration") or (f"{duration // 60}:{duration % 60:02d}" if duration > 0 else "")

                # Пропускаем, если такой трек уже добавлен из кэша
                sig = f"{artist} - {title}".lower()
                if sig in seen_keys:
                    continue
                seen_keys.add(sig)

                cand_id = uuid.uuid4().hex[:10]
                resolved_thumb_url = _extract_https_thumbnail(cand_url, cand_data.get("thumbnail"))

                save_inline_candidate(
                    cand_id=cand_id,
                    target=cand_url,
                    title=title,
                    artist=artist,
                    album=album,
                    duration=duration,
                    thumbnail_url=resolved_thumb_url
                )

                desc = f"{artist} • {dur_str}" if dur_str else artist

                article = InlineQueryResultArticle(
                    id=f"art_{cand_id}",
                    title=title,
                    description=desc,
                    thumbnail_url=resolved_thumb_url,
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>\n"
                            f"⏱ <b>Длительность:</b> {dur_str or '—'}\n\n"
                            f"⏳ <i>Подготовка к загрузке...</i>"
                        ),
                        parse_mode="HTML"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text="⏳ Подготавливается...",
                                callback_data=f"inl_status:{cand_id}"
                            )
                        ]]
                    )
                )
                results.append(article)
        except Exception as search_err:
            logger.warning("Ошибка поиска YouTube/SoundCloud в inline query: %s", search_err)

    # 6. Если ничего не найдено — возвращаем заглушку
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

    logger.info("INLINE QUERY\nquery=%s\nresults_count=%d", query_text, len(results))
    print(f"INLINE QUERY\nquery={query_text}\nresults_count={len(results)}", flush=True)

    try:
        await inline_query.answer(results=results[:5], cache_time=1, is_personal=True)
    except Exception as api_err:
        logger.error("TELEGRAM answer_inline_query ERROR (%s): %s", type(api_err).__name__, api_err)


def _start_inline_download(
    cand_id: str,
    inline_message_id: Optional[str],
    bot: Bot,
    query: Optional[str] = None
) -> asyncio.Task:
    """Создает и отслеживает фоновую задачу скачивания для выбранного inline-трека."""
    task = asyncio.create_task(
        process_inline_download(
            cand_id=cand_id,
            inline_message_id=inline_message_id,
            bot=bot,
            query=query
        )
    )
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return task


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot):
    """
    Обработчик выбора конкретного inline-результата пользователем.
    Служит основным триггером старта скачивания (1-клик flow).
    """
    logger.info(
        "INLINE SELECTED\nresult_id=%s\ninline_message_id=%s\nquery=%s",
        chosen.result_id,
        chosen.inline_message_id,
        chosen.query
    )
    print(
        f"INLINE SELECTED\nresult_id={chosen.result_id}\ninline_message_id={chosen.inline_message_id}\nquery={chosen.query}",
        flush=True
    )

    if not chosen.result_id or not chosen.result_id.startswith("art_"):
        return

    cand_id = chosen.result_id[4:]
    _start_inline_download(
        cand_id=cand_id,
        inline_message_id=chosen.inline_message_id,
        bot=bot,
        query=chosen.query
    )


async def process_inline_download(
    cand_id: str,
    inline_message_id: Optional[str],
    bot: Bot,
    query: Optional[str] = None
) -> Optional[str]:
    """
    Выполняет загрузку выбранного трека, конвертацию, получение file_id
    и замену inline-сообщения на аудиоплеер через edit_message_media.
    Возвращает Telegram file_id (или None при ошибке).
    """
    candidate = await get_inline_candidate_async(cand_id)
    if not candidate:
        if inline_message_id:
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text="⚠️ Срок действия этой ссылки истёк. Пожалуйста, выполните поиск заново."
                )
            except Exception:
                pass
        return None

    artist = candidate.get("artist") or "Unknown Artist"
    title = candidate.get("title") or "Unknown Track"
    album = candidate.get("album")
    duration = candidate.get("duration")
    target = candidate.get("target") or f"{artist} - {title}"

    source_key, source_type, source_id = build_source_key(target)
    dedup_key = source_key

    # 1. Сразу переводим сообщение в состояние DOWNLOADING и удаляем техническую клавиатуру
    if inline_message_id:
        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=(
                    f"🎵 <b>{html.escape(artist)} — {html.escape(title)}</b>\n\n"
                    f"⏳ Скачиваю..."
                ),
                parse_mode="HTML",
                reply_markup=None
            )
        except Exception as edit_status_err:
            logger.debug("Не удалось обновить статус inline сообщения: %s", edit_status_err)

    # 2. Дедупликация: проверяем реестр активных загрузок
    async with _in_flight_lock:
        if dedup_key in _in_flight_downloads:
            fut = _in_flight_downloads[dedup_key]
            is_first = False
        else:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _in_flight_downloads[dedup_key] = fut
            is_first = True

    if not is_first:
        logger.info("INLINE JOIN IN-FLIGHT: dedup_key=%s", dedup_key)
        try:
            file_id = await fut
        except Exception:
            file_id = None

        if file_id and inline_message_id:
            try:
                await bot.edit_message_media(
                    inline_message_id=inline_message_id,
                    media=InputMediaAudio(
                        media=file_id,
                        title=title,
                        performer=artist,
                        duration=duration or 0
                    )
                )
                logger.info("INLINE MESSAGE EDITED\nresult_id=art_%s\nsuccess=true", cand_id)
                print(f"INLINE MESSAGE EDITED\nresult_id=art_{cand_id}\nsuccess=true", flush=True)
            except Exception as e:
                logger.warning("edit_message_media failed for joined in-flight: %s", e)
        return file_id

    # Первый поток выполняет загрузку
    downloaded = None
    file_id = None
    effective_storage_id = get_effective_storage_channel_id()
    try:
        # 3. Persistent Cache Lookup (L1 RAM + Cloudflare D1)
        cached_track = await get_persistent_track_async(source_key)
        if cached_track and is_valid_telegram_file_id(cached_track.telegram_file_id):
            is_match, cached_hash, current_hash = check_metadata_match(
                cached_track, artist=artist, title=title, album=album, duration=duration
            )
            if is_match:
                file_id = cached_track.telegram_file_id
                logger.info("Persistent cache HIT\nsource_key=%s\nfile_id=%s", source_key, file_id)
            else:
                logger.info(
                    "Persistent cache metadata mismatch -> treating as MISS to refresh ID3 tags:\n"
                    "  source_key=%s\n"
                    "  cached_hash=%s (cached: %r - %r, album=%r)\n"
                    "  current_hash=%s (candidate: %r - %r, album=%r)",
                    source_key,
                    cached_hash, cached_track.artist, cached_track.title, cached_track.album,
                    current_hash, artist, title, album
                )
                file_id = None
        else:
            # Fallback к legacy local SQLite кэшу, если в D1 еще нет
            legacy_cached = await get_cached_track_async(target)
            if not legacy_cached and artist and title:
                legacy_cached = await get_cached_track_async(f"{artist} - {title}")
            cached_file_id = legacy_cached.get("file_id") if legacy_cached else None
            if is_valid_telegram_file_id(cached_file_id):
                file_id = cached_file_id
                logger.info("Persistent cache HIT\nsource_key=%s\nfile_id=%s", source_key, file_id)
                # Асинхронно мигрируем запись в D1
                asyncio.create_task(
                    save_persistent_track_async(
                        source_key=source_key,
                        source_type=source_type,
                        source_id=source_id,
                        artist=legacy_cached.get("artist") or artist,
                        title=legacy_cached.get("title") or title,
                        album=album,
                        duration=legacy_cached.get("duration") or duration,
                        telegram_file_id=file_id
                    )
                )

        # 4. Если в кэше найден file_id — пытаемся сразу применить через edit_message_media
        if file_id:
            if inline_message_id:
                try:
                    await bot.edit_message_media(
                        inline_message_id=inline_message_id,
                        media=InputMediaAudio(
                            media=file_id,
                            title=(cached_track.title if cached_track else title),
                            performer=(cached_track.artist if cached_track else artist),
                            duration=((cached_track.duration if cached_track else duration) or 0)
                        )
                    )
                    logger.info("INLINE MESSAGE EDITED\nresult_id=art_%s\nsuccess=true", cand_id)
                    print(f"INLINE MESSAGE EDITED\nresult_id=art_{cand_id}\nsuccess=true", flush=True)
                    if not fut.done():
                        fut.set_result(file_id)
                    return file_id
                except Exception as media_err:
                    if is_unusable_file_id_error(media_err):
                        logger.info("Invalid Telegram file_id, invalidating cache\nsource_key=%s", source_key)
                        await invalidate_persistent_track_async(source_key, file_id)
                        file_id = None
                    else:
                        raise media_err

        # 5. Если в кэше нет (или file_id был инвалидирован) — скачиваем через download_track()
        if not file_id:
            logger.info("Persistent cache MISS\nsource_key=%s", source_key)
            logger.info("Persistent cache MISS -> downloading\nsource_key=%s", source_key)
            logger.info(
                "INLINE DOWNLOAD EXECUTE:\n"
                "  query=%s\n"
                "  cand_id=%s\n"
                "  source_url=%s\n"
                "  expected_duration=%s\n"
                "  storage_channel_id=%s",
                query or f"{artist} - {title}", cand_id, candidate["target"], candidate.get("duration"), effective_storage_id
            )

            # Приоритет обложек: Authoritative/Studio cover > Canonical cover (Deezer/iTunes) > YouTube fallback
            thumb_to_use = candidate.get("thumbnail_url")
            if artist and title and (not thumb_to_use or "ytimg.com" in thumb_to_use):
                try:
                    canonical = await asyncio.wait_for(
                        resolve_canonical_track_info_async(f"{artist} {title}"),
                        timeout=1.5
                    )
                    if canonical and canonical.thumbnail_url:
                        thumb_to_use = canonical.thumbnail_url
                except Exception as ex:
                    logger.debug("Inline canonical art lookup skipped or timed out: %s", ex)

            req_id = f"inl_{uuid.uuid4().hex[:6]}"
            custom_art = artist if (artist and not is_generic_artist_name(artist)) else None
            custom_tit = title if (title and title.lower() != "unknown track") else None
            downloaded = await download_track(
                query_or_url=candidate["target"],
                custom_title=custom_tit,
                custom_artist=custom_art,
                thumbnail_url=thumb_to_use,
                expected_duration=candidate.get("duration"),
                request_id=req_id,
                custom_album=candidate.get("album")
            )

            logger.info(
                "INLINE DOWNLOAD SUCCESS:\n"
                "  cand_id=%s\n"
                "  downloader_path=%s\n"
                "  downloaded_duration=%s\n"
                "  filesize=%s",
                cand_id, downloaded.file_path, downloaded.duration, downloaded.filesize
            )

            # ВАЖНО: Загрузка ТОЛЬКО в канал хранилища STORAGE_CHANNEL_ID!
            # Запрещено отправлять в ЛС пользователю или администратору!
            if not effective_storage_id:
                logger.error(
                    "Inline storage channel is not configured: STORAGE_CHANNEL_ID environment variable is missing or empty. "
                    "(cand_id=%s, target=%s, raw_STORAGE_CHANNEL_ID=%r)",
                    cand_id, target, STORAGE_CHANNEL_ID
                )
                raise RuntimeError("STORAGE_CHANNEL_ID не настроен")

            thumb_file = FSInputFile(downloaded.thumbnail_path) if downloaded.thumbnail_path and downloaded.thumbnail_path.exists() else None
            audio_file = FSInputFile(downloaded.file_path)

            logger.info(
                "INLINE TELEGRAM UPLOAD ATTEMPT:\n"
                "  cand_id=%s\n"
                "  storage_channel_id=%s\n"
                "  audio_file=%s\n"
                "  has_thumb=%s",
                cand_id, effective_storage_id, downloaded.file_path, bool(thumb_file)
            )

            uploaded_msg = await bot.send_audio(
                chat_id=effective_storage_id,
                audio=audio_file,
                title=downloaded.title,
                performer=downloaded.artist,
                duration=downloaded.duration,
                thumbnail=thumb_file,
                disable_notification=True
            )

            if not uploaded_msg or not uploaded_msg.audio:
                raise RuntimeError("Не удалось получить audio object после загрузки в Telegram")

            file_id = uploaded_msg.audio.file_id

            logger.info("Uploaded audio to Telegram storage\nsource_key=%s\nstorage_message_id=%s", source_key, uploaded_msg.message_id)
            logger.info(
                "INLINE TELEGRAM UPLOAD COMPLETE:\n"
                "  cand_id=%s\n"
                "  storage_channel_id=%s\n"
                "  telegram_file_id=%s",
                cand_id, effective_storage_id, file_id
            )

            # 6. Сохраняем в Persistent Cache (Cloudflare D1 + L1)
            await save_persistent_track_async(
                source_key=source_key,
                source_type=source_type,
                source_id=source_id,
                artist=downloaded.artist,
                title=downloaded.title,
                album=downloaded.album,
                duration=downloaded.duration,
                telegram_file_id=file_id,
                telegram_file_unique_id=uploaded_msg.audio.file_unique_id,
                storage_message_id=uploaded_msg.message_id,
                file_size=downloaded.filesize
            )

        if not fut.done():
            fut.set_result(file_id)

        # 7. Заменяем inline-сообщение в чате на аудиоплеер через edit_message_media
        if inline_message_id and file_id:
            logger.info(
                "INLINE EDIT MESSAGE MEDIA ATTEMPT:\n"
                "  cand_id=%s\n"
                "  inline_message_id=%s\n"
                "  file_id=%s",
                cand_id, inline_message_id, file_id
            )
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaAudio(
                    media=file_id,
                    title=downloaded.title if downloaded else title,
                    performer=downloaded.artist if downloaded else artist,
                    duration=downloaded.duration if downloaded else (candidate.get("duration") or 0)
                )
            )
            logger.info("INLINE MESSAGE EDITED SUCCESS: cand_id=%s, inline_message_id=%s", cand_id, inline_message_id)

        return file_id

    except Exception as err:
        logger.exception(
            "INLINE DOWNLOAD PIPELINE EXCEPTION:\n"
            "  query=%s\n"
            "  cand_id=%s\n"
            "  source_url=%s\n"
            "  expected_duration=%s\n"
            "  downloader_path=%s\n"
            "  storage_channel_id=%s\n"
            "  telegram_upload_success=%s\n"
            "  file_id=%s\n"
            "  exception_type=%s\n"
            "  exception_message=%s",
            query or f"{artist} - {title}",
            cand_id,
            target,
            candidate.get("duration"),
            str(downloaded.file_path) if downloaded else "None",
            effective_storage_id,
            bool(file_id),
            file_id,
            type(err).__name__,
            str(err)
        )
        if not fut.done():
            fut.set_exception(err)
        if inline_message_id:
            try:
                if "STORAGE_CHANNEL_ID" in str(err):
                    err_text = "⚠️ Не удалось подготовить аудио."
                else:
                    err_text = (
                        "⚠️ Не удалось скачать этот трек.\n\n"
                        "Попробуйте другой результат."
                    )
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=err_text,
                    parse_mode="HTML"
                )
            except Exception as edit_err:
                logger.warning("Failed to edit inline message with error text: %s", edit_err)
        return None

    finally:
        if not fut.done():
            fut.set_result(None)
        if downloaded:
            downloaded.cleanup()
        async with _in_flight_lock:
            _in_flight_downloads.pop(dedup_key, None)


@router.callback_query(F.data.startswith("inl_status:"))
async def handle_inline_status_callback(callback: CallbackQuery, bot: Bot):
    """
    Failsafe-обработчик клика по технической кнопке '⏳ Подготавливается...'.
    Если ChosenInlineResult уже отработал, просто уведомляет пользователя.
    Если feedback задержался, запускает скачивание.
    """
    cand_id = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Скачивание уже идёт...", show_alert=False)
    if callback.inline_message_id:
        _start_inline_download(
            cand_id=cand_id,
            inline_message_id=callback.inline_message_id,
            bot=bot
        )
