import asyncio
import os
import shutil
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import sys
import yt_dlp

# Отключаем предупреждение yt-dlp об устаревании Python 3.10 в консоли
if 'yt_dlp.YoutubeDL' in sys.modules:
    sys.modules['yt_dlp.YoutubeDL']._get_system_deprecation = lambda: None

from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, ID3NoHeaderError

from config import DOWNLOADS_DIR, DEFAULT_AUDIO_BITRATE, MAX_FILE_SIZE_BYTES, BASE_DIR, get_cookies_info


@dataclass
class DownloadedAudio:
    file_path: Path
    title: str
    artist: str
    duration: int
    thumbnail_path: Optional[Path]
    filesize: int
    folder_path: Path

    def cleanup(self):
        """Удаляет временную папку загрузки и все файлы внутри."""
        try:
            if self.folder_path.exists():
                shutil.rmtree(self.folder_path, ignore_errors=True)
        except Exception:
            pass


def _convert_thumbnail_to_jpg(thumb_path: Path) -> Optional[Path]:
    """Конвертирует обложку в формат JPEG (требование Telegram) и сжимает при необходимости."""
    if not thumb_path or not thumb_path.exists():
        return None
    try:
        target_path = thumb_path.with_suffix(".jpg")
        with Image.open(thumb_path) as img:
            rgb_img = img.convert("RGB")
            rgb_img.thumbnail((640, 640))
            rgb_img.save(target_path, "JPEG", quality=85)
        return target_path
    except Exception:
        return thumb_path if thumb_path.suffix.lower() in [".jpg", ".jpeg"] else None


def _apply_custom_metadata(
    mp3_path: Path,
    title: Optional[str],
    artist: Optional[str],
    cover_path: Optional[Path] = None
):
    """Записывает точные ID3-теги названия, исполнителя и обложки."""
    try:
        try:
            audio = EasyID3(mp3_path)
        except ID3NoHeaderError:
            audio = EasyID3()
            audio.save(mp3_path)

        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        audio.save(mp3_path)

        # Вшиваем обложку в тег ID3 APIC
        if cover_path and cover_path.exists():
            id3 = ID3(mp3_path)
            with open(cover_path, "rb") as albumart:
                id3.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,  # 3 is for album front cover
                        desc="Cover",
                        data=albumart.read()
                    )
                )
            id3.save(v2_version=3)
    except Exception:
        pass


async def _download_remote_thumbnail(url: str, target_path: Path) -> Optional[Path]:
    """Скачивает обложку по URL, если yt-dlp её не предоставил."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    raw_thumb = target_path.with_suffix(".temp_img")
                    with open(raw_thumb, "wb") as f:
                        f.write(data)
                    jpg_thumb = _convert_thumbnail_to_jpg(raw_thumb)
                    if raw_thumb.exists() and raw_thumb != jpg_thumb:
                        raw_thumb.unlink(missing_ok=True)
                    return jpg_thumb
    except Exception:
        pass
    return None


def _sync_download(
    query_or_url: str,
    output_dir: Path,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE,
    skip_thumbnail: bool = False
) -> DownloadedAudio:
    """Синхронный процесс ускоренной загрузки и конвертации через yt-dlp."""
    outtmpl = str(output_dir / "%(title).100B.%(ext)s")

    cookies_info = get_cookies_info()

    ydl_opts = {
        # Приоритет отдаем m4a и opus аудиопотокам оптимального размера (быстрая загрузка без лишнего веса)
        "format": "ba[ext=m4a]/ba[ext=webm]/ba/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "writethumbnail": not skip_thumbnail,
        "quiet": True,
        "no_warnings": True,
        # Ускорение сети: 5 параллельных потоков загрузки фрагментов и увеличенный размер чанка
        "concurrent_fragment_downloads": 5,
        "buffersize": 64 * 1024,
        "http_chunk_size": 10485760,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            }
        ],
        # Максимальное ускорение кодирования MP3 на Render (compression_level 9 в 7-10 раз быстрее уровня 0)
        "postprocessor_args": {
            "FFmpegExtractAudio": [
                "-threads", "0",
                "-compression_level", "9",
                "-vn"
            ]
        },
    }

    # Клиенты YouTube:
    # Важно: мобильные клиенты (mweb, android, ios) исключают ошибку "The page needs to be reloaded",
    # которая блокирует настольный веб-клиент на серверах Render.
    if cookies_info["active"]:
        ydl_opts["cookiefile"] = cookies_info["path"]
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["mweb", "android", "ios", "web"],
            }
        }
        print(f"[DOWNLOADER] Быстрый режим с cookies: {cookies_info['path']}", flush=True)
    else:
        # Резервный режим без cookies
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"],
            }
        }
        print("[DOWNLOADER] Режим без cookies (мобильные клиенты)", flush=True)


    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query_or_url, download=True)
        if "entries" in info:
            if not info["entries"]:
                raise ValueError("Трек не найден по данному запросу.")
            info = info["entries"][0]

    mp3_files = list(output_dir.glob("*.mp3"))
    if not mp3_files:
        raise FileNotFoundError("Аудиофайл MP3 не был создан после обработки.")

    mp3_path = mp3_files[0]
    filesize = mp3_path.stat().st_size

    # Находим обложку
    thumb_candidates = list(output_dir.glob("*.webp")) + list(output_dir.glob("*.jpg")) + list(output_dir.glob("*.png"))
    thumbnail_path = None
    if thumb_candidates:
        thumbnail_path = _convert_thumbnail_to_jpg(thumb_candidates[0])

    extracted_title = custom_title or info.get("track") or info.get("title") or "Unknown Track"
    extracted_artist = custom_artist or info.get("artist") or info.get("uploader") or info.get("channel") or "Unknown Artist"
    duration = int(info.get("duration") or 0)

    _apply_custom_metadata(mp3_path, extracted_title, extracted_artist, thumbnail_path)

    return DownloadedAudio(
        file_path=mp3_path,
        title=extracted_title,
        artist=extracted_artist,
        duration=duration,
        thumbnail_path=thumbnail_path,
        filesize=filesize,
        folder_path=output_dir
    )


async def download_track(
    query_or_url: str,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE
) -> DownloadedAudio:
    """
    Асинхронная функция загрузки трека в MP3.
    """
    session_id = uuid.uuid4().hex
    output_dir = DOWNLOADS_DIR / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    try:
        audio = await asyncio.to_thread(
            _sync_download,
            query_or_url,
            output_dir,
            custom_title,
            custom_artist,
            bitrate,
            bool(thumbnail_url)
        )

        # Если yt-dlp не скачал обложку, но есть ссылка на неё (например, из Spotify/Apple Music)
        if not audio.thumbnail_path and thumbnail_url:
            downloaded_thumb = await _download_remote_thumbnail(thumbnail_url, output_dir / "cover")
            if downloaded_thumb:
                audio.thumbnail_path = downloaded_thumb
                # Повторно применим теги с новой обложкой
                _apply_custom_metadata(audio.file_path, audio.title, audio.artist, audio.thumbnail_path)

        elapsed = time.time() - t_start
        print(f"[DOWNLOADER] ✅ Трек успешно получен за {elapsed:.2f} сек: {audio.title}", flush=True)
        return audio
    except Exception as primary_error:
        elapsed = time.time() - t_start
        print(f"[DOWNLOADER] Первичная загрузка {query_or_url} ({elapsed:.2f}s) вернула ошибку: {primary_error}", flush=True)

        fallback_query = None
        if custom_artist and custom_title:
            fallback_query = f"{custom_artist} - {custom_title}"
        elif custom_title:
            fallback_query = custom_title
        else:
            path_parts = [p for p in urllib.parse.urlparse(query_or_url).path.split('/') if p and p not in ('sets', 'track', 'song')]
            if path_parts:
                fallback_query = " ".join(path_parts[-2:]).replace("-", " ").replace("_", " ")

        # 1. Fallback в SoundCloud: если YouTube выдал ошибку (The page needs to be reloaded / CAPTCHA / bot check)
        if fallback_query and not query_or_url.startswith("scsearch"):
            try:
                print(f"[DOWNLOADER] Попытка Fallback через SoundCloud: scsearch1:{fallback_query}", flush=True)
                audio = await asyncio.to_thread(
                    _sync_download,
                    f"scsearch1:{fallback_query}",
                    output_dir,
                    custom_title,
                    custom_artist,
                    bitrate,
                    bool(thumbnail_url)
                )
                if not audio.thumbnail_path and thumbnail_url:
                    downloaded_thumb = await _download_remote_thumbnail(thumbnail_url, output_dir / "cover")
                    if downloaded_thumb:
                        audio.thumbnail_path = downloaded_thumb
                        _apply_custom_metadata(audio.file_path, audio.title, audio.artist, audio.thumbnail_path)
                elapsed_fb = time.time() - t_start
                print(f"[DOWNLOADER] ✅ Трек получен через SoundCloud Fallback за {elapsed_fb:.2f} сек: {audio.title}", flush=True)
                return audio
            except Exception as sc_err:
                print(f"[DOWNLOADER] Fallback SoundCloud не удался: {sc_err}", flush=True)

        # 2. Fallback в YouTube Search (если изначально передавалась прямая ссылка на видео YouTube)
        if fallback_query and not query_or_url.startswith("ytsearch") and not query_or_url.startswith("scsearch"):
            try:
                print(f"[DOWNLOADER] Попытка Fallback через YouTube Search: ytsearch1:{fallback_query}", flush=True)
                audio = await asyncio.to_thread(
                    _sync_download,
                    f"ytsearch1:{fallback_query}",
                    output_dir,
                    custom_title,
                    custom_artist,
                    bitrate,
                    bool(thumbnail_url)
                )
                if not audio.thumbnail_path and thumbnail_url:
                    downloaded_thumb = await _download_remote_thumbnail(thumbnail_url, output_dir / "cover")
                    if downloaded_thumb:
                        audio.thumbnail_path = downloaded_thumb
                        _apply_custom_metadata(audio.file_path, audio.title, audio.artist, audio.thumbnail_path)
                elapsed_fb = time.time() - t_start
                print(f"[DOWNLOADER] ✅ Трек получен через YouTube Search Fallback за {elapsed_fb:.2f} сек: {audio.title}", flush=True)
                return audio
            except Exception as yt_err:
                print(f"[DOWNLOADER] Fallback YouTube Search не удался: {yt_err}", flush=True)

        shutil.rmtree(output_dir, ignore_errors=True)
        raise primary_error
