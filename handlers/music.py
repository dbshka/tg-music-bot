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
)
from services.downloader import download_track
from services.database import (
    log_user_activity_async,
    increment_user_download_async,
    get_cached_track_async,
    save_cached_track_async,
    delete_cached_track_async,
    invalidate_cached_file_id_async,
)
from handlers.tag_editor import get_audio_edit_keyboard

logger = logging.getLogger(__name__)

router = Router(name="music_router")

# Семафор: 3 одновременных задачи (измеренный peak RSS = 133.5 MB при лимите 512 MB, сокращает время очереди на 30% по сравнению с 2)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)


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
    if message.from_user:
        await log_user_activity_async(message.from_user.id, message.from_user.username, message.from_user.full_name)
    start_text = (
        "Отправьте ссылку на трек или напишите автора и название песни.\n\n"
        "Также вы можете отправить свой аудиофайл для редактирования тегов."
    )
    await message.answer(start_text, reply_markup=get_main_reply_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "Инструкция по использованию:\n\n"
        "1. Скачивание по ссылке\n"
        "Отправьте ссылку на трек из Spotify, Apple Music, YouTube или SoundCloud.\n\n"
        "2. Поиск по тексту\n"
        "Отправьте сообщение в формате: Автор — Название. Или используйте команду /search.\n\n"
        "3. Редактор тегов\n"
        "Нажмите кнопку редактирования под отправленным треком или пришлите свой аудиофайл в чат.\n\n"
        "Поддерживаемые платформы: Spotify, Apple Music, YouTube, SoundCloud.\n"
        "Другие сервисы (включая Яндекс Музыку и VK) не поддерживаются из-за региональных ограничений."
    )
    await message.answer(help_text, reply_markup=get_main_reply_keyboard())


@router.message(Command("version"))
async def cmd_version(message: Message):
    from config import BOT_VERSION
    await message.answer(f"Версия бота: <b>v{BOT_VERSION}</b>", parse_mode="HTML")


@router.message(Command("cancel"))
@router.message(F.text.in_({"Отмена", "❌ Отмена"}))
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("Поиск отменен.", reply_markup=get_main_reply_keyboard())
    else:
        await message.answer("Нет активного поиска.", reply_markup=get_main_reply_keyboard())


@router.callback_query(F.data == "search:cancel")
async def cb_search_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Поиск отменен")
    try:
        await callback.message.edit_text("Поиск отменен.")
    except Exception:
        pass


@router.message(F.text.in_({"Найти песню", "🔍 Найти песню (автор ➔ название)"}))
@router.message(Command("search"))
async def cmd_search_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(SearchFSM.waiting_for_artist)
    await message.answer(
        "Шаг 1 из 2: Введите имя исполнителя:\n\n"
        "Или отправьте сообщение в формате: Исполнитель — Название",
        reply_markup=get_cancel_reply_keyboard()
    )


@router.callback_query(F.data == "search:use_quick_artist")
async def cb_use_quick_artist(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    artist = data.get("quick_artist")
    if not artist:
        await callback.answer("Ошибка: автор не найден", show_alert=True)
        return
    await state.update_data(artist=artist)
    await state.set_state(SearchFSM.waiting_for_title)
    await callback.answer()
    prompt_text = (
        f"Исполнитель: <b>{html.escape(artist)}</b>\n\n"
        "Шаг 2 из 2: Введите название трека:"
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

    # Если пользователь случайно прислал ссылку
    url = find_first_url(artist_text)
    if url:
        await state.clear()
        clean_url, mods = parse_url_and_modifiers(artist_text)
        variant = ", ".join(mods) if mods else "original"
        await _execute_download_and_send(message=message, raw_query=artist_text, url=clean_url or url, variant=variant)
        return

    # Если пользователь прислал сразу "Автор — Название"
    dash_match = re.split(r'\s+[-—–]\s+', artist_text, maxsplit=1)
    if len(dash_match) == 2 and dash_match[0].strip() and dash_match[1].strip():
        await state.clear()
        clean_title, mods = extract_track_modifiers(dash_match[1].strip())
        variant = ", ".join(mods) if mods else "original"
        await message.answer(
            f"Ищу трек: <b>{html.escape(dash_match[0].strip())} — {html.escape(clean_title)}</b>...",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard()
        )
        await _execute_download_and_send(
            message=message,
            raw_query=artist_text,
            custom_artist=dash_match[0].strip(),
            custom_title=clean_title,
            variant=variant
        )
        return

    await state.update_data(artist=artist_text)
    await state.set_state(SearchFSM.waiting_for_title)
    await message.answer(
        f"Исполнитель: <b>{html.escape(artist_text)}</b>\n\n"
        "Шаг 2 из 2: Введите название трека:",
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

    # Если пользователь случайно прислал ссылку
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

    await message.answer(
        f"Ищу трек: <b>{html.escape(artist)} — {html.escape(clean_title)}</b>...",
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

    # 2. Разделение по тире: "Исполнитель — Название"
    # Сначала проверяем дефис/тире в исходном тексте, чтобы не ломать треки вроде "The Drums - Drums"
    dash_match = re.split(r'\s+[-—–]\s+', user_text, maxsplit=1)
    if len(dash_match) == 2 and dash_match[0].strip() and dash_match[1].strip():
        raw_artist = dash_match[0].strip()
        raw_title = dash_match[1].strip()
        artist_words = set(re.findall(r'[\w]+', raw_artist.lower()))
        clean_title, text_mods = extract_track_modifiers(raw_title, ignore_words=artist_words)
        variant = ", ".join(text_mods) if text_mods else "original"
        await _execute_download_and_send(
            message=message,
            raw_query=user_text,
            custom_artist=raw_artist,
            custom_title=clean_title,
            variant=variant
        )
        return

    # 2b. Текст без дефиса: проверяем модификаторы
    clean_text, text_mods = extract_track_modifiers(user_text)
    variant = ", ".join(text_mods) if text_mods else "original"

    # 3. Текст без тире и не ссылка: запускаем пошаговый поиск (автор -> название)
    await state.clear()
    await state.set_state(SearchFSM.waiting_for_artist)
    await state.update_data(quick_artist=clean_text)

    quick_markup = None
    if len(clean_text) <= 40:
        quick_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"Использовать «{clean_text}» как автора", callback_data="search:use_quick_artist")],
                [InlineKeyboardButton(text="Отмена", callback_data="search:cancel")]
            ]
        )

    await message.answer(
        "Поиск музыки\n\n"
        "Шаг 1 из 2: Введите имя исполнителя:\n\n"
        "Или отправьте сообщение в формате:\n"
        "Исполнитель — Название",
        parse_mode="HTML",
        reply_markup=quick_markup or get_cancel_reply_keyboard()
    )


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
            status_msg = await message.reply("Анализирую ссылку...")
            print(f"[MUSIC][request_id={req_id}] resolve_track_url START url='{url}'", flush=True)
            track_info = await resolve_track_url(url)
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
            status_msg = await message.reply(
                f"Ищу трек: <b>{html.escape(custom_artist)} — {html.escape(custom_title)}</b>...",
                parse_mode="HTML"
            )
            combined_q = f"{custom_artist} {custom_title}"
            canonical = None
            if not has_track_modifiers(combined_q):
                canonical = await resolve_canonical_track_info_async(combined_q)

            if canonical:
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
                track_info = ExtractedTrack(
                    platform="TextSearch",
                    target=f"ytsearch5:{custom_artist} {custom_title}",
                    is_search=True,
                    title=custom_title,
                    artist=custom_artist,
                    thumbnail_url=None,
                    duration=None
                )
        else:
            status_msg = await message.reply(
                f"Ищу трек: <b>{html.escape(raw_query)}</b>...",
                parse_mode="HTML"
            )
            print(f"[MUSIC][request_id={req_id}] resolve_text_to_track_info START query='{raw_query}'", flush=True)
            track_info = await resolve_text_to_track_info(raw_query)

        t_metadata = time.perf_counter() - t_m0
        print(f"[MUSIC][request_id={req_id}] metadata SUCCESS in {t_metadata*1000:.1f}ms platform='{track_info.platform}' target='{track_info.target}' duration={track_info.duration}s", flush=True)

        # ⚡ Шаг 0: Проверка в двух-уровневом кэше L1 (RAM) / L2 (SQLite) с проверкой точности хронометража и изоляцией вариантов
        t_c0 = time.perf_counter()
        cached = await get_cached_track_async(cache_key, variant=variant)
        if not cached and track_info.artist and track_info.title:
            cached = await get_cached_track_async(f"{track_info.artist} - {track_info.title}".lower(), variant=variant)
        t_cache = time.perf_counter() - t_c0

        is_direct_media = bool(url and any(d in url.lower() for d in ("youtube.com", "youtu.be", "music.youtube.com", "soundcloud.com", "bandcamp.com", "vk.com", "tiktok.com")))
        has_canonical_dur = bool(track_info.duration and track_info.duration > 35)
        is_apple_music = bool(track_info and track_info.platform in ("Apple Music", "Spotify", "Canonical/Deezer", "Canonical/iTunes"))
        is_text_input = bool(not url)

        if cached:
            cached_dur = cached.get("duration") or 0
            cached_title = cached.get("title") or ""
            cached_artist = cached.get("artist") or ""
            _, user_mods = extract_track_modifiers(raw_query)
            _, cached_mods = extract_track_modifiers(f"{cached_title} {cached_artist}")

            should_invalidate = False
            # 1. Для каталогов (Apple Music / Spotify / Deezer / Text): несовпадение хронометража более чем на 4 сек
            if not is_direct_media and has_canonical_dur and cached_dur > 0 and abs(cached_dur - track_info.duration) > 4:
                print(f"[MUSIC][request_id={req_id}] Catalog cache INVALIDATED: cached_duration={cached_dur}s != expected={track_info.duration}s. Purging.", flush=True)
                should_invalidate = True
            # 2. Если в кэше лежит ремикс/микс/драмка, а пользователь искал оригинал
            elif variant == "original" and cached_mods:
                print(f"[MUSIC][request_id={req_id}] Text search cache INVALIDATED: cached track '{cached_title}' has unwanted modifiers {cached_mods}. Purging.", flush=True)
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
                    cached = None
                except Exception as cache_err:
                    print(f"[MUSIC][request_id={req_id}] cache send failed: {cache_err}. Falling back to live download.", flush=True)
                    cached = None

        platform_label = f"\nПлатформа: <b>{track_info.platform}</b>" if track_info.platform and "Search" not in track_info.platform else ""
        if status_msg:
            await status_msg.edit_text(
                f"Скачиваю: <b>{html.escape(track_info.display_name)}</b>{platform_label}\n"
                f"Загрузка аудиопотока...",
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
                        is_text_input=is_text_input
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
                            is_text_input=is_text_input
                        )
            else:
                raise dl_err

        print(f"[MUSIC][request_id={req_id}] download_track SUCCESS title='{downloaded_audio.title}' duration={downloaded_audio.duration}s size={downloaded_audio.filesize} bytes", flush=True)

        if downloaded_audio.filesize > MAX_FILE_SIZE_BYTES:
            size_mb = downloaded_audio.filesize / (1024 * 1024)
            await status_msg.edit_text(
                f"Файл слишком большой ({size_mb:.1f} МБ).\n"
                f"Telegram разрешает ботам отправлять файлы размером до 50 МБ."
            )
            return

        # Финальный барьер перед отправкой в Telegram: аутентичность и хронометраж студийного оригинала или канонической версии
        if not is_direct_media and has_canonical_dur:
            _, query_mods = extract_track_modifiers(raw_query)
            has_user_dsp_mods = bool(query_mods and any(m in query_mods for m in ["speed_multiplier", "8d", "nightcore", "bass boost"]))
            if not has_user_dsp_mods:
                actual_final_dur = downloaded_audio.duration or 0
                final_diff = abs(actual_final_dur - track_info.duration)
                max_final_diff = max(4, min(7, int(track_info.duration * 0.02)))
                if final_diff > max_final_diff:
                    ver_desc = f"версии «{variant}»" if variant != "original" else "студийного оригинала"
                    print(f"[MUSIC][request_id={req_id}] FINAL VALIDATION FAILED: final_diff={final_diff}s > {max_final_diff}s (got {actual_final_dur}s vs canonical {track_info.duration}s for {ver_desc}). Refusing to send to Telegram.", flush=True)
                    await status_msg.edit_text(
                        f"⚠️ К сожалению, найденный аудиофайл не прошёл финальную проверку подлинности {ver_desc} (отклонение хронометража более {max_final_diff} сек). Попробуйте уточнить запрос."
                    )
                    downloaded_audio.cleanup()
                    return

        await status_msg.edit_text("Отправка трека в Telegram...")

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

    except Exception as e:
        print(f"[MUSIC][request_id={req_id}] ERROR at processing: {e}\n{traceback.format_exc()}", flush=True)
        logger.exception("Ошибка при обработке запроса %s", url or raw_query)
        err_str = str(e)
        if "Яндекс Музык" in err_str:
            user_friendly = (
                "<b>Прямые ссылки Яндекс Музыки не поддерживаются.</b>\n\n"
                "Из-за региональных ограничений хостинга загрузка по прямым ссылкам недоступна.\n\n"
                "Отправьте автора и название трека текстом."
            )
        elif "ВК Музык" in err_str or "vk.com" in err_str:
            user_friendly = (
                "<b>Прямые ссылки ВКонтакте не поддерживаются.</b>\n\n"
                "Отправьте автора и название трека текстом."
            )
        elif "drm protected" in err_str.lower() or "is drm protected" in err_str.lower():
            user_friendly = (
                "<b>Этот трек защищен DRM (SoundCloud Go+).</b>\n\n"
                "Попробуйте отправить ссылку из Spotify, Apple Music или YouTube."
            )
        else:
            user_friendly = f"<b>Не удалось скачать трек.</b>\n<i>Причина: {html.escape(err_str[:250])}</i>"
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
