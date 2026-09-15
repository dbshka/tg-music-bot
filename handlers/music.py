import asyncio
import html
import logging
import os
import re
import time
import traceback
import uuid
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, CallbackQuery
from aiogram.utils.chat_action import ChatActionSender

from config import MAX_FILE_SIZE_BYTES
from services.extractor import find_first_url, resolve_track_url, resolve_text_to_track_info
from services.downloader import download_track
from services.database import (
    log_user_activity_async,
    increment_user_download_async,
    get_cached_track_async,
    save_cached_track_async
)
from handlers.tag_editor import get_audio_edit_keyboard

logger = logging.getLogger(__name__)

router = Router(name="music_router")

# Семафор: 3 одновременных задачи (измеренный peak RSS = 133.5 MB при лимите 512 MB, сокращает время очереди на 30% по сравнению с 2)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.from_user:
        await log_user_activity_async(message.from_user.id, message.from_user.username, message.from_user.full_name)
    text = (
        "👋 <b>Привет! Я помогу скачать музыку и настроить её под себя.</b>\n\n"
        "🎵 <b>Скачать трек</b>\n"
        "Отправь мне <b>ссылку на песню</b> из YouTube, Spotify, Apple Music, SoundCloud, VK и других платформ.\n\n"
        "Или просто напиши <b>название трека или исполнителя</b>:\n"
        "<code>The Weeknd — Blinding Lights</code>\n\n"
        "✏️ <b>Изменить теги</b>\n"
        "Под каждым скачанным треком есть кнопка <b>[ ✏️ Изменить теги ]</b>.\n"
        "Можно изменить:\n"
        "• название\n"
        "• исполнителя\n"
        "• альбом\n"
        "• обложку\n\n"
        "📂 <b>Обработать свой MP3</b>\n"
        "Отправь мне любой <b>MP3-файл</b>, и я помогу изменить его теги и обложку.\n\n"
        "🎧 <b>Отправь ссылку или название песни — и я начну.</b>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>Как пользоваться ботом:</b>\n\n"
        "1. <b>Скачивание по ссылке или тексту:</b>\n"
        "   Отправь ссылку (YouTube, Spotify, Apple Music, SoundCloud и др.) или напиши название трека.\n\n"
        "2. <b>Редактирование тегов и обложки:</b>\n"
        "   • Нажми <b>[ ✏️ Изменить теги ]</b> под любым отправленным ботом треком.\n"
        "   • Либо просто пришли боту свой <code>.mp3</code> файл из памяти телефона или компьютера.\n"
        "   • В кнопочном меню можно поменять: Название, Исполнителя, Альбом и загрузить фото обложки.\n\n"
        "⚠️ <i>Telegram разрешает отправку файлов размером до 50 МБ.</i>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("version"))
async def cmd_version(message: Message):
    from config import BOT_VERSION
    await message.answer(f"🤖 Версия бота: <b>v{BOT_VERSION}</b>", parse_mode="HTML")



@router.message(F.text)
async def handle_music_request(message: Message):
    if not message.text:
        return
    user_text = message.text.strip()
    if not user_text:
        return
    # Пропускаем команды бота
    if user_text.startswith("/"):
        return

    req_id = uuid.uuid4().hex[:6]
    t_req_start = time.perf_counter()
    user_id = message.from_user.id if message.from_user else "unknown"
    print(f"[MUSIC][request_id={req_id}] handler START user={user_id} text='{user_text[:80]}'", flush=True)

    if message.from_user:
        await log_user_activity_async(message.from_user.id, message.from_user.username, message.from_user.full_name)

    url = find_first_url(user_text)
    cache_key = url if url else user_text
    print(f"[MUSIC][request_id={req_id}] query='{cache_key}' is_url={bool(url)}", flush=True)

    # ⚡ Шаг 0: Мгновенная отдача из двух-уровневого кэша L1 (RAM) / L2 (SQLite)
    t_c0 = time.perf_counter()
    cached = await get_cached_track_async(cache_key)
    t_cache = time.perf_counter() - t_c0

    if cached:
        print(f"[MUSIC][request_id={req_id}] cache lookup HIT in {t_cache*1000:.2f}ms", flush=True)
        try:
            t_u0 = time.perf_counter()
            await message.answer_audio(
                audio=cached["file_id"],
                title=cached.get("title") or "Unknown Track",
                performer=cached.get("artist") or "Unknown Artist",
                duration=cached.get("duration") or 0,
                reply_markup=get_audio_edit_keyboard()
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
        except Exception as cache_err:
            print(f"[MUSIC][request_id={req_id}] cache send failed, falling back to live download: {cache_err}", flush=True)
            logger.warning("Кэшированный file_id устарел или недоступен, выполняем загрузку: %s", cache_err)
    else:
        print(f"[MUSIC][request_id={req_id}] cache lookup MISS in {t_cache*1000:.2f}ms", flush=True)

    # Распознавание трека: по ссылке либо по текстовому запросу как по виртуальной ссылке
    downloaded_audio = None
    t_cleanup = 0.0
    try:
        t_m0 = time.perf_counter()
        if url:
            status_msg = await message.reply("🔎 <i>Анализирую ссылку...</i>", parse_mode="HTML")
            print(f"[MUSIC][request_id={req_id}] resolve_track_url START url='{url}'", flush=True)
            track_info = await resolve_track_url(url)
        else:
            status_msg = await message.reply(f"🔎 <i>Ищу трек:</i> <b>{html.escape(user_text)}</b>...", parse_mode="HTML")
            print(f"[MUSIC][request_id={req_id}] resolve_text_to_track_info START query='{user_text}'", flush=True)
            track_info = await resolve_text_to_track_info(user_text)

        t_metadata = time.perf_counter() - t_m0
        print(f"[MUSIC][request_id={req_id}] metadata SUCCESS in {t_metadata*1000:.1f}ms platform='{track_info.platform}' target='{track_info.target}'", flush=True)

        platform_label = f"\nПлатформа: <b>{track_info.platform}</b>" if track_info.platform and "Search" not in track_info.platform else ""
        await status_msg.edit_text(
            f"⏳ Скачиваю: <b>{html.escape(track_info.display_name)}</b>{platform_label}\n"
            f"<i>Загрузка аудиопотока...</i>",
            parse_mode="HTML"
        )

        print(f"[MUSIC][request_id={req_id}] download_track START target='{track_info.target}'", flush=True)
            try:
                async with DOWNLOAD_SEMAPHORE:
                    async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                        downloaded_audio = await download_track(
                            query_or_url=track_info.target,
                            custom_title=track_info.title,
                            custom_artist=track_info.artist,
                            thumbnail_url=track_info.thumbnail_url,
                            expected_duration=track_info.duration,
                            request_id=req_id
                        )
            except Exception as dl_err:
                # Если прямая ссылка недоступна (SoundCloud Go+, bot-check YouTube и т.д.),
                # автоматически скачиваем трек через всесторонний поиск!
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
                                request_id=f"{req_id}_fb"
                            )
                else:
                    raise dl_err
            print(f"[MUSIC][request_id={req_id}] download_track SUCCESS title='{downloaded_audio.title}' duration={downloaded_audio.duration}s size={downloaded_audio.filesize} bytes", flush=True)

            file_p = Path(downloaded_audio.file_path)
            p_exists = file_p.exists()
            p_size = file_p.stat().st_size if p_exists else 0
            p_readable = os.access(file_p, os.R_OK) if p_exists else False

            if downloaded_audio.filesize > MAX_FILE_SIZE_BYTES:
                size_mb = downloaded_audio.filesize / (1024 * 1024)
                await status_msg.edit_text(
                    f"❌ <b>Файл слишком большой ({size_mb:.1f} МБ)</b>.\n"
                    f"Telegram разрешает ботам отправлять файлы размером до 50 МБ.",
                    parse_mode="HTML"
                )
                return

            await status_msg.edit_text("📤 <i>Отправка трека в Telegram...</i>", parse_mode="HTML")

            audio_file = FSInputFile(downloaded_audio.file_path)
            thumb_path = downloaded_audio.thumbnail_path
            thumb_file = (
                FSInputFile(thumb_path)
                if (thumb_path and thumb_path.exists() and thumb_path.is_file() and thumb_path.stat().st_size > 0)
                else None
            )

            t_u0 = time.perf_counter()
            print(f"[MUSIC][request_id={req_id}] send_audio START", flush=True)
            try:
                sent_msg = await message.answer_audio(
                    audio=audio_file,
                    title=downloaded_audio.title,
                    performer=downloaded_audio.artist,
                    duration=downloaded_audio.duration,
                    thumbnail=thumb_file,
                    reply_markup=get_audio_edit_keyboard()
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
                        reply_markup=get_audio_edit_keyboard()
                    )
                else:
                    raise
            t_telegram = time.perf_counter() - t_u0
            print(f"[MUSIC][request_id={req_id}] send_audio SUCCESS in {t_telegram:.2f}s", flush=True)

            # Сохраняем в кэш для мгновенной отдачи будущим запросам
            if sent_msg.audio and sent_msg.audio.file_id:
                await save_cached_track_async(
                    query=cache_key,
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )
                await save_cached_track_async(
                    query=f"{downloaded_audio.artist} - {downloaded_audio.title}",
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
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
            print(f"[MUSIC][request_id={req_id}] ERROR at link processing: {e}\n{traceback.format_exc()}", flush=True)
            logger.exception("Ошибка при обработке запроса %s", url or user_text)
            err_str = str(e)
            if "Яндекс Музык" in err_str:
                user_friendly = (
                    "⚠️ <b>Прямые ссылки Яндекс Музыки отключены.</b>\n\n"
                    "Из-за региональных ограничений хостинга загрузка по прямым ссылкам недоступна.\n\n"
                    "💡 <b>Решение:</b> отправьте название трека или исполнителя текстом (например: <code>Gazan — 67</code>). Бот моментально найдёт и скачает трек!"
                )
            elif "ВК Музык" in err_str or "vk.com" in err_str:
                user_friendly = (
                    "⚠️ <b>Прямые ссылки ВК Музыки не поддерживаются.</b>\n\n"
                    "ВКонтакте полностью закрыл аудиозаписи для внешних серверов без авторизации.\n\n"
                    "💡 <b>Решение:</b> отправьте название трека текстом (например: <code>MiyaGi — Captain</code>). Бот моментально найдёт и скачает трек!"
                )
            elif "drm protected" in err_str.lower() or "is drm protected" in err_str.lower():
                user_friendly = (
                    "⚠️ <b>Этот трек защищен DRM (SoundCloud Go+ / платная подписка).</b>\n\n"
                    "💡 <b>Решение:</b> попробуйте отправить прямую ссылку на трек из YouTube Music или ссылку из Spotify / Apple Music."
                )
            else:
                user_friendly = f"❌ <b>Не удалось скачать трек.</b>\n<i>Причина: {html.escape(err_str[:250])}</i>"
            try:
                await status_msg.edit_text(user_friendly, parse_mode="HTML")
            except Exception:
                pass
        finally:
            if downloaded_audio:
                t_cl0 = time.perf_counter()
                downloaded_audio.cleanup()
                t_cleanup = time.perf_counter() - t_cl0
                print(f"[MUSIC][request_id={req_id}] cleanup={t_cleanup:.4f}s", flush=True)
