import asyncio
import html
import logging
import time
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
from aiogram.utils.chat_action import ChatActionSender

from config import MAX_FILE_SIZE_BYTES
from services.extractor import find_first_url, resolve_track_url
from services.downloader import download_track
from services.database import (
    log_user_activity,
    increment_user_download,
    get_cached_track,
    save_cached_track
)
from handlers.tag_editor import get_audio_edit_keyboard

logger = logging.getLogger(__name__)

router = Router(name="music_router")

# Семафор: не более 5 одновременных задач
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(5)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.from_user:
        log_user_activity(message.from_user.id, message.from_user.username, message.from_user.full_name)
    text = (
        "👋 <b>Привет! Я помогу скачать музыку и настроить её под себя.</b>\n\n"
        "🎵 <b>Скачать трек</b>\n"
        "Отправь мне <b>ссылку на песню</b> из YouTube, Spotify, Яндекс Музыки, Apple Music, SoundCloud, VK и других платформ.\n\n"
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
        "   Отправь ссылку (YouTube, Spotify, Яндекс Музыка, Apple Music, SoundCloud и др.) или напиши название трека.\n\n"
        "2. <b>Редактирование тегов и обложки:</b>\n"
        "   • Нажми <b>[ ✏️ Изменить теги ]</b> под любым отправленным ботом треком.\n"
        "   • Либо просто пришли боту свой <code>.mp3</code> файл из памяти телефона или компьютера.\n"
        "   • В кнопочном меню можно поменять: Название, Исполнителя, Альбом и загрузить фото обложки.\n\n"
        "⚠️ <i>Telegram разрешает отправку файлов размером до 50 МБ.</i>"
    )
    await message.answer(text, parse_mode="HTML")



@router.message(F.text)
async def handle_music_request(message: Message):
    if message.from_user:
        log_user_activity(message.from_user.id, message.from_user.username, message.from_user.full_name)

    t_req_start = time.perf_counter()
    print(f"[PERF] request received for: {user_text[:80]}", flush=True)

    url = find_first_url(user_text)
    cache_key = url if url else user_text

    # ⚡ Шаг 0: Мгновенная отдача из Telegram-кэша (0.2–0.4 сек, без повторной загрузки и кодирования)
    t_c0 = time.perf_counter()
    cached = get_cached_track(cache_key)
    t_cache = time.perf_counter() - t_c0
    print(f"[PERF] cache={t_cache*1000:.1f}ms", flush=True)

    if cached:
        try:
            await message.answer_audio(
                audio=cached["file_id"],
                title=cached.get("title") or "Unknown Track",
                performer=cached.get("artist") or "Unknown Artist",
                duration=cached.get("duration") or 0,
                reply_markup=get_audio_edit_keyboard()
            )
            if message.from_user:
                increment_user_download(message.from_user.id)
            print(f"[CACHE] ⚡ Мгновенная отдача из кэша для: {cache_key} (всего {time.perf_counter() - t_req_start:.2f}s)", flush=True)
            return
        except Exception as cache_err:
            logger.warning("Кэшированный file_id устарел или недоступен, выполняем загрузку: %s", cache_err)

    # 1. Если передана ссылка
    if url:
        status_msg = await message.reply("🔎 <i>Анализирую ссылку...</i>", parse_mode="HTML")
        downloaded_audio = None
        try:
            t_m0 = time.perf_counter()
            track_info = await resolve_track_url(url)
            t_metadata = time.perf_counter() - t_m0
            print(f"[PERF] metadata={t_metadata*1000:.1f}ms", flush=True)
            
            await status_msg.edit_text(
                f"⏳ Скачиваю: <b>{html.escape(track_info.display_name)}</b>\n"
                f"Платформа: <b>{track_info.platform}</b>\n"
                f"<i>Конвертация в MP3...</i>",
                parse_mode="HTML"
            )

            async with DOWNLOAD_SEMAPHORE:
                async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                    downloaded_audio = await download_track(
                        query_or_url=track_info.target,
                        custom_title=track_info.title,
                        custom_artist=track_info.artist,
                        thumbnail_url=track_info.thumbnail_url,
                        expected_duration=track_info.duration
                    )

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
            thumb_file = FSInputFile(downloaded_audio.thumbnail_path) if downloaded_audio.thumbnail_path else None

            t_u0 = time.perf_counter()
            sent_msg = await message.answer_audio(
                audio=audio_file,
                title=downloaded_audio.title,
                performer=downloaded_audio.artist,
                duration=downloaded_audio.duration,
                thumbnail=thumb_file,
                reply_markup=get_audio_edit_keyboard()
            )
            t_telegram = time.perf_counter() - t_u0
            print(f"[PERF] telegram={t_telegram:.2f}s", flush=True)

            # Сохраняем в кэш для мгновенной отдачи будущим запросам
            if sent_msg.audio and sent_msg.audio.file_id:
                save_cached_track(
                    query=cache_key,
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )
                save_cached_track(
                    query=f"{downloaded_audio.artist} - {downloaded_audio.title}",
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )

            if message.from_user:
                increment_user_download(message.from_user.id)

            t_total = time.perf_counter() - t_req_start
            perf = downloaded_audio.perf_timings or {}
            print(
                f"[PERF] ===== PRODUCTION TIMING SUMMARY =====\n"
                f"[PERF] metadata={t_metadata*1000:.1f}ms\n"
                f"[PERF] cache={t_cache*1000:.1f}ms\n"
                f"[PERF] search={perf.get('search', 0.0):.2f}s\n"
                f"[PERF] candidate selection={perf.get('candidate_selection', 0.0):.3f}s\n"
                f"[PERF] download={perf.get('download', 0.0):.2f}s\n"
                f"[PERF] ffmpeg={perf.get('ffmpeg', 0.0):.2f}s\n"
                f"[PERF] tags={perf.get('tags', 0.0):.2f}s\n"
                f"[PERF] telegram={t_telegram:.2f}s\n"
                f"[PERF] total={t_total:.2f}s\n"
                f"[PERF] ======================================",
                flush=True
            )

            await status_msg.delete()

        except Exception as e:
            logger.exception("Ошибка при обработке ссылки %s", url)
            err_str = str(e)
            if "Sign in to confirm you’re not a bot" in err_str or "Sign in to confirm" in err_str:
                user_friendly = (
                    "❌ <b>YouTube временно ограничил прямое скачивание по этой ссылке.</b>\n\n"
                    "💡 <b>Решение:</b> просто отправьте название этой песни текстом — "
                    "бот мгновенно найдет и пришлет её!"
                )
            else:
                user_friendly = f"❌ <b>Не удалось скачать трек.</b>\n<i>Причина: {html.escape(err_str[:250])}</i>"
            await status_msg.edit_text(user_friendly, parse_mode="HTML")
        finally:
            if downloaded_audio:
                downloaded_audio.cleanup()

    # 2. Если передан обычный текст (поисковой запрос)
    else:
        status_msg = await message.reply(
            f"🔎 <i>Ищу трек:</i> <b>{html.escape(user_text)}</b>...",
            parse_mode="HTML"
        )
        downloaded_audio = None
        try:
            async with DOWNLOAD_SEMAPHORE:
                async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                    # YouTube Music search в 10 раз быстрее (2-3 сек вместо 15-30 сек на SoundCloud)
                    # Фильтрация кандидатов автоматически отсекает 30-секундные шортсы и превью
                    downloaded_audio = await download_track(
                        query_or_url=f"ytsearch3:{user_text}"
                    )

            if downloaded_audio.filesize > MAX_FILE_SIZE_BYTES:
                size_mb = downloaded_audio.filesize / (1024 * 1024)
                await status_msg.edit_text(
                    f"❌ <b>Файл слишком большой ({size_mb:.1f} МБ)</b>.",
                    parse_mode="HTML"
                )
                return

            await status_msg.edit_text("📤 <i>Отправка трека в Telegram...</i>", parse_mode="HTML")

            audio_file = FSInputFile(downloaded_audio.file_path)
            thumb_file = FSInputFile(downloaded_audio.thumbnail_path) if downloaded_audio.thumbnail_path else None

            t_u0 = time.perf_counter()
            sent_msg = await message.answer_audio(
                audio=audio_file,
                title=downloaded_audio.title,
                performer=downloaded_audio.artist,
                duration=downloaded_audio.duration,
                thumbnail=thumb_file,
                reply_markup=get_audio_edit_keyboard()
            )
            t_telegram = time.perf_counter() - t_u0
            print(f"[PERF] telegram={t_telegram:.2f}s", flush=True)

            if sent_msg.audio and sent_msg.audio.file_id:
                save_cached_track(
                    query=cache_key,
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )
                save_cached_track(
                    query=f"{downloaded_audio.artist} - {downloaded_audio.title}",
                    file_id=sent_msg.audio.file_id,
                    title=downloaded_audio.title,
                    artist=downloaded_audio.artist,
                    duration=downloaded_audio.duration
                )

            if message.from_user:
                increment_user_download(message.from_user.id)

            t_total = time.perf_counter() - t_req_start
            perf = downloaded_audio.perf_timings or {}
            print(
                f"[PERF] ===== PRODUCTION TIMING SUMMARY =====\n"
                f"[PERF] metadata=0.0ms\n"
                f"[PERF] cache={t_cache*1000:.1f}ms\n"
                f"[PERF] search={perf.get('search', 0.0):.2f}s\n"
                f"[PERF] candidate selection={perf.get('candidate_selection', 0.0):.3f}s\n"
                f"[PERF] download={perf.get('download', 0.0):.2f}s\n"
                f"[PERF] ffmpeg={perf.get('ffmpeg', 0.0):.2f}s\n"
                f"[PERF] tags={perf.get('tags', 0.0):.2f}s\n"
                f"[PERF] telegram={t_telegram:.2f}s\n"
                f"[PERF] total={t_total:.2f}s\n"
                f"[PERF] ======================================",
                flush=True
            )

            await status_msg.delete()

        except Exception as e:
            logger.exception("Ошибка при поиске трека %s", user_text)
            err_str = str(e)
            user_friendly = f"❌ <b>Трек не найден или произошла ошибка:</b>\n<i>{html.escape(err_str[:250])}</i>"
            await status_msg.edit_text(user_friendly, parse_mode="HTML")
        finally:
            if downloaded_audio:
                downloaded_audio.cleanup()
