import asyncio
import os
import re
import shutil
import threading
import time
import unicodedata
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
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

import config
from config import DOWNLOADS_DIR, DEFAULT_AUDIO_BITRATE, MAX_FILE_SIZE_BYTES, BASE_DIR, get_cookies_info, get_sanitized_proxy_info

# Модульная переменная для обратной совместимости с моками в тестах (unittest.mock.patch)
YOUTUBE_PROXY = None


def get_current_youtube_proxy() -> Optional[str]:
    """
    Возвращает актуальный YouTube прокси.
    Гарантирует, что downloader всегда получает актуальное значение прокси,
    установленное dynamic-стартом VLESS (config.YOUTUBE_PROXY),
    а также корректно работает при мокировании в тестах (patch("services.downloader.YOUTUBE_PROXY", ...)).
    """
    cfg_proxy = getattr(config, "YOUTUBE_PROXY", None)
    if cfg_proxy:
        return cfg_proxy
    return globals().get("YOUTUBE_PROXY")
from services.http_client import get_shared_session
from services.identity import (
    clean_unicode_text,
    has_track_modifiers,
    extract_modifiers,
    extract_core_title_words,
    compute_title_match_ratio,
    validate_artist_match,
    parse_speed_multiplier,
    TRACK_MODIFIERS,
    PERFORMANCE_MODIFIERS,
    DSP_SUPPORTED_MODIFIERS,
    SEMANTIC_MODIFIERS
)


@dataclass
class DownloadedAudio:
    file_path: Path
    title: str
    artist: str
    duration: int
    thumbnail_path: Optional[Path]
    filesize: int
    folder_path: Path
    perf_timings: Optional[dict] = None
    invocations: Optional[list] = None

    def cleanup(self):
        """Удаляет временную папку загрузки и все файлы внутри."""
        try:
            if self.folder_path and self.folder_path.exists():
                resolved_folder = self.folder_path.resolve()
                resolved_downloads = DOWNLOADS_DIR.resolve()
                if resolved_folder != resolved_downloads and resolved_downloads in resolved_folder.parents:
                    shutil.rmtree(resolved_folder, ignore_errors=True)
        except Exception:
            pass


def extract_speed_multiplier_ratio(text: str) -> Optional[float]:
    """Извлекает коэффициент скорости через parse_speed_multiplier (диапазон 0.5x .. 2.0x)."""
    return parse_speed_multiplier(text)


def _apply_audio_modifier_if_needed(audio_path: Path, requested_modifiers: set, cand_modifiers: set, req_tag: str = "", req_query: str = "") -> int:
    """
    Если пользователь явно запросил slowed/sped up/speed multiplier, а скачанный трек является
    оригинальной версией без модификаторов, программно применяем эффект через FFmpeg.
    """
    if not requested_modifiers:
        return 0

    non_dsp_requested = requested_modifiers - {"slowed", "slow", "super slowed", "super slow", "ultra slowed", "sped up", "spedup", "speed up", "speedup", "fast version", "speed_multiplier"}
    if non_dsp_requested and not (non_dsp_requested & cand_modifiers):
        raise ValueError(
            f"Версия с запрошенной модификацией ({', '.join(sorted(non_dsp_requested))}) не найдена. "
            f"Оригинальный трек отклонён во избежание подмены."
        )

    if requested_modifiers & cand_modifiers:
        return 0

    is_slowed = bool(requested_modifiers & {"super slowed", "super slow", "ultra slowed", "slowed", "slow"})
    is_sped_up = bool(requested_modifiers & {"speed up", "speedup", "sped up", "spedup", "fast version"})
    mult_ratio = extract_speed_multiplier_ratio(req_query) if "speed_multiplier" in requested_modifiers else None

    if not (is_slowed or is_sped_up or mult_ratio):
        return 0

    temp_out = audio_path.with_name(f"mod_{audio_path.name}")
    if mult_ratio:
        filter_str = f"asetrate=44100*{mult_ratio:.4f},aresample=44100"
    elif is_slowed:
        filter_str = "asetrate=44100*0.89,aresample=44100"
    else:
        filter_str = "asetrate=44100*1.15,aresample=44100"

    print(f"{req_tag}[AUDIO_MOD] Применяем программный фильтр {filter_str} к оригинальному аудио...", flush=True)
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-i", str(audio_path),
        "-filter_complex", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-threads", "2",
        str(temp_out)
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if res.returncode == 0 and temp_out.exists() and temp_out.stat().st_size > 1000:
            shutil.move(temp_out, audio_path)
            from mutagen import File as MutagenFile
            mf = MutagenFile(audio_path)
            new_dur = int(mf.info.length) if (mf and mf.info and hasattr(mf.info, "length")) else 0
            print(f"{req_tag}[AUDIO_MOD] Фильтр успешно применен! Новая длительность: {new_dur}s", flush=True)
            return new_dur
        else:
            raise RuntimeError(f"FFmpeg audio modifier filter failed (code {res.returncode})")
    except subprocess.TimeoutExpired:
        if temp_out.exists():
            temp_out.unlink(missing_ok=True)
        raise RuntimeError("FFmpeg audio modifier filter timed out (60s limit)")


def _restore_studio_speed_and_pitch_if_needed(
    audio_path: Path,
    expected_duration: Optional[int],
    requested_modifiers: set,
    actual_dur: int,
    req_tag: str = "",
    is_apple_music: bool = False
) -> int:
    """
    Восстановление студийной скорости и тональности применяется для треков
    Apple Music и текстового поиска (где известен официальный канонический
    хронометраж, а сторонние аплоады на YouTube/SoundCloud могут быть искусственно замедлены).
    Для ссылок на другие платформы (Spotify, SoundCloud, прямые ссылки) фильтр не применяется.
    """
    if not is_apple_music or not expected_duration or expected_duration <= 35 or requested_modifiers or actual_dur <= 0:
        return actual_dur

    diff = abs(actual_dur - expected_duration)
    ratio = actual_dur / expected_duration
    if 2 < diff <= 35 and (0.85 <= ratio <= 1.15):
        print(
            f"{req_tag}[RESTORATION] Обнаружено отклонение скорости/тональности: {actual_dur}s vs {expected_duration}s "
            f"(ratio={ratio:.4f}, diff={diff}s). Восстанавливаем оригинальную 1.0x студийную скорость...",
            flush=True
        )
        temp_out = audio_path.with_name(f"restored_{audio_path.name}")
        try:
            import subprocess
            in_sr = 44100
            try:
                from mutagen import File as MutagenFile
                mf_probe = MutagenFile(audio_path)
                if mf_probe and mf_probe.info and hasattr(mf_probe.info, "sample_rate") and mf_probe.info.sample_rate:
                    in_sr = int(mf_probe.info.sample_rate)
            except Exception:
                pass

            is_m4a = audio_path.suffix.lower() == ".m4a"
            codec_args = ["-c:a", "aac", "-b:a", "320k"] if is_m4a else ["-c:a", "libmp3lame", "-b:a", "320k"]
            cmd = [
                "ffmpeg", "-y", "-i", str(audio_path),
                "-filter:a", f"asetrate={in_sr}*{ratio:.6f},aresample={in_sr}",
                "-vn"
            ] + codec_args + ["-threads", "0", str(temp_out)]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and temp_out.exists() and temp_out.stat().st_size > 1000:
                shutil.move(temp_out, audio_path)
                try:
                    from mutagen import File as MutagenFile
                    mf = MutagenFile(audio_path)
                    if mf and mf.info and hasattr(mf.info, "length"):
                        new_dur = int(round(mf.info.length))
                        print(f"{req_tag}[RESTORATION] Успешно восстановлен студийный хронометраж: {new_dur}s (было {actual_dur}s)", flush=True)
                        return new_dur
                except Exception:
                    pass
                return expected_duration
        except Exception as e:
            print(f"{req_tag}[RESTORATION] Ошибка восстановления: {e}", flush=True)
            if temp_out.exists():
                temp_out.unlink(missing_ok=True)
    return actual_dur


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
    audio_path: Path,
    title: Optional[str],
    artist: Optional[str],
    cover_path: Optional[Path] = None,
    album: Optional[str] = None
):
    """
    Записывает теги названия, исполнителя и обложки в один атомарный проход.
    Поддерживает как MP3 (ID3), так и M4A/AAC (MP4 атомы).
    """
    ext = audio_path.suffix.lower()
    if ext in [".m4a", ".mp4"]:
        try:
            mp4 = MP4(audio_path)
            if title:
                mp4["\xa9nam"] = [title]
            if artist:
                mp4["\xa9ART"] = [artist]
            if album:
                mp4["\xa9alb"] = [album]
            if cover_path and cover_path.exists():
                try:
                    with open(cover_path, "rb") as f:
                        c_data = f.read()
                    image_fmt = MP4Cover.FORMAT_JPEG if cover_path.suffix.lower() in [".jpg", ".jpeg"] else MP4Cover.FORMAT_PNG
                    mp4["covr"] = [MP4Cover(c_data, imageformat=image_fmt)]
                except Exception:
                    pass
            mp4.save()
        except Exception:
            pass
        return

    try:
        try:
            id3 = ID3(audio_path)
        except ID3NoHeaderError:
            id3 = ID3()

        if title:
            id3["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            id3["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            from mutagen.id3 import TALB
            id3["TALB"] = TALB(encoding=3, text=album)

        # Вшиваем обложку в тег ID3 APIC
        if cover_path and cover_path.exists():
            try:
                with open(cover_path, "rb") as albumart:
                    id3.delall("APIC")
                    id3.add(
                        APIC(
                            encoding=3,
                            mime="image/jpeg",
                            type=3,  # 3 is for album front cover
                            desc="Cover",
                            data=albumart.read()
                        )
                    )
            except Exception:
                pass

        # Одиночный сброс на диск
        id3.save(audio_path, v2_version=3)
    except Exception:
        pass


async def _download_remote_thumbnail(url: str, target_path: Path) -> Optional[Path]:
    """Скачивает обложку по URL через shared session, если yt-dlp её не предоставил."""
    try:
        session = get_shared_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.read()
                raw_thumb = target_path.with_suffix(".temp_img")
                raw_thumb.write_bytes(data)
                jpg_thumb = await asyncio.to_thread(_convert_thumbnail_to_jpg, raw_thumb)
                if raw_thumb.exists() and raw_thumb != jpg_thumb:
                    raw_thumb.unlink(missing_ok=True)
                return jpg_thumb
    except Exception:
        pass
    return None




def compute_candidate_penalty(
    candidate: dict,
    custom_artist: Optional[str] = None,
    custom_title: Optional[str] = None,
    expected_duration: Optional[int] = None,
    requested_modifiers: Optional[set] = None,
    is_text_input: bool = False,
    is_apple_music: bool = False,
    source: str = "youtube",
    clean_search: str = ""
) -> float:
    """Вычисляет штрафные баллы для ранжирования кандидата."""
    e = candidate
    cand_title = unicodedata.normalize("NFKC", e.get("title") or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    cand_uploader = unicodedata.normalize("NFKC", e.get("uploader") or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    cand_channel = unicodedata.normalize("NFKC", e.get("channel") or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    dur = e.get("duration") or 0
    fmt_str = (str(e.get("formats", "")) + str(e.get("format_id", ""))).lower()
    is_prev = "preview" in fmt_str or (expected_duration and expected_duration > 50 and 0 < dur <= 35)
    if is_prev:
        return 5000.0

    penalty = 0.0
    cand_text = f"{cand_title} {cand_uploader} {cand_channel}"

    # 0. Строгая валидация исполнителя (Artist Validation)
    if custom_artist:
        if not validate_artist_match(custom_artist, cand_text):
            penalty += 4000.0
        else:
            penalty -= 80.0

    # 1. Семантическое соответствие названия трека (Core Title Matching)
    core_title_words = extract_core_title_words(custom_title, artist=custom_artist) if custom_title else set()
    if core_title_words:
        match_ratio = compute_title_match_ratio(cand_title, core_title_words)
        if match_ratio >= 0.8:
            penalty -= 120.0
        elif match_ratio >= 0.5:
            penalty -= 30.0
        else:
            penalty += 4000.0

    ignore_cand_words = core_title_words | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())
    cand_modifiers = extract_modifiers(cand_text, ignore_words=ignore_cand_words)
    req_mods = requested_modifiers or set()

    cookies_info = get_cookies_info()

    if is_apple_music:
        if req_mods:
            matching = req_mods & cand_modifiers
            if matching:
                penalty -= 150.0 * len(matching)
            else:
                penalty += 200.0
        else:
            if cand_modifiers:
                penalty += 4000.0

        if expected_duration:
            diff = abs(dur - expected_duration)
            if not req_mods:
                if diff <= 2:
                    penalty -= 150.0
                elif diff <= 5:
                    penalty -= 60.0
                elif diff <= 10:
                    penalty += 150.0 + (diff * 10.0)
                else:
                    penalty += 1500.0 + (diff * 20.0)
            else:
                if diff <= 8:
                    penalty -= 60.0
                elif diff <= 20:
                    penalty -= 20.0
                elif diff <= 45:
                    penalty += diff * 5.0
                else:
                    penalty += 300.0 + (diff * 10.0)
        elif dur > 0:
            if dur > 900 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                penalty += 2500.0
            elif dur >= 45:
                penalty += 0.0
            else:
                penalty += 100.0 + (45 - dur) * 10.0

        cand_src = e.get("_source") or source
        if cand_src == "youtube":
            is_topic = cand_uploader.endswith("- topic") or cand_channel.endswith("- topic") or " - topic" in cand_uploader or " - topic" in cand_channel
            if is_topic:
                penalty -= 350.0
            elif "vevo" in cand_uploader or "official" in cand_uploader or "vevo" in cand_channel:
                penalty -= 120.0
        elif cand_src == "soundcloud":
            if custom_artist:
                ca = custom_artist.lower().strip()
                if ca in cand_uploader or ca.replace(" ", "") in cand_uploader.replace(" ", ""):
                    penalty -= 50.0
                else:
                    penalty += 250.0
            else:
                penalty += 250.0

    elif is_text_input:
        if req_mods:
            matching = req_mods & cand_modifiers
            unrequested = cand_modifiers - req_mods
            if matching:
                penalty -= 500.0 * len(matching)
                if unrequested:
                    penalty += 300.0 * len(unrequested)
            else:
                if unrequested:
                    penalty += 4000.0
                else:
                    penalty += 600.0
        else:
            if cand_modifiers:
                penalty += 4000.0

        if expected_duration:
            diff = abs(dur - expected_duration)
            if not req_mods:
                if diff <= 4:
                    penalty -= 160.0
                elif diff <= 8:
                    penalty += 350.0 + (diff * 20.0)
                elif diff <= 15:
                    penalty += 1000.0 + (diff * 30.0)
                else:
                    penalty += 3000.0 + (diff * 50.0)
            else:
                is_tempo_req = bool(req_mods & {"sped up", "spedup", "speed up", "speedup", "fast version", "slowed", "slow", "super slowed", "super slow", "ultra slowed"})
                if is_tempo_req:
                    penalty += 0.0
                elif diff <= 15:
                    penalty -= 60.0
                elif diff <= 45:
                    penalty += diff * 2.0
                else:
                    penalty += 200.0 + (diff * 5.0)
        elif dur > 0:
            if dur > 900 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                penalty += 2500.0
            elif dur >= 45:
                penalty += 0.0
            else:
                penalty += 100.0 + (45 - dur) * 10.0

        cand_src = e.get("_source") or source
        if cand_src == "youtube":
            is_topic = cand_uploader.endswith("- topic") or cand_channel.endswith("- topic") or " - topic" in cand_uploader or " - topic" in cand_channel
            if is_topic and not req_mods:
                penalty -= 350.0
            elif is_topic and req_mods:
                penalty += 0.0
            elif ("vevo" in cand_uploader or "official" in cand_uploader or "vevo" in cand_channel) and not req_mods:
                penalty -= 150.0
        elif cand_src == "soundcloud":
            if custom_artist:
                ca = custom_artist.lower().strip()
                if ca in cand_uploader or ca.replace(" ", "") in cand_uploader.replace(" ", ""):
                    penalty -= 50.0
                else:
                    penalty += 300.0
            else:
                penalty += 300.0
    else:
        if req_mods:
            matching = req_mods & cand_modifiers
            if matching:
                penalty -= 150.0 * len(matching)
            else:
                penalty += 200.0
        else:
            for mod in cand_modifiers:
                penalty += 350.0

        if expected_duration:
            diff = abs(dur - expected_duration)
            if not req_mods:
                if diff <= 4:
                    penalty -= 60.0
                elif diff <= 8:
                    penalty -= 20.0
                elif diff <= 15:
                    penalty += diff * 5.0
                elif diff <= 25:
                    penalty += 150.0 + (diff * 10.0)
                else:
                    penalty += 400.0 + (diff * 15.0)
            else:
                if diff <= 8:
                    penalty -= 60.0
                elif diff <= 20:
                    penalty -= 20.0
                elif diff <= 45:
                    penalty += diff * 5.0
                else:
                    penalty += 300.0 + (diff * 10.0)
        elif dur > 0:
            if dur > 900 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                penalty += 2500.0
            elif dur >= 45:
                penalty += 0.0
            else:
                penalty += 100.0 + (45 - dur) * 10.0

        cand_src = e.get("_source") or source
        if cand_src == "youtube":
            is_topic = cand_uploader.endswith("- topic") or cand_channel.endswith("- topic") or " - topic" in cand_uploader or " - topic" in cand_channel
            if is_topic and cookies_info.get("active"):
                penalty -= 150.0
            elif is_topic and not cookies_info.get("active"):
                penalty -= 100.0
            elif "vevo" in cand_uploader or "official" in cand_uploader or "vevo" in cand_channel:
                penalty -= 80.0
        elif cand_src == "soundcloud":
            if custom_artist:
                ca = custom_artist.lower().strip()
                if ca in cand_uploader or ca.replace(" ", "") in cand_uploader.replace(" ", ""):
                    penalty -= 50.0

    return penalty


def _sync_download(
    query_or_url: str,
    output_dir: Path,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE,
    skip_thumbnail: bool = False,
    expected_duration: Optional[int] = None,
    request_id: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    is_apple_music: bool = False,
    is_text_input: bool = False
) -> DownloadedAudio:
    """Синхронный процесс ускоренной загрузки и конвертации через yt-dlp."""
    req_tag = f"[MUSIC][request_id={request_id}] " if request_id else ""
    outtmpl = str(output_dir / "%(title).100B.%(ext)s")

    cookies_info = get_cookies_info()

    ydl_opts = {
        # Максимальный допустимый размер файла для предотвращения переполнения диска
        "max_filesize": MAX_FILE_SIZE_BYTES,
        # Приоритет отдаем прямому M4A (AAC) аудиопотоку: без долгой перекодировки FFmpeg в MP3 (-3..5 сек)
        # Если прямого M4A нет, берем лучший аудиопоток (webm/opus) либо видео+аудио поток для извлечения звука
        "format": "ba[ext=m4a]/ba[ext=mp3]/ba/bv*+ba/b/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "writethumbnail": not skip_thumbnail,
        "quiet": True,
        "no_warnings": True,
        # Ультра-быстрая сеть: увеличенный буфер и параллельная загрузка фрагментов потока
        "buffersize": 256 * 1024,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 5,
        "retries": 1,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
        # Быстрая конфигурация: все доступные потоки CPU, без лишнего пережатия
        "postprocessor_args": {
            "FFmpegExtractAudio": [
                "-threads", "0",
                "-vn"
            ]
        },
    }

    # Клиенты YouTube:
    is_youtube = not query_or_url.startswith("scsearch") and "soundcloud.com" not in query_or_url

    yt_proxy = get_current_youtube_proxy()
    if is_youtube and yt_proxy:
        ydl_opts["proxy"] = yt_proxy
        print(f"{req_tag}[DOWNLOADER] YouTube proxy enabled: {get_sanitized_proxy_info(yt_proxy)}", flush=True)
    elif is_youtube:
        ydl_opts.pop("proxy", None)
        print(f"{req_tag}[DOWNLOADER] YouTube proxy disabled", flush=True)

    if is_youtube and cookies_info["active"]:
        ydl_opts["cookiefile"] = cookies_info["path"]
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android"],
            }
        }
        print(f"{req_tag}[DOWNLOADER] Быстрый режим с cookies: {cookies_info['path']}", flush=True)
    elif is_youtube:
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android"],
            }
        }
        print(f"{req_tag}[DOWNLOADER] Режим без cookies (клиент android)", flush=True)

    is_search = query_or_url.startswith("ytsearch") or query_or_url.startswith("scsearch")

    invocations = []
    perf_timings = {
        "search": 0.0,
        "candidate_selection": 0.0,
        "download": 0.0,
        "ffmpeg": 0.0,
        "tags": 0.0
    }
    t_start_all = time.perf_counter()

    def _execute_extraction(options):
        source = "soundcloud" if (query_or_url.startswith("scsearch") or "soundcloud.com" in query_or_url) else "youtube"
        if is_search:
            # 1. Параллельный опрос YouTube + SoundCloud для мгновенного нахождения лучшего трека
            inv_idx1 = len(invocations) + 1
            t_s0 = time.perf_counter()

            if custom_artist and custom_title:
                clean_search = f"{custom_artist} - {custom_title}"
            else:
                clean_search = query_or_url.split(":", 1)[1] if ":" in query_or_url else query_or_url

            search_query_lower = clean_search.lower()
            entries = []

            def _fetch_candidates(target_q, src_name):
                s_opts = dict(options)
                s_opts.pop("extractor_args", None)  # Поисковые эндпоинты не должны использовать player_client!
                yt_proxy = get_current_youtube_proxy()
                if src_name == "soundcloud":
                    s_opts.pop("cookiefile", None)
                    s_opts.pop("proxy", None)  # Прокси применяется исключительно к YouTube-трафику
                elif src_name == "youtube" and yt_proxy:
                    s_opts["proxy"] = yt_proxy
                else:
                    s_opts.pop("proxy", None)
                s_opts["extract_flat"] = True
                s_opts["noplaylist"] = True
                s_opts["ignoreerrors"] = True
                s_opts["socket_timeout"] = 7
                s_opts["retries"] = 1
                try:
                    with yt_dlp.YoutubeDL(s_opts) as ydl_s:
                        info = ydl_s.extract_info(target_q, download=False)
                        items = [e for e in (info.get("entries") or []) if e]
                        # Fallback поиск в SoundCloud, если запрос с исполнителем вернул 0 результатов
                        if not items and src_name == "soundcloud" and ":" in target_q:
                            raw_target = target_q.split(":", 1)[1]
                            pure_title = raw_target.split(" - ", 1)[1] if " - " in raw_target else raw_target
                            pure_title = re.sub(r'\s*[\(\[](?:feat|ft\.)[^\)\]]*[\)\]]', '', pure_title, flags=re.IGNORECASE).strip()
                            clean_words = re.sub(r'[/\\_]+', ' ', pure_title).strip()
                            if len(clean_words) >= 3:
                                info2 = ydl_s.extract_info(f"scsearch4:{clean_words}", download=False)
                                items = [e for e in (info2.get("entries") or []) if e]
                        for item in items:
                            item["_source"] = src_name
                        return items
                except Exception as ex:
                    print(f"{req_tag}[SEARCH] Ошибка поиска {src_name}: {ex}", flush=True)
                    return []

            # Анализируем, запрашивал ли пользователь явно модификаторы (slowed, sped up, remix, cover и т.д.)
            req_context = f"{custom_artist or ''} {custom_title or ''} {clean_search}".lower()
            requested_modifiers = extract_modifiers(req_context) or set()
            dsp_supported_modifiers = {
                "slowed", "slow", "super slowed", "super slow", "ultra slowed",
                "sped up", "spedup", "speed up", "speedup", "fast version", "speed_multiplier"
            }
            semantic_requested = requested_modifiers - dsp_supported_modifiers

            # Ключевые слова названия трека для семантической проверки
            core_title_words = extract_core_title_words(custom_title or clean_search, custom_artist)
            if not core_title_words and custom_title:
                core_title_words = set(re.findall(r'[\w]+', custom_title.lower()))

            clean_artist_str = (custom_artist or "").strip()
            clean_title_str = (custom_title or "").strip()
            clean_q_simple = f"{clean_artist_str} {clean_title_str}".strip() if (clean_artist_str and clean_title_str) else clean_search

            import concurrent.futures
            if is_apple_music or is_text_input:
                # Для Apple Music и текстовых запросов: ищем официальные студийные дорожки Topic и чистый аудиопоток
                if requested_modifiers:
                    clean_core_title = " ".join(core_title_words) if core_title_words else ""
                    clean_core_query = f"{clean_artist_str} {clean_core_title}".strip()
                    search_tasks = [
                        ("youtube", f"ytsearch6:{clean_q_simple}"),
                        ("soundcloud", f"scsearch5:{clean_q_simple}")
                    ]
                    # Если пользователь запросил темповую модификацию (1.1x, slowed и др.),
                    # параллельно опрашиваем чистый студийный трек, чтобы применить эффект через FFmpeg при отсутствии готового релиза
                    if clean_core_query and clean_core_query.lower() != clean_q_simple.lower():
                        search_tasks.append(("youtube", f"ytsearch3:{clean_core_query}"))
                else:
                    search_tasks = [
                        ("youtube", f"ytsearch5:{clean_q_simple} Topic"),
                        ("youtube", f"ytsearch5:{clean_q_simple}"),
                        ("soundcloud", f"scsearch5:{clean_q_simple}")
                    ]
                    if clean_search and clean_search != clean_q_simple:
                        search_tasks.append(("youtube", f"ytsearch3:{clean_search}"))
            else:
                # Для ссылок на другие платформы (Spotify, YouTube, SoundCloud) - ультрабыстрый минимальный опрос
                search_tasks = [
                    ("youtube", f"ytsearch5:{clean_q_simple}"),
                    ("soundcloud", f"scsearch5:{clean_q_simple}")
                ]

            clean_core_title = " ".join(core_title_words) if core_title_words else ""
            if clean_core_title and custom_artist and requested_modifiers:
                # Дополнительный точный поиск в SoundCloud по имени артиста и ключевым словам названия
                search_tasks.append(("soundcloud", f"scsearch3:{custom_artist} {clean_core_title}"))

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(search_tasks))
            try:
                futures = [executor.submit(_fetch_candidates, q, src) for src, q in search_tasks]
                done, not_done = concurrent.futures.wait(futures, timeout=7.0)
                for f in done:
                    try:
                        res = f.result()
                        if res:
                            entries.extend(res)
                    except Exception:
                        pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            # Если параллельный опрос не вернул ни одного YouTube-кандидата,
            # выполняем чистый fallback-запрос к YouTube без cookies
            has_youtube = any(e.get("_source") == "youtube" for e in entries)
            if not has_youtube:
                try:
                    fb_opts = dict(options)
                    fb_opts.pop("extractor_args", None)
                    yt_proxy = get_current_youtube_proxy()
                    if yt_proxy:
                        fb_opts["proxy"] = yt_proxy
                    else:
                        fb_opts.pop("proxy", None)
                    fb_opts["extract_flat"] = True
                    fb_opts["noplaylist"] = True
                    fb_opts["ignoreerrors"] = True
                    fb_opts["socket_timeout"] = 7
                    fb_opts["retries"] = 1
                    with yt_dlp.YoutubeDL(fb_opts) as ydl_yt_fb:
                        info_yt = ydl_yt_fb.extract_info(f"ytsearch5:{clean_q_simple}", download=False)
                        fb_items = [e for e in (info_yt.get("entries") or []) if e]
                        for fe in fb_items:
                            fe["_source"] = "youtube"
                        entries.extend(fb_items)
                        if fb_items:
                            print(f"{req_tag}[SEARCH] YouTube standalone fallback вернул {len(fb_items)} кандидатов.", flush=True)
                except Exception as fb_err:
                    print(f"{req_tag}[SEARCH] YouTube standalone fallback search failed: {fb_err}", flush=True)

            t_s1 = time.perf_counter()
            dur_s = t_s1 - t_s0
            perf_timings["search"] += dur_s

            invocations.append({
                "invocation": inv_idx1,
                "purpose": "parallel_candidate_search",
                "source": "youtube+soundcloud",
                "start": t_s0 - t_start_all,
                "end": t_s1 - t_start_all,
                "duration": dur_s,
                "result": f"OK ({len(entries)} candidates)"
            })
            print(f"{req_tag}[YTDLP] invocation=#{inv_idx1} purpose='parallel_candidate_search' start={t_s0 - t_start_all:.2f}s end={t_s1 - t_start_all:.2f}s duration={dur_s:.2f}s result='OK ({len(entries)} candidates)'", flush=True)

            if not entries:
                raise ValueError("Трек не найден по данному запросу.")

            # 2. Интеллектуальный Query-Aware скоринг кандидатов
            t_c0 = time.perf_counter()

            def _candidate_penalty(e):
                return compute_candidate_penalty(
                    candidate=e,
                    custom_artist=custom_artist,
                    custom_title=custom_title,
                    expected_duration=expected_duration,
                    requested_modifiers=requested_modifiers,
                    is_text_input=is_text_input,
                    is_apple_music=is_apple_music,
                    source=source,
                    clean_search=clean_search
                )

            ranked_candidates = sorted(entries, key=_candidate_penalty)
            t_c1 = time.perf_counter()
            perf_timings["candidate_selection"] += (t_c1 - t_c0)

            # 3. Цикл скачивания лучших кандидатов с принципом гарантированной доставки (Zero False Negatives)
            last_cand_error = None
            youtube_blocked = False
            best_fallback_info = None

            for cand_idx, selected_entry in enumerate(ranked_candidates):
                cand_source = selected_entry.get("_source") or source
                if youtube_blocked and cand_source == "youtube":
                    continue

                target_url = selected_entry.get("webpage_url") or selected_entry.get("url") or selected_entry.get("id")
                if target_url and not target_url.startswith("http") and "soundcloud" not in cand_source:
                    target_url = f"https://www.youtube.com/watch?v={target_url}"
                if not target_url:
                    continue

                cand_title = selected_entry.get("title") or target_url
                cand_dur = selected_entry.get("duration") or 0
                print(f"{req_tag}[YTDLP] Попытка загрузки кандидата #{cand_idx+1} ({cand_source}): '{cand_title}' ({cand_dur}s) url='{target_url}'", flush=True)

                cand_dl_opts = dict(options)
                cand_dl_opts["extract_flat"] = False
                if cand_source == "soundcloud":
                    cand_dl_opts.pop("cookiefile", None)
                    cand_dl_opts.pop("extractor_args", None)
                    cand_dl_opts.pop("proxy", None)  # Прокси применяется только к YouTube
                elif cand_source == "youtube":
                    yt_proxy = get_current_youtube_proxy()
                    if yt_proxy:
                        cand_dl_opts["proxy"] = yt_proxy
                        print(f"{req_tag}[DOWNLOADER] YouTube proxy enabled: {get_sanitized_proxy_info(yt_proxy)}", flush=True)
                    else:
                        cand_dl_opts.pop("proxy", None)
                        print(f"{req_tag}[DOWNLOADER] YouTube proxy disabled", flush=True)

                hook_times = {"dl_start": 0, "dl_end": 0, "pp_start": 0, "pp_end": 0}
                def p_hook(d):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Download cancelled by user")
                    if d.get("status") == "downloading" and not hook_times["dl_start"]:
                        hook_times["dl_start"] = time.perf_counter()
                    elif d.get("status") == "finished":
                        hook_times["dl_end"] = time.perf_counter()

                def pp_hook(d):
                    if d.get("status") == "started" and not hook_times["pp_start"]:
                        hook_times["pp_start"] = time.perf_counter()
                    elif d.get("status") == "finished":
                        hook_times["pp_end"] = time.perf_counter()

                cand_dl_opts["progress_hooks"] = [p_hook]
                cand_dl_opts["postprocessor_hooks"] = [pp_hook]

                ignore_cand_words = core_title_words | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())

                inv_idx2 = len(invocations) + 1
                t_d0 = time.perf_counter()
                try:
                    with yt_dlp.YoutubeDL(cand_dl_opts) as ydl_dl:
                        res_info = ydl_dl.extract_info(target_url, download=True)
                    t_d1 = time.perf_counter()
                    dur_dl_all = t_d1 - t_d0

                    # Проверяем появление готового аудиофайла (M4A или MP3)
                    audio_files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"] and not f.name.startswith("backup_")]
                    if not audio_files:
                        raise FileNotFoundError("Аудиофайл не был создан после обработки кандидата.")

                    actual_dur = int(res_info.get("duration") or 0)
                    if not actual_dur and audio_files:
                        try:
                            from mutagen import File as MutagenFile
                            mf = MutagenFile(audio_files[0])
                            if mf and mf.info and hasattr(mf.info, "length"):
                                actual_dur = int(mf.info.length)
                        except Exception:
                            pass

                    cand_entry_title = unicodedata.normalize("NFKC", res_info.get("title") or cand_title or "")
                    cand_match_ratio = compute_title_match_ratio(cand_entry_title, core_title_words)

                    # 1. Жесткая защита от неаутентичных треков: если ключевые слова названия известны,
                    # а кандидат имеет менее 50% совпадения, ЭТО ЧУЖАЯ ПЕСНЯ! Немедленно отклоняем.
                    if core_title_words and cand_match_ratio < 0.5:
                        print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' недостаточно соответствует названию ({cand_match_ratio:.2f} < 0.5, words={core_title_words}). Отклоняем как неаутентичный.", flush=True)
                        for temp_f in output_dir.iterdir():
                            if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                try:
                                    temp_f.unlink(missing_ok=True)
                                except Exception:
                                    pass
                        continue

                    # 2. Проверяем модификаторы и при необходимости восстанавливаем студийный темп/тональность
                    cand_text = f"{cand_entry_title} {selected_entry.get('uploader') or ''} {selected_entry.get('channel') or ''}".lower()
                    ignore_cand_words = core_title_words | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())
                    cand_modifiers = extract_modifiers(cand_text, ignore_words=ignore_cand_words)

                    # Если пользователь искал оригинал Apple Music или по тексту, а кандидат содержит несовместимые модификаторы:
                    if is_apple_music or is_text_input:
                        unrequested_cand_mods = cand_modifiers - requested_modifiers
                        if not requested_modifiers:
                            if cand_modifiers:
                                print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' содержит нежелательные модификаторы {cand_modifiers}. Отклоняем.", flush=True)
                                for temp_f in output_dir.iterdir():
                                    if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                        try:
                                            temp_f.unlink(missing_ok=True)
                                        except Exception:
                                            pass
                                continue
                        else:
                            # Пользователь явно запросил модификацию (например acoustic, live или remix):
                            # Если кандидат содержит несовместимые чужие модификаторы:
                            conflicting_mods = unrequested_cand_mods & {
                                "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix", "club mix", "dance mix",
                                "live", "лайв", "концерт", "performance", "cover", "кавер", "acoustic", "акустика",
                                "drum edit", "drums", "dnb", "драмка", "с драмкой",
                                "slowed", "slow", "sped up", "speed up", "nightcore",
                                "instrumental", "инструментал", "minus", "минус", "karaoke"
                            }
                            if conflicting_mods:
                                print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' содержит несовместимые модификаторы {conflicting_mods} (запрошено: {requested_modifiers}). Отклоняем.", flush=True)
                                for temp_f in output_dir.iterdir():
                                    if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                        try:
                                            temp_f.unlink(missing_ok=True)
                                        except Exception:
                                            pass
                                continue

                            # Если запрошен семантический модификатор (acoustic, live, remix, cover, instrumental, reverb),
                            # а кандидат его НЕ содержит — отклоняем, чтобы не отдать обычный студийный трек!
                            if semantic_requested and not (semantic_requested & cand_modifiers):
                                print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' не содержит запрошенный модификатор {semantic_requested}. Отклоняем.", flush=True)
                                for temp_f in output_dir.iterdir():
                                    if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                        try:
                                            temp_f.unlink(missing_ok=True)
                                        except Exception:
                                            pass
                                continue

                    new_dur = _apply_audio_modifier_if_needed(audio_files[0], requested_modifiers, cand_modifiers, req_tag, req_query=clean_search)
                    if new_dur > 0:
                        actual_dur = new_dur
                        res_info["duration"] = new_dur

                    # 3. Проверяем допустимость хронометража:
                    diff = abs(actual_dur - expected_duration) if (expected_duration and expected_duration > 35 and actual_dur > 0) else 0
                    is_duration_acceptable = False
                    if not expected_duration:
                        is_duration_acceptable = True
                    elif requested_modifiers and (requested_modifiers & cand_modifiers):
                        is_tempo_req = bool(requested_modifiers & {"slowed", "slow", "super slowed", "super slow", "ultra slowed", "sped up", "spedup", "speed up", "speedup", "fast version", "speed_multiplier"})
                        if is_tempo_req or diff <= 45:
                            is_duration_acceptable = True
                    elif requested_modifiers and bool(requested_modifiers & {"slowed", "slow", "super slowed", "super slow", "ultra slowed", "sped up", "spedup", "speed up", "speedup", "fast version", "speed_multiplier"}):
                        # Программно применили темповый модификатор
                        is_duration_acceptable = True
                    elif is_apple_music or is_text_input:
                        # Строгий допуск для студийного оригинала:
                        # Для верифицированных официальных релизов допускаем до 2% (макс 7с).
                        # Для неофициальных/сомнительных источников оставляем строгий лимит 4 секунды.
                        cand_uploader_l = (res_info.get("uploader") or selected_entry.get("uploader") or "").lower()
                        cand_channel_l = (res_info.get("channel") or selected_entry.get("channel") or "").lower()
                        is_official_high_confidence = (
                            "- topic" in cand_uploader_l or "- topic" in cand_channel_l or
                            "vevo" in cand_uploader_l or "vevo" in cand_channel_l or
                            "gazgolder" in cand_uploader_l or "gazgolder" in cand_channel_l or
                            "official" in cand_uploader_l or "official" in cand_channel_l
                        )
                        max_allowed_diff = max(4, min(7, int(expected_duration * 0.02))) if is_official_high_confidence else 4
                        if diff <= max_allowed_diff:
                            is_duration_acceptable = True
                    else:
                        # Для ВСЕХ остальных платформ: стандартный допуск 25 секунд
                        if diff <= 25:
                            is_duration_acceptable = True

                    print(
                        f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' ({cand_source}): "
                        f"actual_dur={actual_dur}s expected={expected_duration}s diff={diff}s "
                        f"match_ratio={cand_match_ratio:.2f} acceptable={is_duration_acceptable}",
                        flush=True
                    )

                    if is_duration_acceptable and (not core_title_words or cand_match_ratio >= 0.5):
                        # Идеальное попадание! Удаляем возможный бэкап и возвращаем результат
                        for old_f in output_dir.iterdir():
                            if old_f.name.startswith("backup_"):
                                old_f.unlink(missing_ok=True)

                        if hook_times["dl_start"] and hook_times["dl_end"]:
                            dur_net = hook_times["dl_end"] - hook_times["dl_start"]
                        else:
                            dur_net = dur_dl_all * 0.65

                        if hook_times["pp_start"] and hook_times["pp_end"]:
                            dur_ff = hook_times["pp_end"] - hook_times["pp_start"]
                        else:
                            dur_ff = max(0.1, dur_dl_all - dur_net)

                        perf_timings["download"] += dur_net
                        perf_timings["ffmpeg"] += dur_ff

                        invocations.append({
                            "invocation": inv_idx2,
                            "purpose": f"stream_download_cand_{cand_idx+1}",
                            "source": cand_source,
                            "start": t_d0 - t_start_all,
                            "end": t_d1 - t_start_all,
                            "duration": dur_dl_all,
                            "result": "OK"
                        })
                        print(f"{req_tag}[YTDLP] invocation=#{inv_idx2} candidate=#{cand_idx+1} purpose='stream_download' source='{cand_source}' duration={dur_dl_all:.2f}s result='OK'", flush=True)
                        return res_info

                    # Длительность отличается, сохраняем как резервный вариант ТОЛЬКО ЕСЛИ название совпадает и нет модификаторов!
                    cand_uploader_l = (res_info.get("uploader") or selected_entry.get("uploader") or "").lower()
                    cand_channel_l = (res_info.get("channel") or selected_entry.get("channel") or "").lower()
                    is_official_high_confidence = (
                        "- topic" in cand_uploader_l or "- topic" in cand_channel_l or
                        "vevo" in cand_uploader_l or "vevo" in cand_channel_l or
                        "gazgolder" in cand_uploader_l or "gazgolder" in cand_channel_l or
                        "official" in cand_uploader_l or "official" in cand_channel_l
                    )
                    max_backup_diff = (max(4, min(7, int(expected_duration * 0.02))) if is_official_high_confidence else 4) if (is_apple_music or is_text_input) else 12
                    has_required_mods = True
                    if semantic_requested:
                        has_required_mods = bool(semantic_requested & cand_modifiers)
                    if (cand_match_ratio >= 0.5 or not core_title_words) and has_required_mods and (requested_modifiers or not cand_modifiers) and diff <= max_backup_diff:
                        print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_title}' имеет допустимую резервную разницу длительности {diff}s (<= {max_backup_diff}s). Сохраняем как резерв.", flush=True)
                        if best_fallback_info is None or diff < best_fallback_info.get("diff", 99999):
                            for af in audio_files:
                                backup_p = output_dir / f"backup_{af.name}"
                                shutil.copy2(af, backup_p)
                            best_fallback_info = {"res_info": res_info, "diff": diff}

                    for temp_f in output_dir.iterdir():
                        if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                            try:
                                temp_f.unlink(missing_ok=True)
                            except Exception:
                                pass
                    continue
                except Exception as cand_err:
                    if cancel_event and cancel_event.is_set():
                        shutil.rmtree(output_dir, ignore_errors=True)
                        raise
                    last_cand_error = cand_err
                    cand_err_str = str(cand_err).lower()
                    is_drm = any(k in cand_err_str for k in ["drm protected", "drm", "copyright", "georestricted"])
                    if is_drm:
                        print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_title}' ({cand_source}) защищён DRM. Пропускаем.", flush=True)
                    else:
                        print(f"{req_tag}[DOWNLOADER] Кандидат #{cand_idx+1} не удался ({cand_err}).", flush=True)

                    # Если кандидат YouTube завершился ошибкой формата/клиента/бота, пробуем резервный вызов без cookies
                    if cand_source == "youtube" and not getattr(selected_entry, "_retried", False):
                        if any(k in cand_err_str for k in ["reload", "format", "sign in", "bot", "403", "429"]):
                            print(f"{req_tag}[DOWNLOADER] Пробуем резервный запуск для кандидата #{cand_idx+1} без cookies...", flush=True)
                            selected_entry["_retried"] = True
                            retry_cand_opts = dict(cand_dl_opts)
                            retry_cand_opts.pop("cookiefile", None)
                            retry_cand_opts["extractor_args"] = {
                                "youtube": {
                                    "player_client": ["android"]
                                }
                            }
                            try:
                                with yt_dlp.YoutubeDL(retry_cand_opts) as ydl_retry:
                                    res_info = ydl_retry.extract_info(target_url, download=True)
                                audio_files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"] and not f.name.startswith("backup_")]
                                if audio_files:
                                    retry_title = unicodedata.normalize("NFKC", res_info.get("title") or cand_title or "")
                                    retry_match_ratio = compute_title_match_ratio(retry_title, core_title_words)
                                    if core_title_words and retry_match_ratio < 0.5:
                                        print(f"{req_tag}[AUTHENTICITY] Резервный запуск: кандидат '{retry_title}' не соответствует названию ({retry_match_ratio:.2f} < 0.5). Отклоняем.", flush=True)
                                        for temp_f in output_dir.iterdir():
                                            if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                                try:
                                                    temp_f.unlink(missing_ok=True)
                                                except Exception:
                                                    pass
                                        continue

                                    retry_mods = extract_modifiers(f"{retry_title} {selected_entry.get('uploader') or ''}", ignore_words=ignore_cand_words)
                                    if not requested_modifiers and retry_mods:
                                        for temp_f in output_dir.iterdir():
                                            if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                                try:
                                                    temp_f.unlink(missing_ok=True)
                                                except Exception:
                                                    pass
                                        continue

                                    if requested_modifiers:
                                        if semantic_requested and not (semantic_requested & retry_mods):
                                            for temp_f in output_dir.iterdir():
                                                if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                                                    try:
                                                        temp_f.unlink(missing_ok=True)
                                                    except Exception:
                                                        pass
                                            continue

                                    new_retry_dur = _apply_audio_modifier_if_needed(audio_files[0], requested_modifiers, retry_mods, req_tag, req_query=clean_search)
                                    actual_dur = new_retry_dur if new_retry_dur > 0 else int(res_info.get("duration") or 0)
                                    if new_retry_dur > 0:
                                        res_info["duration"] = new_retry_dur

                                    diff = abs(actual_dur - expected_duration) if (expected_duration and expected_duration > 35 and actual_dur > 0) else 0
                                    cand_uploader_l = (res_info.get("uploader") or selected_entry.get("uploader") or "").lower()
                                    cand_channel_l = (res_info.get("channel") or selected_entry.get("channel") or "").lower()
                                    is_official_high_confidence = (
                                        "- topic" in cand_uploader_l or "- topic" in cand_channel_l or
                                        "vevo" in cand_uploader_l or "vevo" in cand_channel_l or
                                        "gazgolder" in cand_uploader_l or "gazgolder" in cand_channel_l or
                                        "official" in cand_uploader_l or "official" in cand_channel_l
                                    )
                                    max_retry_diff = 45 if requested_modifiers else ((max(4, min(7, int(expected_duration * 0.02))) if is_official_high_confidence else 4) if (is_apple_music or is_text_input) else 25)
                                    dur_ok = not expected_duration or (diff <= max_retry_diff)
                                    if dur_ok:
                                        print(f"{req_tag}[DOWNLOADER] Резервный запуск кандидата #{cand_idx+1} успешен!", flush=True)
                                        return res_info
                            except Exception as sub_retry_err:
                                print(f"{req_tag}[DOWNLOADER] Резервный запуск кандидата #{cand_idx+1} также завершился ошибкой ({sub_retry_err}).", flush=True)

                    for temp_f in output_dir.iterdir():
                        if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_"):
                            try:
                                temp_f.unlink(missing_ok=True)
                            except Exception:
                                pass

                    is_ip_blocked = any(m in cand_err_str for m in ["confirm you’re not a bot", "confirm you're not a bot", "sign in", "bot", "429", "too many requests"])
                    if is_ip_blocked and cand_source == "youtube":
                        print(f"{req_tag}[DOWNLOADER] YouTube заблокировал кандидата #{cand_idx+1}. Пропускаем YouTube и переключаемся на SoundCloud...", flush=True)
                        youtube_blocked = True
                    continue

            # Если идеального кандидата не нашлось, но скачался резервный — ВОССТАНАВЛИВАЕМ И ОТДАЕМ ЕГО!
            if best_fallback_info:
                print(f"{req_tag}[AUTHENTICITY] Восстанавливаем лучший скачанный резерв (diff={best_fallback_info['diff']}s)", flush=True)
                for bf in list(output_dir.iterdir()):
                    if bf.name.startswith("backup_"):
                        orig_name = bf.name.replace("backup_", "", 1)
                        bf.rename(output_dir / orig_name)
                return best_fallback_info["res_info"]

            # Экстренный поиск в SoundCloud (запрещён для прямых ссылок YouTube!)
            is_direct_yt = bool(("youtube.com" in query_or_url or "youtu.be" in query_or_url) and not query_or_url.startswith("ytsearch"))
            if (last_cand_error or not entries) and not is_direct_yt:
                print(f"{req_tag}[DOWNLOADER] Экстренный Fallback: поиск трека '{clean_search}' в SoundCloud...", flush=True)
                try:
                    sc_opts = dict(options)
                    sc_opts.pop("cookiefile", None)
                    sc_opts.pop("extractor_args", None)
                    sc_opts.pop("proxy", None)  # Прокси применяется только к YouTube
                    sc_opts["extract_flat"] = True
                    sc_opts["noplaylist"] = True
                    sc_opts["ignoreerrors"] = True
                    with yt_dlp.YoutubeDL(sc_opts) as ydl_sc:
                        sc_raw = ydl_sc.extract_info(f"scsearch4:{clean_search}", download=False)
                        sc_entries = [e for e in sc_raw.get("entries", []) if e]
                        if not sc_entries:
                            pure_sc = clean_search.split(" - ", 1)[1] if " - " in clean_search else clean_search
                            pure_sc = re.sub(r'[/\\_]+', ' ', pure_sc).strip()
                            if len(pure_sc) >= 3:
                                sc_raw2 = ydl_sc.extract_info(f"scsearch4:{pure_sc}", download=False)
                                sc_entries = [e for e in sc_raw2.get("entries", []) if e]
                        for se in sc_entries:
                            se["_source"] = "soundcloud"
                        if sc_entries:
                            sc_ranked = sorted(sc_entries, key=_candidate_penalty)
                            for s_cand in sc_ranked:
                                s_url = s_cand.get("webpage_url") or s_cand.get("url")
                                s_title = unicodedata.normalize("NFKC", s_cand.get("title") or "")
                                s_mods = extract_modifiers(f"{s_title} {s_cand.get('uploader') or ''}", ignore_words=ignore_cand_words)
                                s_dur = s_cand.get("duration") or 0
                                s_diff = abs(s_dur - expected_duration) if (expected_duration and expected_duration > 35 and s_dur > 0) else 0
                                if not requested_modifiers:
                                    if (is_apple_music or is_text_input) and s_mods:
                                        continue
                                else:
                                    s_unrequested = s_mods - requested_modifiers
                                    s_conflicting = s_unrequested & {
                                        "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix", "club mix", "dance mix",
                                        "live", "лайв", "концерт", "performance", "cover", "кавер", "acoustic", "акустика",
                                        "drum edit", "drums", "dnb", "драмка", "с драмкой",
                                        "slowed", "slow", "sped up", "speed up", "nightcore",
                                        "instrumental", "инструментал", "minus", "минус", "karaoke"
                                    }
                                    if s_conflicting:
                                        continue
                                    if semantic_requested and not (semantic_requested & s_mods):
                                        continue

                                max_sc_diff = 45 if requested_modifiers else (4 if (is_apple_music or is_text_input) else 35)
                                if expected_duration and not requested_modifiers and s_diff > max_sc_diff:
                                    continue
                                if s_url:
                                    sc_opts_dl = dict(options)
                                    sc_opts_dl.pop("cookiefile", None)
                                    sc_opts_dl.pop("extractor_args", None)
                                    sc_opts_dl.pop("proxy", None)  # Прокси только для YouTube
                                    sc_opts_dl["extract_flat"] = False
                                    try:
                                        with yt_dlp.YoutubeDL(sc_opts_dl) as ydl_sc_dl:
                                            res_cand = ydl_sc_dl.extract_info(s_url, download=True)
                                            audio_files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"] and not f.name.startswith("backup_")]
                                            if audio_files:
                                                sc_dur = int(res_cand.get("duration") or 0)
                                                sc_diff = abs(sc_dur - expected_duration) if (expected_duration and expected_duration > 35 and sc_dur > 0) else 0
                                                if (is_apple_music or is_text_input) and expected_duration and not requested_modifiers and sc_diff > 4:
                                                    print(f"{req_tag}[DOWNLOADER] SoundCloud track '{s_title}' diff={sc_diff}s > 4s. Rejecting.", flush=True)
                                                    for temp_f in output_dir.iterdir():
                                                        if temp_f.is_file() and not temp_f.name.startswith("cover"):
                                                            temp_f.unlink(missing_ok=True)
                                                    continue
                                                new_sc_dur = _apply_audio_modifier_if_needed(audio_files[0], requested_modifiers, s_mods, req_tag, req_query=clean_search)
                                                if new_sc_dur > 0:
                                                    res_cand["duration"] = new_sc_dur
                                                return res_cand
                                    except Exception as s_err:
                                        print(f"{req_tag}[DOWNLOADER] SoundCloud fallback candidate '{s_url}' не удался: {s_err}", flush=True)
                                        continue
                except Exception as sc_err:
                    print(f"{req_tag}[DOWNLOADER] Экстренный поиск SoundCloud не удался: {sc_err}", flush=True)
                if last_cand_error:
                    if any(k in str(last_cand_error).lower() for k in ["drm protected", "drm"]):
                        raise ValueError("Трек защищён DRM на найденных источниках. Попробуйте другой запрос или ссылку.")
                    raise last_cand_error
            raise ValueError("Ни один кандидат поиска не подошел для загрузки.")
        else:
            inv_idx = len(invocations) + 1
            t_d0 = time.perf_counter()
            dl_opts = dict(options)
            is_direct_yt = not query_or_url.startswith("scsearch") and "soundcloud.com" not in query_or_url
            if is_direct_yt:
                yt_proxy = get_current_youtube_proxy()
                if yt_proxy:
                    dl_opts["proxy"] = yt_proxy
                    print(f"{req_tag}[DOWNLOADER] YouTube proxy enabled: {get_sanitized_proxy_info(yt_proxy)}", flush=True)
                else:
                    dl_opts.pop("proxy", None)
                    print(f"{req_tag}[DOWNLOADER] YouTube proxy disabled", flush=True)
            hook_times = {"dl_start": 0, "dl_end": 0, "pp_start": 0, "pp_end": 0}
            def p_hook(d):
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("Download cancelled by user")
                if d.get("status") == "downloading" and not hook_times["dl_start"]:
                    hook_times["dl_start"] = time.perf_counter()
                elif d.get("status") == "finished":
                    hook_times["dl_end"] = time.perf_counter()

            def pp_hook(d):
                if d.get("status") == "started" and not hook_times["pp_start"]:
                    hook_times["pp_start"] = time.perf_counter()
                elif d.get("status") == "finished":
                    hook_times["pp_end"] = time.perf_counter()

            dl_opts["progress_hooks"] = [p_hook]
            dl_opts["postprocessor_hooks"] = [pp_hook]

            with yt_dlp.YoutubeDL(dl_opts) as ydl_dl:
                res_info = ydl_dl.extract_info(query_or_url, download=True)
            t_d1 = time.perf_counter()
            dur_dl_all = t_d1 - t_d0

            if hook_times["dl_start"] and hook_times["dl_end"]:
                dur_net = hook_times["dl_end"] - hook_times["dl_start"]
            else:
                dur_net = dur_dl_all * 0.65

            if hook_times["pp_start"] and hook_times["pp_end"]:
                dur_ff = hook_times["pp_end"] - hook_times["pp_start"]
            else:
                dur_ff = max(0.1, dur_dl_all - dur_net)

            perf_timings["download"] += dur_net
            perf_timings["ffmpeg"] += dur_ff

            invocations.append({
                "invocation": inv_idx,
                "purpose": "direct_download",
                "source": source,
                "start": t_d0 - t_start_all,
                "end": t_d1 - t_start_all,
                "duration": dur_dl_all,
                "result": "OK"
            })
            print(f"{req_tag}[YTDLP] invocation=#{inv_idx} purpose='direct_download' source='{source}' start={t_d0 - t_start_all:.2f}s end={t_d1 - t_start_all:.2f}s duration={dur_dl_all:.2f}s result='OK'", flush=True)
            return res_info

    try:
        info = _execute_extraction(ydl_opts)
    except Exception as extract_err:
        err_msg = str(extract_err).lower()
        should_retry_no_cookies = "cookiefile" in ydl_opts and any(
            m in err_msg for m in [
                "sign in", "bot", "cookie", "reload", "403",
                "requested format", "format", "not available", "unavailable"
            ]
        )
        def _fallback_direct_search(last_err):
            # Точная прямая ссылка не должна молча заменяться другим треком (Section 11, 15)
            print(f"[DOWNLOADER] Прямая ссылка недоступна: {last_err}", flush=True)
            raise last_err

        if should_retry_no_cookies:
            print(f"[DOWNLOADER] Сессия cookies вызвала ошибку ({extract_err}). Пробуем чистый запуск без cookies с клиентами android/ios...", flush=True)
            ydl_opts_retry = dict(ydl_opts)
            ydl_opts_retry.pop("cookiefile", None)
            ydl_opts_retry["format"] = "ba[ext=m4a]/ba[ext=mp3]/ba/bv*+ba/b/best"
            ydl_opts_retry["extractor_args"] = {
                "youtube": {
                    "player_client": ["android", "mweb", "ios"],
                }
            }
            try:
                info = _execute_extraction(ydl_opts_retry)
            except Exception as retry_err:
                return _fallback_direct_search(retry_err)
        else:
            return _fallback_direct_search(extract_err)

    if "entries" in info:
        if not info["entries"]:
            raise ValueError("Трек не найден по данному запросу.")
        info = info["entries"][0]

    audio_files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"]]
    if not audio_files:
        raise FileNotFoundError("Аудиофайл не был создан после обработки.")

    audio_path = audio_files[0]
    filesize = audio_path.stat().st_size

    # Находим обложку
    thumb_candidates = list(output_dir.glob("*.webp")) + list(output_dir.glob("*.jpg")) + list(output_dir.glob("*.png"))
    thumbnail_path = None
    if thumb_candidates:
        thumbnail_path = _convert_thumbnail_to_jpg(thumb_candidates[0])

    extracted_title = _clean_audio_branding(custom_title or info.get("track") or info.get("title") or "Unknown Track")
    extracted_artist = _clean_audio_branding(custom_artist or info.get("artist") or info.get("uploader") or info.get("channel") or "Unknown Artist")
    duration = int(info.get("duration") or 0)
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(audio_path)
        if mf and mf.info and hasattr(mf.info, "length"):
            duration = int(round(mf.info.length))
    except Exception:
        pass

    # Финальная валидация хронометража перед отдачей DownloadedAudio (Apple Music, Spotify, Deezer, Text search)
    is_direct_media_url = bool(not query_or_url.startswith(("ytsearch", "scsearch")) and any(d in query_or_url.lower() for d in ("youtube.com", "youtu.be", "soundcloud.com", "bandcamp.com", "vk.com", "tiktok.com")))
    if not is_direct_media_url and expected_duration and expected_duration > 35:
        query_mods = extract_modifiers(f"{custom_artist or ''} {custom_title or ''}")
        if not query_mods and duration > 0:
            final_dl_diff = abs(duration - expected_duration)
            max_final_gate = max(4, min(7, int(expected_duration * 0.02)))
            if final_dl_diff > max_final_gate:
                raise ValueError(
                    f"Финальная проверка отклонена: итоговый аудиофайл имеет длительность {duration}с "
                    f"при эталоне {expected_duration}с (разница {final_dl_diff}с > {max_final_gate}с)."
                )

    t_tag0 = time.perf_counter()
    _apply_custom_metadata(audio_path, extracted_title, extracted_artist, thumbnail_path)
    perf_timings["tags"] = time.perf_counter() - t_tag0

    return DownloadedAudio(
        file_path=audio_path,
        title=extracted_title,
        artist=extracted_artist,
        duration=duration,
        thumbnail_path=thumbnail_path,
        filesize=filesize,
        folder_path=output_dir,
        perf_timings=perf_timings,
        invocations=invocations
    )


def _clean_audio_branding(text: Optional[str]) -> Optional[str]:
    """Удаляет брендовые приписки платформ (on Apple Music, в Apple Music, - Topic) из названий и исполнителей."""
    if not text:
        return text
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s*-\s*(?:Topic|Тема)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+(?:on|в|sur|en|auf|su)\s+Apple\s*Music.*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*Apple\s*Music.*$', '', text, flags=re.IGNORECASE)
    return text.strip()


def _build_fallback_queries(
    custom_artist: Optional[str],
    custom_title: Optional[str],
    query_or_url: str
) -> list[str]:
    """
    Генерирует ранжированный список очищенных поисковых запросов для fallback (SoundCloud / YouTube).
    Удаляет спецсимволы, суффиксы YouTube (- Topic / - Тема), feat-конструкции и лишние теги.
    """
    queries = []

    # 1. Очистка артиста
    clean_artist = ""
    if custom_artist:
        a = _clean_audio_branding(custom_artist)
        a = re.sub(r'[/\\:;*?"<>|]+', ' ', a)
        clean_artist = " ".join(a.split()).strip()

    # 2. Очистка названия
    clean_title = ""
    title_no_feat = ""
    if custom_title:
        t = _clean_audio_branding(custom_title)
        t = re.sub(
            r'\s*[\(\[](?:Official|Music Video|Audio|Lyric|Video|Remix|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
            '',
            t,
            flags=re.IGNORECASE
        )
        t = re.sub(r'[/\\:;*?"<>|]+', ' ', t)
        clean_title = " ".join(t.split()).strip()

        t_nf = re.sub(r'\s*[\(\[](?:feat\.?|ft\.?)[^\)\]]*[\)\]]', '', clean_title, flags=re.IGNORECASE)
        t_nf = re.sub(r'\s+(?:feat\.?|ft\.?)\s+.*$', '', t_nf, flags=re.IGNORECASE)
        title_no_feat = " ".join(t_nf.split()).strip()

    if clean_artist and clean_title:
        queries.append(f"{clean_artist} {clean_title}")
    if clean_artist and title_no_feat and title_no_feat != clean_title:
        queries.append(f"{clean_artist} {title_no_feat}")
    if clean_title:
        queries.append(clean_title)
    if title_no_feat and title_no_feat != clean_title:
        queries.append(title_no_feat)

    if not queries:
        if query_or_url.startswith(("scsearch", "ytsearch")):
            raw = query_or_url.split(":", 1)[1]
            queries.append(re.sub(r'[/\\:;*?"<>|]+', ' ', raw).strip())
        else:
            path_parts = [p for p in urllib.parse.urlparse(query_or_url).path.split('/') if p and p not in ('sets', 'track', 'song', 'watch')]
            if path_parts:
                slug = " ".join(path_parts[-2:]).replace("-", " ").replace("_", " ").replace("—", " ")
                queries.append(re.sub(r'[/\\:;*?"<>|]+', ' ', slug).strip())

    seen = set()
    deduped = []
    for q in queries:
        norm = " ".join(q.split()).strip()
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            deduped.append(norm)
    return deduped


async def download_track(
    query_or_url: str,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE,
    expected_duration: Optional[int] = None,
    request_id: Optional[str] = None,
    is_apple_music: bool = False,
    is_text_input: bool = False
) -> DownloadedAudio:
    """
    Асинхронная функция загрузки трека в MP3.
    """
    req_tag = f"[MUSIC][request_id={request_id}] " if request_id else ""
    session_id = uuid.uuid4().hex
    output_dir = DOWNLOADS_DIR / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    cancel_event = threading.Event()

    # Параллельная загрузка обложки в фоне во время скачивания аудио (экономит 0.5-1.5 сек)
    thumb_task = None
    if thumbnail_url:
        thumb_task = asyncio.create_task(
            _download_remote_thumbnail(thumbnail_url, output_dir / "cover")
        )

    try:
        audio = await asyncio.to_thread(
            _sync_download,
            query_or_url,
            output_dir,
            custom_title,
            custom_artist,
            bitrate,
            bool(thumbnail_url),
            expected_duration,
            request_id,
            cancel_event,
            is_apple_music,
            is_text_input
        )

        # Если результат подозрительно короткий (< 35s), а ожидался полноценный трек (> 60s)
        if expected_duration and expected_duration > 60 and audio.duration <= 35:
            raise ValueError(f"Скачано превью ({audio.duration}s) вместо полного трека ({expected_duration}s)")

        if thumb_task:
            try:
                downloaded_thumb = await thumb_task
                if downloaded_thumb and downloaded_thumb.exists():
                    audio.thumbnail_path = downloaded_thumb
                    _apply_custom_metadata(audio.file_path, audio.title, audio.artist, audio.thumbnail_path)
            except Exception:
                pass

        if audio.thumbnail_path and not audio.thumbnail_path.exists():
            audio.thumbnail_path = None

        elapsed = time.time() - t_start
        print(f"{req_tag}[DOWNLOADER] [OK] Трек успешно получен за {elapsed:.2f} сек: {audio.title}", flush=True)
        return audio
    except asyncio.CancelledError:
        cancel_event.set()
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except Exception as primary_error:
        elapsed = time.time() - t_start
        print(f"{req_tag}[DOWNLOADER] Первичная загрузка {query_or_url} ({elapsed:.2f}s) вернула ошибку: {primary_error}", flush=True)

        # Очищаем только временные аудиофайлы, сохраняя скачанную обложку
        if output_dir.exists():
            for item in output_dir.iterdir():
                if item.is_file() and not item.name.startswith("cover"):
                    try:
                        item.unlink(missing_ok=True)
                    except Exception:
                        pass
        else:
            output_dir.mkdir(parents=True, exist_ok=True)

        fallback_queries = _build_fallback_queries(custom_artist, custom_title, query_or_url)
        err_msg = str(primary_error).lower()
        is_bot_blocked = any(
            marker in err_msg
            for marker in ["confirm you’re not a bot", "confirm you're not a bot", "http error 429", "too many requests", "bot", "sign in"]
        )

        # 1. Fallback в YouTube Search (поиск аудиорелиза из 5 кандидатов)
        # Пропускаем, если YouTube заблокировал IP проверкой на бота или ошибкой 429
        if fallback_queries and not query_or_url.startswith("ytsearch") and not is_bot_blocked:
            yt_query = fallback_queries[0]
            try:
                print(f"{req_tag}[DOWNLOADER] Попытка Fallback через YouTube Search: ytsearch5:{yt_query}", flush=True)
                audio = await asyncio.to_thread(
                    _sync_download,
                    f"ytsearch5:{yt_query}",
                    output_dir,
                    custom_title,
                    custom_artist,
                    bitrate,
                    bool(thumbnail_url),
                    expected_duration,
                    request_id,
                    cancel_event,
                    is_apple_music,
                    is_text_input
                )
                if expected_duration and expected_duration > 60 and audio.duration <= 35:
                    raise ValueError(f"Fallback YouTube вернул превью ({audio.duration}s)")

                if thumb_task:
                    try:
                        downloaded_thumb = await thumb_task
                        if downloaded_thumb and downloaded_thumb.exists() and not audio.thumbnail_path:
                            audio.thumbnail_path = downloaded_thumb
                            _apply_custom_metadata(audio.file_path, audio.title, audio.artist, audio.thumbnail_path)
                    except Exception:
                        pass
                if audio.thumbnail_path and not audio.thumbnail_path.exists():
                    audio.thumbnail_path = None
                elapsed_fb = time.time() - t_start
                print(f"{req_tag}[DOWNLOADER] [OK] Трек получен через YouTube Search Fallback за {elapsed_fb:.2f} сек: {audio.title}", flush=True)
                return audio
            except Exception as yt_err:
                print(f"{req_tag}[DOWNLOADER] Fallback YouTube Search не удался: {yt_err}", flush=True)
        is_direct_yt = bool(("youtube.com" in query_or_url or "youtu.be" in query_or_url) and not query_or_url.startswith("ytsearch"))
        if is_direct_yt:
            print(f"{req_tag}[DOWNLOADER] Прямая ссылка YouTube завершилась ошибкой ({primary_error}). Fallback в SoundCloud запрещён.", flush=True)
            shutil.rmtree(output_dir, ignore_errors=True)
            raise primary_error

        # 2. Fallback в SoundCloud (выбирает полный трек среди лучших вариантов запроса)
        # Применяется только если исходный запрос был ссылкой на сторонний сервис, а не прямым YouTube или поиском
        if not query_or_url.startswith(("ytsearch", "scsearch")) and not is_direct_yt:
            for fb_q in fallback_queries[:2]:
                try:
                    print(f"{req_tag}[DOWNLOADER] Попытка Fallback через SoundCloud: scsearch5:{fb_q}", flush=True)
                    audio = await asyncio.to_thread(
                        _sync_download,
                        f"scsearch5:{fb_q}",
                        output_dir,
                        custom_title,
                        custom_artist,
                        bitrate,
                        bool(thumbnail_url),
                        expected_duration,
                        request_id,
                        cancel_event,
                        is_apple_music,
                        is_text_input
                    )
                    if expected_duration and expected_duration > 60 and audio.duration <= 35:
                        raise ValueError(f"Fallback SoundCloud вернул превью ({audio.duration}s)")

                    if thumb_task:
                        try:
                            downloaded_thumb = await thumb_task
                            if downloaded_thumb and downloaded_thumb.exists() and not audio.thumbnail_path:
                                audio.thumbnail_path = downloaded_thumb
                                _apply_custom_metadata(audio.file_path, audio.title, audio.artist, audio.thumbnail_path)
                        except Exception:
                            pass
                    if audio.thumbnail_path and not audio.thumbnail_path.exists():
                        audio.thumbnail_path = None
                    elapsed_fb = time.time() - t_start
                    print(f"{req_tag}[DOWNLOADER] [OK] Трек получен через SoundCloud Fallback за {elapsed_fb:.2f} сек: {audio.title}", flush=True)
                    return audio
                except Exception as sc_err:
                    print(f"{req_tag}[DOWNLOADER] Fallback SoundCloud '{fb_q}' не удался: {sc_err}", flush=True)

        shutil.rmtree(output_dir, ignore_errors=True)
        raise primary_error
