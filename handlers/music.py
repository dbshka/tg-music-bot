import asyncio
import html
import logging
import os
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    FSInputFile,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.exceptions import TelegramAPIError

from config import MAX_FILE_SIZE_BYTES
from services.extractor import (
    find_first_url,
    resolve_track_url,
    resolve_text_to_track_info,
    resolve_canonical_track_info_async,
    has_track_modifiers,
    extract_track_modifiers,
    parse_url_and_modifiers,
    ExtractedTrack,
    UnsupportedUrlError,
)
from services.downloader import (
    download_track,
    DOWNLOAD_SEMAPHORE,
    log_memory_stage,
)
from services.database import (
    register_user_async,
    log_user_activity_async,
    increment_user_download_async,
    get_cached_track_async,
    save_cached_track_async,
    invalidate_cached_file_id_async
)
from services.identity import (
    extract_modifiers,
    format_track_display,
    is_candidate_matching_modifiers,
    parse_multi_artist_query,
)
from services.persistent_cache import (
    build_source_key,
    get_persistent_track_async,
    save_persistent_track_async,
    invalidate_persistent_track_async,
    check_metadata_match,
)
from handlers.inline import is_valid_telegram_file_id
from handlers.tag_editor import get_audio_edit_keyboard

logger = logging.getLogger(__name__)

router = Router(name="music_router")


class SearchFSM(StatesGroup):
    waiting_for_artist = State()
    waiting_for_title = State()


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура с кнопкой поиска по автору и названию."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Найти песню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_cancel_reply_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены в режиме поиска."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id if message.from_user else message.chat.id
    chat_id = message.chat.id
    username = message.from_user.username if message.from_user else None
    full_name = message.from_user.full_name if message.from_user else None
    await register_user_async(user_id=user_id, chat_id=chat_id, username=username, full_name=full_name)
    start_text = (
        "Поиск и загрузка музыки в MP3.\n\n"
        "Отправьте ссылку на трек или напишите:\n"
        "<code>Исполнитель — Название</code>\n\n"
        "Если исполнителей два или больше, укажите их через запятую:\n"
        "<code>Исполнитель 1, Исполнитель 2 — Название</code>\n\n"
        "Поддерживаемые платформы:\n"
        "Spotify · Apple Music · YouTube · SoundCloud · Яндекс Музыка\n\n"
        "Формат аудио: MP3.\n\n"
        "Также можно отправить аудиофайл и изменить его теги."
    )
    await message.answer(start_text, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "Как пользоваться\n\n"
        "1. Загрузка по ссылке\n\n"
        "Отправьте ссылку на отдельный трек из одной из поддерживаемых платформ:\n"
        "Spotify · Apple Music · YouTube · SoundCloud · Яндекс Музыка\n\n"
        "Формат аудио: MP3.\n\n"
        "2. Поиск по названию\n\n"
        "Напишите:\n"
        "<code>Исполнитель — Название</code>\n\n"
        "Если исполнителей несколько:\n"
        "<code>Исполнитель 1, Исполнитель 2 — Название</code>\n\n"
        "Также можно использовать /search.\n\n"
        "3. Редактирование тегов\n\n"
        "Отправьте аудиофайл или нажмите «Изменить теги» под отправленным треком.\n\n"
        "Команды:\n\n"
        "/start — Главное меню\n"
        "/search — Поиск трека\n"
        "/cancel — Отмена действия\n"
        "/help — Эта справка"
    )
    await message.answer(help_text, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")


@router.message(Command("version"))
async def cmd_version(message: Message):
    from config import BOT_VERSION
    await message.answer(f"Версия: v{BOT_VERSION}")


@router.message(Command("cancel"))
@router.message(F.text.in_({"Отмена", "❌ Отмена"}))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("Поиск отменён.", reply_markup=get_main_reply_keyboard())
    else:
        await message.answer("Сейчас нечего отменять.", reply_markup=get_main_reply_keyboard())


@router.callback_query(F.data == "search:cancel")
async def cb_search_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Поиск отменён.")
    try:
        await callback.message.edit_text("Поиск отменён.")
    except Exception:
        pass


@router.message(F.text.in_({"Найти песню", "🔍 Найти песню (автор ➔ название)"}))
@router.message(Command("search"))
async def cmd_search_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(SearchFSM.waiting_for_artist)
    await message.answer(
        "Шаг 1 из 2: введите имя исполнителя.\n\n"
        "Если исполнителей два или больше, укажите их через запятую:\n"
        "<code>Исполнитель 1, Исполнитель 2</code>\n\n"
        "Можно сразу отправить исполнителя и название:\n"
        "<code>Исполнитель — Название</code>",
        reply_markup=get_cancel_reply_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "search:use_quick_artist")
async def cb_use_quick_artist(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    artist = data.get("quick_artist")
    if not artist:
        await callback.answer("Исполнитель не указан.", show_alert=True)
        return
    await state.update_data(artist=artist)
    await state.set_state(SearchFSM.waiting_for_title)
    await callback.answer()
    prompt_text = (
        f"Исполнитель: <b>{html.escape(artist)}</b>\n\n"
        "Шаг 2 из 2: введите название трека."
    )
    try:
        await callback.message.edit_text(prompt_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(prompt_text, parse_mode="HTML", reply_markup=get_cancel_reply_keyboard())


@router.message(SearchFSM.waiting_for_artist)
async def process_search_artist(message: Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        return
    artist_text = message.text.strip()
    if not artist_text:
        return

    # Если пользователь прислал ссылку
    url = find_first_url(artist_text)
    if url:
        await state.clear()
        clean_url, mods, _ = parse_url_and_modifiers(artist_text)
        variant = ", ".join(mods) if mods else "original"
        await _execute_download_and_send(message=message, raw_query=artist_text, url=clean_url or url, variant=variant)
        return

    # Если пользователь прислал сразу "Исполнитель — Название" или "Исполнитель 1, Исполнитель 2 -- Название"
    artists_list, parsed_title, raw_artist = parse_multi_artist_query(artist_text)
    if parsed_title and raw_artist:
        await state.clear()
        artist_words = set(re.findall(r'[\w]+', raw_artist.lower()))
        clean_title, mods = extract_track_modifiers(parsed_title, ignore_words=artist_words)
        variant = ", ".join(mods) if mods else "original"
        display_name = format_track_display(artists_list, clean_title)
        await message.answer(
            f"Ищу: <b>{html.escape(display_name)}</b>",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
        await _execute_download_and_send(
            message=message,
            raw_query=artist_text,
            custom_artist=raw_artist,
            custom_title=clean_title,
            variant=variant
        )
        return

    await state.update_data(artist=artist_text)
    await state.set_state(SearchFSM.waiting_for_title)
    await message.answer(
        f"Исполнитель: <b>{html.escape(artist_text)}</b>\n\n"
        "Шаг 2 из 2: введите название трека.",
        parse_mode="HTML",
        reply_markup=get_cancel_reply_keyboard()
    )


@router.message(SearchFSM.waiting_for_title)
async def process_search_title(message: Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        return
    title_text = message.text.strip()
    if not title_text:
        return

    # Если пользователь прислал ссылку
    url = find_first_url(title_text)
    if url:
        await state.clear()
        await message.answer("Ссылка принята.", reply_markup=get_main_reply_keyboard())
        clean_url, mods, _ = parse_url_and_modifiers(title_text)
        variant = ", ".join(mods) if mods else "original"
        await _execute_download_and_send(message=message, raw_query=title_text, url=clean_url or url, variant=variant)
        return

    data = await state.get_data()
    artist = (data.get("artist") or "").strip()
    await state.clear()

    artist_words = set(re.findall(r'[\w]+', artist.lower())) if artist else None
    clean_title, mods = extract_track_modifiers(title_text, ignore_words=artist_words)
    variant = ", ".join(mods) if mods else "original"

    display_name = format_track_display(artist, clean_title)
    await message.answer(
        f"Ищу: <b>{html.escape(display_name)}</b>",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard()
    )
    await _execute_download_and_send(
        message=message,
        raw_query=f"{artist} {title_text}".strip(),
        custom_artist=artist,
        custom_title=clean_title,
        variant=variant
    )


@router.message(F.text | F.caption)
async def handle_music_request(message: Message, state: FSMContext):
    user_text = (message.text or message.caption or "").strip()
    if not user_text or user_text.startswith("/"):
        return

    # 1. Ссылка (Spotify, Apple Music, YouTube, SoundCloud и др.) + модификаторы
    clean_url, url_mods, _ = parse_url_and_modifiers(user_text)
    if clean_url:
        variant = ", ".join(url_mods) if url_mods else "original"
        await _execute_download_and_send(message=message, raw_query=user_text, url=clean_url, variant=variant)
        return

    # 2. Разделение по тире: "Исполнитель — Название" или "Исполнитель 1, Исполнитель 2 -- Название"
    artists_list, parsed_title, raw_artists = parse_multi_artist_query(user_text)
    if parsed_title and raw_artists:
        artist_words = set(re.findall(r'[\w]+', raw_artists.lower()))
        clean_title, text_mods = extract_track_modifiers(parsed_title, ignore_words=artist_words)
        variant = ", ".join(text_mods) if text_mods else "original"
        await _execute_download_and_send(
            message=message,
            raw_query=user_text,
            custom_artist=raw_artists,
            custom_title=clean_title,
            variant=variant
        )
        return

    # 2b. Текст без дефиса: проверяем модификаторы
    clean_text, text_mods = extract_track_modifiers(user_text)
    variant = ", ".join(text_mods) if text_mods else "original"

    # 3. Текст без тире и не ссылка: запускаем пошаговый поиск
    await state.clear()
    await state.set_state(SearchFSM.waiting_for_artist)
    await state.update_data(quick_artist=clean_text)

    quick_markup = None
    if len(clean_text) <= 40:
        quick_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"Использовать «{clean_text}» как исполнителя", callback_data="search:use_quick_artist")],
                [InlineKeyboardButton(text="Отмена", callback_data="search:cancel")]
            ]
        )

    await message.answer(
        "Шаг 1 из 2: введите имя исполнителя.\n\n"
        "Если исполнителей два или больше, укажите их через запятую:\n"
        "<code>Исполнитель 1, Исполнитель 2</code>\n\n"
        "Можно сразу отправить:\n"
        "<code>Исполнитель — Название</code>",
        parse_mode="HTML",
        reply_markup=quick_markup or get_cancel_reply_keyboard()
    )


def format_download_error(e: Exception, track_info: Optional[ExtractedTrack] = None) -> str:
    """
    Преобразует внутренние ошибки и исключения yt-dlp/сети в понятные,
    лаконичные сообщения для пользователя без технических деталей и дампов.
    """
    err_str = str(e)
    if err_str.startswith("⚠️"):
        err_str = err_str.replace("⚠️ ", "").strip()
    if err_str.startswith("Возникла ошибка"):
        return err_str

    err_lower = err_str.lower()
    if "drm protected" in err_lower or "is drm protected" in err_lower:
        return "Возникла ошибка 1. Попробуйте другой источник или отправьте название трека текстом."
    if track_info and track_info.platform in ("Yandex Music", "VK Music") and (
        "не найден" in err_lower or "не подошел" in err_lower or "кандидат" in err_lower or "not found" in err_lower
    ):
        return "Возникла ошибка 2. Попробуйте ещё раз или отправьте название трека текстом."
    if any(k in err_lower for k in ("private video", "this video is private", "video is private")):
        return "Возникла ошибка 3. Попробуйте другой трек."
    if any(k in err_lower for k in ("confirm your age", "age-restricted", "age restricted", "content warning", "requires authentication")):
        return "Возникла ошибка 4. Попробуйте другой трек."
    if any(k in err_lower for k in ("not available in your country", "uploader has not made this video available", "geo-restricted", "georestricted", "blocked in your country")):
        return "Возникла ошибка 5. Попробуйте другой источник."
    if any(k in err_lower for k in ("confirm you're not a bot", "automated queries", "too many requests", "http error 429", "bot-check", "bot check")):
        return "Возникла ошибка 6. Попробуйте ещё раз через некоторое время."
    if any(k in err_lower for k in ("format is not available", "requested format", "no audio stream", "format not available")):
        return "Возникла ошибка 7. Попробуйте другой трек или отправьте название текстом."
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)) or any(k in err_lower for k in ("timed out", "timeout", "timedout", "connection reset", "network is unreachable", "temporary failure in name resolution")):
        return "Возникла ошибка 8. Попробуйте ещё раз."
    if any(k in err_lower for k in ("ffmpeg", "ffprobe", "conversion failed", "postprocessing")):
        return "Возникла ошибка 9. Попробуйте ещё раз."
    if any(k in err_lower for k in ("video unavailable", "this video is unavailable", "has been removed", "deleted", "does not exist")):
        return "Возникла ошибка 10. Попробуйте другой трек."

    return "Возникла ошибка 11. Попробуйте ещё раз."


async def _execute_download_and_send(
    message: Message,
    raw_query: str,
    url: Optional[str] = None,
    custom_artist: Optional[str] = None,
    custom_title: Optional[str] = None,
    variant: str = "original",
):
    req_id = uuid.uuid4().hex[:6]
    t_req_start = time.perf_counter()
    user_id = message.from_user.id if message.from_user else "unknown"
    print(
        f"[MUSIC][request_id={req_id}] handler START user={user_id} raw_query='{raw_query[:80]}' is_url={bool(url)} "
        f"artist='{custom_artist}' title='{custom_title}' variant='{variant}'",
        flush=True
    )

    if message.from_user:
        await log_user_activity_async(message.from_user.id, message.from_user.username, message.from_user.full_name)

    if url:
        cache_key = url
    elif custom_artist and custom_title:
        cache_key = f"{custom_artist} - {custom_title}".lower()
    else:
        cache_key = raw_query.lower()

    downloaded_audio = None
    t_cleanup = 0.0
    status_msg = None
    try:
        t_m0 = time.perf_counter()
        if url:
            status_msg = await message.reply("Проверяю ссылку...")
            print(f"[MUSIC][request_id={req_id}] resolve_track_url START url='{url}'", flush=True)
            track_info = await asyncio.wait_for(resolve_track_url(url), timeout=10.0)
            # Определение варианта для URL-входов:
            # Приоритет:
            # 1. Явные пользовательские модификаторы (url_mods / variant != "original")
            # 2. Канонический вариант из названия трека (Spotify, Apple Music и др.)
            # 3. "original"
            if variant == "original" and track_info and track_info.title:
                _, title_mods = extract_track_modifiers(track_info.title)
                if title_mods:
                    if "super slowed" in title_mods:
                        variant = "super slowed"
                    elif "super slow" in title_mods:
                        variant = "super slow"
                    elif "ultra slowed" in title_mods:
                        variant = "ultra slowed"
                    else:
                        variant = ", ".join(title_mods)
                    print(f"[MUSIC][request_id={req_id}] Variant detected from canonical title: variant='{variant}' (title='{track_info.title}')", flush=True)
        elif custom_artist and custom_title:
            disp_name = format_track_display(custom_artist, custom_title)
            status_msg = await message.reply(
                f"Ищу: <b>{html.escape(disp_name)}</b>",
                parse_mode="HTML"
            )
            combined_q = f"{custom_artist} {custom_title}"
            canonical = None
            if variant == "original" and not has_track_modifiers(combined_q):
                canonical = await resolve_canonical_track_info_async(combined_q)

            if canonical and variant == "original":
                eff_artist = canonical.artist or custom_artist
                eff_title = canonical.title or custom_title
                custom_artist = eff_artist
                custom_title = eff_title
                track_info = ExtractedTrack(
                    platform=canonical.platform,
                    target=f"ytsearch5:{eff_artist} {eff_title}",
                    is_search=True,
                    title=eff_title,
                    artist=eff_artist,
                    thumbnail_url=canonical.thumbnail_url,
                    duration=canonical.duration
                )
            else:
                target_q = f"{custom_artist} {custom_title}"
                if variant != "original":
                    target_q = f"{custom_artist} {custom_title} {variant}"
                track_info = ExtractedTrack(
                    platform="TextSearch",
                    target=f"ytsearch6:{target_q}" if variant != "original" else f"ytsearch5:{target_q}",
                    is_search=True,
                    title=custom_title,
                    artist=custom_artist,
                    thumbnail_url=None,
                    duration=None
                )
        else:
            status_msg = await message.reply(
                f"Ищу: <b>{html.escape(raw_query)}</b>",
                parse_mode="HTML"
            )
            print(f"[MUSIC][request_id={req_id}] resolve_text_to_track_info START query='{raw_query}'", flush=True)
            track_info = await asyncio.wait_for(resolve_text_to_track_info(raw_query), timeout=10.0)

        if not track_info:
            raise ValueError("Возникла ошибка 12. Проверьте название трека и попробуйте ещё раз.")

        t_metadata = time.perf_counter() - t_m0
        print(f"[MUSIC][request_id={req_id}] metadata SUCCESS in {t_metadata*1000:.1f}ms platform='{track_info.platform}' target='{track_info.target}' duration={track_info.duration}s", flush=True)

        source_key, source_type, source_id = build_source_key(url or track_info.target)

        # ⚡ Шаг 0: Проверка в двух-уровневом кэше L1 (RAM) / L2 (SQLite) с проверкой точности хронометража и изоляцией вариантов
        t_c0 = time.perf_counter()
        cached = await get_cached_track_async(cache_key, variant=variant)
        if not cached and track_info.artist and track_info.title:
            cached = await get_cached_track_async(f"{track_info.artist} - {track_info.title}".lower(), variant=variant)

        # Persistent Cache (Cloudflare D1 + L1 RAM) для YouTube-источников
        if source_type == "youtube" and variant == "original":
            cached_persistent = await get_persistent_track_async(source_key)
            if cached_persistent and is_valid_telegram_file_id(cached_persistent.telegram_file_id):
                is_match, cached_hash, current_hash = check_metadata_match(
                    cached_persistent,
                    artist=track_info.artist,
                    title=track_info.title,
                    album=getattr(track_info, "album", None),
                    duration=track_info.duration
                )
                if is_match:
                    cached = {
                        "file_id": cached_persistent.telegram_file_id,
                        "title": cached_persistent.title,
                        "artist": cached_persistent.artist,
                        "duration": cached_persistent.duration,
                        "variant": "original"
                    }
                    print(f"[MUSIC][request_id={req_id}] Persistent cache HIT: {source_key}", flush=True)
                else:
                    print(
                        f"[MUSIC][request_id={req_id}] Persistent cache metadata mismatch for {source_key}: "
                        f"cached_hash={cached_hash} current_hash={current_hash}. Cache MISS.",
                        flush=True
                    )
                    cached = None
        t_cache = time.perf_counter() - t_c0

        is_direct_media = bool(url and any(d in url.lower() for d in ("youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "bandcamp.com", "tiktok.com")))
        has_canonical_dur = bool(track_info.duration and track_info.duration > 35)
        is_apple_music = bool(track_info and track_info.platform in ("Apple Music", "Spotify", "Canonical/Deezer", "Canonical/iTunes", "Yandex Music", "VK Music"))
        is_text_input = bool(not url)

        if cached:
            cached_dur = cached.get("duration") or 0
            cached_title = cached.get("title") or ""
            cached_artist = cached.get("artist") or ""
            _, user_mods = extract_track_modifiers(raw_query)
            _, cached_mods = extract_track_modifiers(f"{cached_title} {cached_artist}")

            should_invalidate = False
            # 1. Для каталогов (Apple Music / Spotify / Deezer / Text): несовпадение хронометража более чем на 4 сек (только для оригинала!)
            if variant == "original" and not is_direct_media and has_canonical_dur and cached_dur > 0 and abs(cached_dur - track_info.duration) > 4:
                print(f"[MUSIC][request_id={req_id}] Catalog cache INVALIDATED: cached_duration={cached_dur}s != expected={track_info.duration}s. Purging.", flush=True)
                should_invalidate = True
            # 2. Если в кэше лежит ремикс/микс/драмка/slowed, а пользователь искал оригинал
            elif variant == "original" and cached_mods:
                print(f"[MUSIC][request_id={req_id}] Text search cache INVALIDATED: cached track '{cached_title}' has unwanted modifiers {cached_mods}. Purging.", flush=True)
                should_invalidate = True
            # 3. Если пользователь искал вариант, а в кэше трек без требуемого варианта
            elif variant != "original":
                target_mods = extract_modifiers(variant)
                cached_var = cached.get("variant")
                cached_var_mods = extract_modifiers(cached_var) if cached_var else set()
                combined_cached_mods = set(cached_mods) | cached_var_mods
                if not is_candidate_matching_modifiers(target_mods, combined_cached_mods):
                    print(f"[MUSIC][request_id={req_id}] Variant cache INVALIDATED: cached track '{cached_title}' does not match variant '{variant}'. Purging.", flush=True)
                    should_invalidate = True

            if should_invalidate:
                print(
                    f"[MUSIC][request_id={req_id}] Cache INVALIDATED. Purging stale cache entry.",
                    flush=True
                )
                await delete_cached_track_async(cache_key, variant=variant)
                if track_info.artist and track_info.title:
                    await delete_cached_track_async(f"{track_info.artist} - {track_info.title}".lower(), variant=variant)
                cached = None
            else:
                print(f"[MUSIC][request_id={req_id}] cache lookup HIT in {t_cache*1000:.2f}ms (duration={cached_dur}s)", flush=True)
                try:
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                    t_u0 = time.perf_counter()
                    owner_id = message.from_user.id if message.from_user else None
                    await message.answer_audio(
                        audio=cached["file_id"],
                        title=cached.get("title") or (track_info.title or custom_title or "Unknown Track"),
                        performer=cached.get("artist") or (track_info.artist or custom_artist or "Unknown Artist"),
                        duration=cached_dur,
                        reply_markup=get_audio_edit_keyboard(owner_id)
                    )
                    t_telegram = time.perf_counter() - t_u0
                    if message.from_user:
                        await increment_user_download_async(message.from_user.id)
                    t_total_cache = time.perf_counter() - t_req_start
                    print(
                        f"[PERF][request_id={req_id}] cache_lookup={t_cache:.3f}s telegram_upload={t_telegram:.3f}s TOTAL={t_total_cache:.3f}s (CACHE_HIT)",
                        flush=True
                    )
                    return
                except TelegramAPIError as cache_err:
                    print(f"[MUSIC][request_id={req_id}] cache send failed with TelegramAPIError ({cache_err}). Purging invalid file_id and re-downloading.", flush=True)
                    logger.warning("Кэшированный file_id устарел или недоступен: %s", cache_err)
                    if cached.get("file_id"):
                        await invalidate_cached_file_id_async(cached["file_id"])
                        if source_type == "youtube":
                            await invalidate_persistent_track_async(source_key, cached["file_id"])
                    cached = None
                except Exception as cache_err:
                    print(f"[MUSIC][request_id={req_id}] cache send failed: {cache_err}. Falling back to live download.", flush=True)
                    cached = None

        platform_label = f"\nПлатформа: <b>{track_info.platform}</b>" if track_info.platform and "Search" not in track_info.platform else ""
        if status_msg:
            display_title = format_track_display(track_info.artist, track_info.title)
            await status_msg.edit_text(
                f"Загрузка: <b>{html.escape(display_title)}</b>{platform_label}",
                parse_mode="HTML"
            )

        print(f"[MUSIC][request_id={req_id}] download_track START target='{track_info.target}' is_apple_music={is_apple_music} is_text_input={is_text_input}", flush=True)
        try:
            async with DOWNLOAD_SEMAPHORE:
                async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                    downloaded_audio = await download_track(
                        query_or_url=track_info.target,
                        custom_title=track_info.title,
                        custom_artist=track_info.artist,
                        thumbnail_url=track_info.thumbnail_url,
                        expected_duration=track_info.duration,
                        request_id=req_id,
                        is_apple_music=is_apple_music,
                        is_text_input=is_text_input,
                        requested_variant=variant,
                        custom_album=getattr(track_info, "album", None)
                    )
        except Exception as dl_err:
            is_direct_url = bool(url and any(d in url.lower() for d in ("youtube.com", "youtu.be", "soundcloud.com")))
            if is_direct_url or is_text_input or not url or (track_info and track_info.is_search):
                # Invariant: EXACT MEDIA FAILURE != SEARCH FAILURE
                # Если прямая ссылка или поисковый запрос уже завершились ошибкой, не делаем вторичный дублирующий поиск
                logger.error("Download failed for %s: %s", (url or (track_info.target if track_info else raw_query)), dl_err)
                raise dl_err

            fallback_query = None
            clean_artist = re.sub(r'[/\\_]+', ' ', track_info.artist or '').strip()
            clean_title = re.sub(r'[/\\_]+', ' ', track_info.title or '').strip()
            clean_title = re.sub(
                r'\s*[\(\[](?:Official\s*(?:Music\s*)?Video|Official\s*Audio|Lyric\s*Video|Video|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
                '',
                clean_title,
                flags=re.IGNORECASE
            ).strip()

            if clean_artist and clean_title and clean_artist.lower() not in clean_title.lower():
                fallback_query = f"{clean_artist} - {clean_title}"
            elif clean_title:
                fallback_query = clean_title
            elif clean_artist:
                fallback_query = clean_artist

            if fallback_query:
                print(f"[MUSIC][request_id={req_id}] Прямая ссылка не отдала аудио ({dl_err}), скачиваем через поиск: '{fallback_query}'", flush=True)
                async with DOWNLOAD_SEMAPHORE:
                    async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                        downloaded_audio = await download_track(
                            query_or_url=f"ytsearch5:{fallback_query}",
                            custom_title=track_info.title,
                            custom_artist=track_info.artist,
                            thumbnail_url=track_info.thumbnail_url,
                            expected_duration=track_info.duration,
                            request_id=f"{req_id}_fb",
                            is_apple_music=is_apple_music,
                            is_text_input=is_text_input,
                            requested_variant=variant,
                            custom_album=getattr(track_info, "album", None)
                        )
            else:
                raise dl_err

        print(f"[MUSIC][request_id={req_id}] download_track SUCCESS title='{downloaded_audio.title}' duration={downloaded_audio.duration}s size={downloaded_audio.filesize} bytes", flush=True)

        if downloaded_audio.filesize > MAX_FILE_SIZE_BYTES:
            await status_msg.edit_text("Возникла ошибка 13. Получившийся файл слишком большой для отправки.")
            return

        # Финальный барьер перед отправкой в Telegram: аутентичность и хронометраж студийного оригинала или канонической версии
        if not is_direct_media:
            if variant == "original" and has_canonical_dur:
                actual_final_dur = downloaded_audio.duration or 0
                final_diff = abs(actual_final_dur - track_info.duration)
                max_final_diff = max(4, min(7, int(track_info.duration * 0.02)))
                if final_diff > max_final_diff:
                    print(f"[MUSIC][request_id={req_id}] FINAL VALIDATION FAILED: final_diff={final_diff}s > {max_final_diff}s (got {actual_final_dur}s vs canonical {track_info.duration}s for студийного оригинала). Refusing to send to Telegram.", flush=True)
                    await status_msg.edit_text("Возникла ошибка 14. Точная версия трека не найдена.")
                    downloaded_audio.cleanup()
                    return
            elif variant != "original":
                target_mods = extract_modifiers(variant)
                # Финальная проверка подлинности найденного варианта:
                # В первую очередь используем source_modifiers и source_title из DownloadedAudio,
                # так как downloaded_audio.title может быть намеренно нормализован через custom_title.
                cand_mods = set(downloaded_audio.source_modifiers or set())
                if not cand_mods and downloaded_audio.source_title:
                    ignore_artist = set(re.findall(r'[\w]+', (downloaded_audio.artist or '').lower()))
                    cand_mods = extract_modifiers(downloaded_audio.source_title, ignore_words=ignore_artist)
                if not cand_mods:
                    file_text = f"{downloaded_audio.title} {downloaded_audio.artist}".lower()
                    ignore_artist = set(re.findall(r'[\w]+', (downloaded_audio.artist or '').lower()))
                    cand_mods = extract_modifiers(file_text, ignore_words=ignore_artist)

                if not is_candidate_matching_modifiers(target_mods, cand_mods):
                    print(f"[MUSIC][request_id={req_id}] FINAL VALIDATION FAILED: downloaded audio '{downloaded_audio.title}' (source: '{downloaded_audio.source_title}') lacks requested variant '{variant}' (got mods: {cand_mods}, target: {target_mods}). Refusing to send to Telegram.", flush=True)
                    await status_msg.edit_text("Возникла ошибка 15. Запрошенная версия трека не найдена.")
                    downloaded_audio.cleanup()
                    return

        await status_msg.edit_text("Отправляю трек...")

        audio_file = FSInputFile(downloaded_audio.file_path)
        thumb_path = downloaded_audio.thumbnail_path
        thumb_file = (
            FSInputFile(thumb_path)
            if (thumb_path and thumb_path.exists() and thumb_path.is_file() and thumb_path.stat().st_size > 0)
            else None
        )

        owner_id = message.from_user.id if message.from_user else None
        t_u0 = time.perf_counter()
        print(f"[MUSIC][request_id={req_id}] send_audio START", flush=True)
        log_memory_stage("before Telegram upload", req_id=req_id, source=source_type, file_path=downloaded_audio.file_path)
        try:
            sent_msg = await message.answer_audio(
                audio=audio_file,
                title=downloaded_audio.title,
                performer=downloaded_audio.artist,
                duration=downloaded_audio.duration,
                thumbnail=thumb_file,
                reply_markup=get_audio_edit_keyboard(owner_id)
            )
        except Exception as send_err:
            if thumb_file:
                print(f"[MUSIC][request_id={req_id}] send_audio with thumbnail failed ({send_err}), retrying without thumbnail...", flush=True)
                sent_msg = await message.answer_audio(
                    audio=audio_file,
                    title=downloaded_audio.title,
                    performer=downloaded_audio.artist,
                    duration=downloaded_audio.duration,
                    thumbnail=None,
                    reply_markup=get_audio_edit_keyboard(owner_id)
                )
            else:
                raise
        log_memory_stage("after Telegram upload", req_id=req_id, source=source_type, file_path=downloaded_audio.file_path)
        t_telegram = time.perf_counter() - t_u0
        print(f"[MUSIC][request_id={req_id}] send_audio SUCCESS in {t_telegram:.2f}s", flush=True)

        # Сохраняем в кэш для мгновенной отдачи будущим запросам с учетом варианта
        if sent_msg.audio and sent_msg.audio.file_id:
            await save_cached_track_async(
                query=cache_key,
                file_id=sent_msg.audio.file_id,
                title=downloaded_audio.title,
                artist=downloaded_audio.artist,
                duration=downloaded_audio.duration,
                variant=variant
            )
            if downloaded_audio.artist and downloaded_audio.title:
                await save_cached_track_async(
                    query=f"{downloaded_audio.artist} - {downloaded_audio.title}",
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration,
                    variant=variant
                )
            if source_type == "youtube" and variant == "original":
                asyncio.create_task(
                    save_persistent_track_async(
                        source_key=source_key,
                        source_type=source_type,
                        source_id=source_id,
                        artist=downloaded_audio.artist or track_info.artist or "Unknown Artist",
                        title=downloaded_audio.title or track_info.title or "Unknown Track",
                        album=downloaded_audio.album or getattr(track_info, "album", None),
                        duration=downloaded_audio.duration or track_info.duration or 0,
                        telegram_file_id=sent_msg.audio.file_id
                    )
                )
            print(f"[MUSIC][request_id={req_id}] cache save SUCCESS", flush=True)

        if message.from_user:
            await increment_user_download_async(message.from_user.id)

        t_total = time.perf_counter() - t_req_start
        perf = downloaded_audio.perf_timings or {}
        print(
            f"[PERF][request_id={req_id}] ===== PRODUCTION TIMING SUMMARY =====\n"
            f"[PERF][request_id={req_id}] cache_lookup={t_cache:.3f}s\n"
            f"[PERF][request_id={req_id}] metadata={t_metadata:.3f}s\n"
            f"[PERF][request_id={req_id}] search={perf.get('search', 0.0):.3f}s\n"
            f"[PERF][request_id={req_id}] candidate_selection={perf.get('candidate_selection', 0.0):.3f}s\n"
            f"[PERF][request_id={req_id}] download={perf.get('download', 0.0):.3f}s\n"
            f"[PERF][request_id={req_id}] ffmpeg={perf.get('ffmpeg', 0.0):.3f}s\n"
            f"[PERF][request_id={req_id}] metadata_write={perf.get('tags', 0.0):.3f}s\n"
            f"[PERF][request_id={req_id}] telegram_upload={t_telegram:.3f}s\n"
            f"[PERF][request_id={req_id}] TOTAL={t_total:.3f}s\n"
            f"[PERF][request_id={req_id}] ======================================",
            flush=True
        )
        print(f"[MUSIC][request_id={req_id}] handler END total={t_total:.2f}s", flush=True)

        try:
            await status_msg.delete()
        except Exception:
            pass

    except UnsupportedUrlError as e:
        logger.info("[MUSIC][request_id=%s] UnsupportedUrlError: %s", req_id, e)
        user_friendly = str(e)
        try:
            if status_msg:
                await status_msg.edit_text(user_friendly, parse_mode="HTML")
            else:
                await message.reply(user_friendly, parse_mode="HTML")
        except Exception:
            try:
                await message.reply(user_friendly, parse_mode="HTML")
            except Exception:
                pass

    except Exception as e:
        print(f"[MUSIC][request_id={req_id}] ERROR at processing: {e}\n{traceback.format_exc()}", flush=True)
        logger.exception("Ошибка при обработке запроса %s", url or raw_query)
        user_friendly = format_download_error(e, track_info)

        try:
            if status_msg:
                await status_msg.edit_text(user_friendly, parse_mode="HTML")
            else:
                await message.reply(user_friendly, parse_mode="HTML")
        except Exception:
            try:
                await message.reply(user_friendly, parse_mode="HTML")
            except Exception:
                pass
    finally:
        if downloaded_audio:
            t_cl0 = time.perf_counter()
            downloaded_audio.cleanup()
            t_cleanup = time.perf_counter() - t_cl0
            print(f"[MUSIC][request_id={req_id}] cleanup={t_cleanup:.4f}s", flush=True)
