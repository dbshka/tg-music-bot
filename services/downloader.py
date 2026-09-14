import asyncio
import os
import re
import shutil
import threading
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
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, ID3NoHeaderError

from config import DOWNLOADS_DIR, DEFAULT_AUDIO_BITRATE, MAX_FILE_SIZE_BYTES, BASE_DIR, get_cookies_info
from services.http_client import get_shared_session


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
    """
    Записывает ID3-теги названия, исполнителя и обложки в один атомарный проход.
    Исключает двойную перезапись MP3-файла на диск.
    """
    try:
        try:
            id3 = ID3(mp3_path)
        except ID3NoHeaderError:
            id3 = ID3()

        if title:
            id3["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            id3["TPE1"] = TPE1(encoding=3, text=artist)

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
        id3.save(mp3_path, v2_version=3)
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



def _sync_download(
    query_or_url: str,
    output_dir: Path,
    custom_title: Optional[str] = None,
    custom_artist: Optional[str] = None,
    bitrate: str = DEFAULT_AUDIO_BITRATE,
    skip_thumbnail: bool = False,
    expected_duration: Optional[int] = None,
    request_id: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None
) -> DownloadedAudio:
    """Синхронный процесс ускоренной загрузки и конвертации через yt-dlp."""
    req_tag = f"[MUSIC][request_id={request_id}] " if request_id else ""
    outtmpl = str(output_dir / "%(title).100B.%(ext)s")

    cookies_info = get_cookies_info()

    ydl_opts = {
        # Приоритет отдаем прямым прогрессивным HTTP MP3/M4A аудиопотокам
        # (в десятки раз быстрее HLS чанков и исключает лишнюю перекодировку MP3)
        "format": "ba[protocol^=http][ext=mp3]/ba[protocol^=http][ext=m4a]/ba[ext=m4a]/ba[protocol^=http]/ba/best",
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
        # Оптимальная конфигурация MP3: 1 поток кодировщика (экономия CPU), compression_level 2 (высокое качество звука)
        "postprocessor_args": {
            "FFmpegExtractAudio": [
                "-threads", "1",
                "-compression_level", "2",
                "-joint_stereo", "1",
                "-vn"
            ]
        },
    }

    # Клиенты YouTube:
    # Android клиент работает в разы быстрее desktop и исключает ошибки SABR streaming и 403
    is_youtube = not query_or_url.startswith("scsearch") and "soundcloud.com" not in query_or_url

    if is_youtube and cookies_info["active"]:
        ydl_opts["cookiefile"] = cookies_info["path"]
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"],
            }
        }
        print(f"{req_tag}[DOWNLOADER] Быстрый режим с cookies: {cookies_info['path']}", flush=True)
    elif is_youtube:
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"],
            }
        }
        print(f"{req_tag}[DOWNLOADER] Режим без cookies (клиенты android, ios, mweb)", flush=True)

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
            # 1. Извлекаем кандидатов поиска БЕЗ полного скачивания веб-страниц (extract_flat=True)
            search_opts = dict(options)
            search_opts["extract_flat"] = True
            search_opts["noplaylist"] = True

            search_opts["ignoreerrors"] = True

            inv_idx1 = len(invocations) + 1
            t_s0 = time.perf_counter()
            with yt_dlp.YoutubeDL(search_opts) as ydl_search:
                search_info = ydl_search.extract_info(query_or_url, download=False)
            t_s1 = time.perf_counter()
            dur_s = t_s1 - t_s0
            perf_timings["search"] += dur_s

            entries = [e for e in search_info.get("entries", []) if e]
            invocations.append({
                "invocation": inv_idx1,
                "purpose": "candidate_search",
                "source": source,
                "start": t_s0 - t_start_all,
                "end": t_s1 - t_start_all,
                "duration": dur_s,
                "result": f"OK ({len(entries)} candidates)"
            })
            print(f"{req_tag}[YTDLP] invocation=#{inv_idx1} purpose='candidate_search' source='{source}' start={t_s0 - t_start_all:.2f}s end={t_s1 - t_start_all:.2f}s duration={dur_s:.2f}s result='OK ({len(entries)} candidates)'", flush=True)

            if not entries:
                raise ValueError("Трек не найден по данному запросу.")

            # 2. Интеллектуальный скоринг и ранжирование кандидатов
            t_c0 = time.perf_counter()
            def _candidate_penalty(e):
                dur = e.get("duration") or 0
                fmt_str = (str(e.get("formats", "")) + str(e.get("format_id", ""))).lower()
                is_prev = "preview" in fmt_str or (expected_duration and expected_duration > 60 and 0 < dur <= 35)
                penalty = 1000.0 if is_prev else 0.0

                if expected_duration:
                    diff = abs(dur - expected_duration)
                    if diff <= 15:
                        penalty += diff
                    elif diff <= 45:
                        penalty += 20.0 + diff
                    else:
                        penalty += 100.0 + diff
                else:
                    if dur >= 45:
                        penalty += 0.0
                    elif dur > 0:
                        penalty += 50.0 + (45 - dur)
                    else:
                        penalty += 80.0

                if source == "youtube":
                    uploader = str(e.get("uploader") or "")
                    channel = str(e.get("channel") or "")
                    is_topic = uploader.endswith("- Topic") or channel.endswith("- Topic") or " - Topic" in uploader or " - Topic" in channel
                    # На серверных/облачных IP YouTube Topic релизы часто блокируются бот-проверкой
                    if is_topic and not cookies_info.get("active"):
                        penalty += 60.0

                return penalty

            ranked_candidates = sorted(entries, key=_candidate_penalty)
            t_c1 = time.perf_counter()
            perf_timings["candidate_selection"] += (t_c1 - t_c0)

            # 3. Цикл скачивания лучших кандидатов (до 4 попыток)
            dl_opts = dict(options)
            dl_opts["extract_flat"] = False

            last_cand_error = None
            for cand_idx, selected_entry in enumerate(ranked_candidates[:4]):
                target_url = selected_entry.get("webpage_url") or selected_entry.get("url") or selected_entry.get("id")
                if target_url and not target_url.startswith("http") and "soundcloud" not in source:
                    target_url = f"https://www.youtube.com/watch?v={target_url}"
                if not target_url:
                    continue

                cand_title = selected_entry.get("title") or target_url
                cand_dur = selected_entry.get("duration") or 0
                print(f"{req_tag}[YTDLP] Попытка загрузки кандидата #{cand_idx+1}/{min(4, len(ranked_candidates))}: '{cand_title}' ({cand_dur}s) url='{target_url}'", flush=True)

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

                inv_idx2 = len(invocations) + 1
                t_d0 = time.perf_counter()
                try:
                    with yt_dlp.YoutubeDL(dl_opts) as ydl_dl:
                        res_info = ydl_dl.extract_info(target_url, download=True)
                    t_d1 = time.perf_counter()
                    dur_dl_all = t_d1 - t_d0

                    # Проверяем появление готового MP3 файла
                    mp3_files = list(output_dir.glob("*.mp3"))
                    if not mp3_files:
                        raise FileNotFoundError("Аудиофайл MP3 не был создан после обработки кандидата.")

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
                        "source": source,
                        "start": t_d0 - t_start_all,
                        "end": t_d1 - t_start_all,
                        "duration": dur_dl_all,
                        "result": "OK"
                    })
                    print(f"{req_tag}[YTDLP] invocation=#{inv_idx2} candidate=#{cand_idx+1} purpose='stream_download' source='{source}' duration={dur_dl_all:.2f}s result='OK'", flush=True)
                    return res_info
                except Exception as cand_err:
                    if cancel_event and cancel_event.is_set():
                        shutil.rmtree(output_dir, ignore_errors=True)
                        raise
                    last_cand_error = cand_err
                    print(f"{req_tag}[DOWNLOADER] Кандидат #{cand_idx+1} не удался ({cand_err}). Пробуем следующего...", flush=True)
                    # Очищаем неполные или временные файлы перед следующей попыткой
                    for temp_f in output_dir.iterdir():
                        if temp_f.is_file() and not temp_f.name.startswith("cover"):
                            try:
                                temp_f.unlink(missing_ok=True)
                            except Exception:
                                pass
                    continue

            if last_cand_error:
                raise last_cand_error
            raise ValueError("Ни один кандидат поиска не подошел для загрузки.")
        else:
            inv_idx = len(invocations) + 1
            t_d0 = time.perf_counter()
            dl_opts = dict(options)
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
        if "cookiefile" in ydl_opts and ("sign in" in err_msg or "bot" in err_msg or "cookie" in err_msg or "reload" in err_msg or "403" in err_msg):
            print(f"[DOWNLOADER] Сессия cookies недействительна ({extract_err}). Пробуем чистый запуск без cookies...", flush=True)
            ydl_opts_retry = dict(ydl_opts)
            ydl_opts_retry.pop("cookiefile", None)
            ydl_opts_retry["extractor_args"] = {
                "youtube": {
                    "player_client": ["android", "ios", "mweb", "web"],
                }
            }
            info = _execute_extraction(ydl_opts_retry)
        else:
            raise extract_err

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

    t_tag0 = time.perf_counter()
    _apply_custom_metadata(mp3_path, extracted_title, extracted_artist, thumbnail_path)
    perf_timings["tags"] = time.perf_counter() - t_tag0

    return DownloadedAudio(
        file_path=mp3_path,
        title=extracted_title,
        artist=extracted_artist,
        duration=duration,
        thumbnail_path=thumbnail_path,
        filesize=filesize,
        folder_path=output_dir,
        perf_timings=perf_timings,
        invocations=invocations
    )


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
        a = custom_artist
        a = re.sub(r'\s*-\s*(?:Topic|Тема)\b', '', a, flags=re.IGNORECASE)
        a = re.sub(r'[/\\:;*?"<>|]+', ' ', a)
        clean_artist = " ".join(a.split()).strip()

    # 2. Очистка названия
    clean_title = ""
    title_no_feat = ""
    if custom_title:
        t = custom_title
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
    request_id: Optional[str] = None
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
            cancel_event
        )

        # Если результат подозрительно короткий (< 35s), а ожидался полноценный трек (> 60s)
        if expected_duration and expected_duration > 60 and audio.duration <= 35:
            raise ValueError(f"Скачано превью ({audio.duration}s) вместо полного трека ({expected_duration}s)")

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
        is_bot_blocked = any(
            marker in str(primary_error).lower()
            for marker in ["confirm you’re not a bot", "confirm you're not a bot", "http error 429", "too many requests", "bot."]
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
                    cancel_event
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
        elif is_bot_blocked:
            print(f"{req_tag}[DOWNLOADER] Обнаружена блокировка YouTube IP (bot-check / 429). Пропускаем YouTube Search и сразу переходим к SoundCloud Fallback.", flush=True)

        # 2. Fallback в SoundCloud (выбирает полный трек среди нескольких вариантов запроса)
        if not query_or_url.startswith("scsearch"):
            for fb_q in fallback_queries:
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
                        cancel_event
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
