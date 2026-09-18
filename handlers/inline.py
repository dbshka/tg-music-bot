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
from services.downloader import download_track, _clean_audio_branding, is_generic_artist_name
from services.extractor import (
    find_first_url,
    resolve_track_url,
    resolve_canonical_track_info_async,
    UnsupportedUrlError,
    UNSUPPORTED_URL_FALLBACK_TEXT,
    clean_unicode_text,
)

logger = logging.getLogger(__name__)

router = Router(name="inline")

# Реестр активных загрузок для предотвращения дублирования при быстрых повторных кликах
_in_flight_downloads: Dict[str, asyncio.Future] = {}
_in_flight_lock = asyncio.Lock()
_active_tasks: set = set()


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


def extract_artist_title_from_query(query: str) -> tuple[Optional[str], Optional[str]]:
    """
    Извлекает (artist, title) из поискового запроса пользователя вида 'Исполнитель — Название'.
    Поддерживает варианты разделителей: ' -- ', '--', ' — ', '—', ' – ', '–', ' - '.
    """
    if not query:
        return None, None
    clean_q = clean_unicode_text(query).strip()
    for pattern in [r'\s*(?:--|—|–)\s*', r'\s+-\s+']:
        parts = re.split(pattern, clean_q, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None, None


def _parse_candidate_title_artist(
    raw_title: str,
    uploader: Optional[str],
    query_artist: Optional[str] = None,
    query_title: Optional[str] = None
) -> tuple[str, str]:
    """
    Разделяет строку на исполнителя и название трека с удалением брендинга.
    YouTube uploader (например, 'Release', '... - Topic', лейблы) НИКОГДА
    не подменяет реального исполнителя из запроса или названия видео.
    """
    cleaned = clean_unicode_text(raw_title).strip()
    # Удаляем распространенные суффиксы клипов/аудио
    cleaned = re.sub(
        r'\s*[\(\[](?:Official\s*(?:Music\s*)?Video|Official\s*Audio|Lyric\s*Video|Video|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
        '',
        cleaned,
        flags=re.IGNORECASE
    ).strip()

    artist = None
    title = None

    # 1. Если в самом названии видео есть разделитель "Исполнитель - Название"
    for sep in [" - ", " — ", " – ", " -- "]:
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            cand_art = _clean_audio_branding(parts[0].strip()) or parts[0].strip()
            cand_tit = _clean_audio_branding(parts[1].strip()) or parts[1].strip()
            if cand_art and not is_generic_artist_name(cand_art):
                artist = cand_art
                title = cand_tit
                break

    # 2. Если в названии видео нет разделителя (или исполнитель generic),
    # используем исполнителя из запроса пользователя (например: 'Макулатура -- Запястья')
    if (not artist or is_generic_artist_name(artist)) and query_artist and not is_generic_artist_name(query_artist):
        artist = _clean_audio_branding(query_artist) or query_artist
        if not title:
            title = _clean_audio_branding(cleaned) or query_title or cleaned

    # 3. Если исполнитель всё ещё не определен, проверяем uploader (только если не generic!)
    if not artist or is_generic_artist_name(artist):
        raw_uploader = (uploader or "").replace(" - Topic", "").replace("- Topic", "").replace(" – Topic", "").strip()
        cleaned_up = _clean_audio_branding(raw_uploader) if raw_uploader else ""
        if cleaned_up and not is_generic_artist_name(cleaned_up):
            artist = cleaned_up

    # 4. Финальный fallback: гарантируем, что 'Release' или generic имя не вернется
    if not artist or is_generic_artist_name(artist):
        artist = query_artist if (query_artist and not is_generic_artist_name(query_artist)) else "Unknown Artist"

    if not title:
        title = _clean_audio_branding(cleaned) or query_title or cleaned or "Unknown Track"

    return artist, title


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

    # 5. Если в кэше меньше 5 треков — выполняем плоский поиск кандидатов (extract_flat)
    q_artist, q_title = extract_artist_title_from_query(query_text)
    remaining_slots = 5 - len(results)
    if remaining_slots > 0:
        try:
            search_items, _ = await search_tracks_async(query_text, limit=5)
            for item in search_items:
                if len(results) >= 5:
                    break
                artist, title = _parse_candidate_title_artist(
                    item.title,
                    item.uploader,
                    query_artist=q_artist,
                    query_title=q_title
                )
                title = title or item.title or "Unknown Track"
                if not artist or is_generic_artist_name(artist):
                    artist = q_artist if (q_artist and not is_generic_artist_name(q_artist)) else "Unknown Artist"
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

                article = InlineQueryResultArticle(
                    id=f"art_{cand_id}",
                    title=title,
                    description=desc,
                    thumbnail_url=thumb_url,
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
    target = candidate.get("target") or f"{artist} - {title}"
    dedup_key = target.strip()

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
                        duration=candidate.get("duration") or 0
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
    try:
        # 3. Проверка кэша базы данных: возможно, файл уже загружен
        cached = await get_cached_track_async(target)
        if not cached and artist and title:
            cached = await get_cached_track_async(f"{artist} - {title}")

        cached_file_id = cached.get("file_id") if cached else None
        if is_valid_telegram_file_id(cached_file_id):
            file_id = cached_file_id

        # 4. Если в кэше нет — скачиваем через существующий download_track()
        if not file_id:
            logger.info("INLINE DOWNLOAD START\nresult_id=art_%s", cand_id)
            print(f"INLINE DOWNLOAD START\nresult_id=art_{cand_id}", flush=True)

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
            downloaded = await download_track(
                query_or_url=candidate["target"],
                custom_title=title,
                custom_artist=artist,
                thumbnail_url=thumb_to_use,
                expected_duration=candidate.get("duration"),
                request_id=req_id,
                custom_album=candidate.get("album")
            )

            # 5. Загружаем аудиофайл в Telegram storage (STORAGE_CHANNEL_ID или ADMIN_ID)
            # ВАЖНО: НИКОГДА не отправлять в ЛС пользователю (from_user.id)
            storage_chat_id = STORAGE_CHANNEL_ID or ADMIN_ID
            if not storage_chat_id:
                raise RuntimeError("STORAGE_CHANNEL_ID или ADMIN_ID не настроены в config.py")

            thumb_file = FSInputFile(downloaded.thumbnail_path) if downloaded.thumbnail_path and downloaded.thumbnail_path.exists() else None
            audio_file = FSInputFile(downloaded.file_path)

            uploaded_msg = await bot.send_audio(
                chat_id=storage_chat_id,
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

            # Если отправка была в ADMIN_ID, удаляем сообщение, чтобы не засорять чат админа
            if storage_chat_id == ADMIN_ID:
                try:
                    await bot.delete_message(chat_id=ADMIN_ID, message_id=uploaded_msg.message_id)
                except Exception:
                    pass

            # 6. Сохраняем в кэш базы данных
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

            logger.info("INLINE DOWNLOAD COMPLETE\nresult_id=art_%s\ntelegram_file_id_saved=true", cand_id)
            print(f"INLINE DOWNLOAD COMPLETE\nresult_id=art_{cand_id}\ntelegram_file_id_saved=true", flush=True)

        if not fut.done():
            fut.set_result(file_id)

        # 7. Заменяем inline-сообщение в чате на аудиоплеер через edit_message_media
        if inline_message_id and file_id:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaAudio(
                    media=file_id,
                    title=downloaded.title if downloaded else title,
                    performer=downloaded.artist if downloaded else artist,
                    duration=downloaded.duration if downloaded else (candidate.get("duration") or 0)
                )
            )
            logger.info("INLINE MESSAGE EDITED\nresult_id=art_%s\nsuccess=true", cand_id)
            print(f"INLINE MESSAGE EDITED\nresult_id=art_{cand_id}\nsuccess=true", flush=True)

        return file_id

    except Exception as err:
        logger.error("Ошибка при обработке inline download: %s", err)
        if not fut.done():
            fut.set_exception(err)
        if inline_message_id:
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=(
                        "⚠️ Не удалось скачать этот трек.\n\n"
                        "Попробуйте другой результат."
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
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
