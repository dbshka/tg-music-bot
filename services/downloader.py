import asyncio
import concurrent.futures
import logging
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
from typing import Optional, Union, Dict, List, Tuple, Callable

import aiohttp
import sys
import yt_dlp

logger = logging.getLogger(__name__)

# Глобальный семафор ограничения одновременных загрузок для предотвращения OOM на ограниченных ресурсах (Render 512MB)
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# Лимиты времени на жизненный цикл кандидата и общий бюджет экстракции
CANDIDATE_DOWNLOAD_TIMEOUT = 12.0  # Максимальный бюджет времени на одного кандидата (включая попытку с cookies и без)
GLOBAL_EXTRACTION_TIMEOUT = 32.0   # Общий предельный бюджет на поиск и скачивание кандидатов
MIN_RETRY_TIME_REMAINING = 3.5     # Минимальный остаток времени для запуска повторной загрузки без cookies
GLOBAL_SC_FALLBACK_RESERVE = 4.0   # Резерв времени в конце глобального бюджета для аварийного SoundCloud fallback

_active_download_count = 0
_download_count_lock = threading.Lock()


class _DownloadCounter:
    """Контекстный менеджер безопасного отслеживания количества одновременных загрузок."""
    def __enter__(self):
        global _active_download_count
        with _download_count_lock:
            _active_download_count += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _active_download_count
        with _download_count_lock:
            _active_download_count = max(0, _active_download_count - 1)



def get_process_rss_mb(pid: Optional[int] = None) -> float:
    """Возвращает RSS память процесса в мегабайтах (MB)."""
    target_pid = pid if pid else "self"
    # 1. На Linux (Docker / Render) через нативный /proc/self/status (без внешних зависимостей)
    try:
        proc_file = f"/proc/{target_pid}/status"
        if os.path.exists(proc_file):
            with open(proc_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return round(float(parts[1]) / 1024.0, 2)
    except Exception:
        pass

    # 2. Через стандартный модуль Unix resource
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            return round(ru / 1024.0, 2)
        else:
            return round(ru / (1024.0 * 1024.0), 2)
    except Exception:
        pass

    # 3. Через psutil (если модуль установлен в окружении)
    try:
        import psutil
        proc = psutil.Process(pid or os.getpid())
        return round(proc.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        pass

    return 0.0


def log_memory_stage(
    stage: str,
    req_id: Optional[str] = None,
    source: Optional[str] = None,
    file_path: Optional[Union[str, Path]] = None,
    extra: Optional[str] = None
) -> None:
    """
    Логирует аккуратную диагностику RSS процесса перед/после каждого ключевого этапа
    без логирования секретов, токенов, cookies или чувствительных параметров.
    """
    pid = os.getpid()
    rss_mb = get_process_rss_mb(pid)
    with _download_count_lock:
        concurrent = _active_download_count

    file_size_str = "none"
    if file_path:
        try:
            p = Path(file_path)
            if p.exists() and p.is_file():
                size_bytes = p.stat().st_size
                file_size_str = f"{size_bytes}B ({size_bytes / (1024 * 1024):.2f}MB)"
        except Exception:
            pass

    msg = (
        f"MEMORY [{stage}] RSS={rss_mb}MB PID={pid} job_id={req_id or 'unknown'} "
        f"source={source or 'unknown'} concurrent={concurrent} file_size={file_size_str}"
    )
    if extra:
        msg += f" {extra}"
    print(msg, flush=True)
    logger.info(msg)


# Отключаем предупреждение yt-dlp об устаревании Python 3.10 в консоли
if 'yt_dlp.YoutubeDL' in sys.modules:
    sys.modules['yt_dlp.YoutubeDL']._get_system_deprecation = lambda: None

from PIL import Image, ImageOps
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
    validate_candidate_artist,
    is_artist_in_title_inversion,
    parse_query_artist_title,
    parse_speed_multiplier,
    TRACK_MODIFIERS,
    PERFORMANCE_MODIFIERS,
    DSP_SUPPORTED_MODIFIERS,
    SEMANTIC_MODIFIERS,
    is_candidate_matching_modifiers,
    SUPER_SLOWED_GROUP,
    SLOWED_GROUP
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
    source_title: Optional[str] = None
    source_modifiers: Optional[set] = None
    cover_path: Optional[Path] = None
    album: Optional[str] = None

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
    КРИТИЧЕСКОЕ ТРЕБОВАНИЕ: Программная модификация аудио через FFmpeg DSP полностью отключена.
    Все модификаторы (Slowed, Super Slowed, Sped Up, Reverb, Nightcore и др.) должны быть найдены
    в виде готового релиза.
    Если запрошен модификатор, а скачанный файл его не содержит, выбрасываем ValueError,
    не производя никакой модификации аудио через FFmpeg.
    """
    if not requested_modifiers:
        return 0

    if is_candidate_matching_modifiers(requested_modifiers, cand_modifiers):
        # Кандидат уже является готовой версией с запрошенным модификатором
        return 0

    # Кандидат не содержит запрошенную модификацию -> отказ от подмены
    raise ValueError("Возникла ошибка 44. Запрошенная версия трека не найдена.")


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
            ] + codec_args + ["-threads", "1", str(temp_out)]
            job_tag = req_tag.strip("[] ").replace("MUSIC", "").replace("request_id=", "").strip()
            log_memory_stage("before FFmpeg", req_id=job_tag, source="audio_modifier", file_path=audio_path)
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_memory_stage("after FFmpeg", req_id=job_tag, source="audio_modifier", file_path=temp_out)
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


def trim_letterbox_black_bars(im: Image.Image, threshold: int = 15) -> Image.Image:
    """
    Удаляет искусственные чёрные полосы леттербоксинга (например, 35-50px сверху и снизу
    в YouTube 4:3 превью hqdefault.jpg 480x360), чтобы обнажить активную область кадра.

    Защитные правила от ложных срабатываний:
    1. Квадратные (w <= h) и стандартные обложки релизов (Case B, C, D, E) НЕ обрезаются:
       Леттербоксинг возможен ТОЛЬКО в формате 4:3 (1.30 <= w / h <= 1.36).
       Форматы 1:1 (1000x1000), 5:4 (1000x800, w/h=1.25), 16:9 (1280x720) и вертикальные гарантированно защищены.
    2. Полосы должны быть СИММЕТРИЧНЫМИ сверху и снизу и составлять 8%-20% высоты кадра
       (стандарт размещения 16:9 видео внутри 4:3 кадра YouTube).
    """
    w, h = im.size
    if w <= h or w < 40 or h < 40:
        return im

    aspect = w / h
    # Леттербоксинг существует исключительно в 4:3 контейнерах (YouTube hqdefault/sddefault)
    if not (1.30 <= aspect <= 1.36):
        return im

    pix = im.load()
    max_scan = int(h * 0.20)
    min_bar = int(h * 0.08)

    # Сканируем верхнюю полосу
    top = 0
    for y in range(max_scan):
        if all(sum(pix[x, y]) <= threshold for x in range(0, w, max(1, w // 20))):
            top = y + 1
        else:
            break

    # Сканируем нижнюю полосу
    bottom = h
    for y in range(h - 1, h - max_scan, -1):
        if all(sum(pix[x, y]) <= threshold for x in range(0, w, max(1, w // 20))):
            bottom = y
        else:
            break

    bot_bar = h - bottom

    # Проверяем, что ОБЕ полосы присутствуют, примерно равны (симметрия) и лежат в диапазоне 8-20% высоты
    if min_bar <= top <= max_scan and min_bar <= bot_bar <= max_scan and abs(top - bot_bar) <= max(6, int(h * 0.03)):
        return im.crop((0, top, w, bottom))

    return im


def make_square_cover(img: Image.Image, max_side: Optional[int] = 320) -> Image.Image:
    """
    Приводит изображение к строго квадратному формату 1:1 по принципу:
    scale to COVER -> center crop -> квадрат.
    Гарантирует отсутствие искажения aspect ratio и искусственных черных рамок.

    Telegram Bot API для sendAudio допускает thumbnail шириной и высотой до 320px
    (JPEG, <200 KB).
    - Для квадратных студийных обложек (1000x1000): пропорциональный downscale до 320x320.
    - Для YouTube превью после очистки леттербоксинга (480x270):
      center-crop с сохранением пропорций 1:1 и безопасное приведение к целевому размеру 320x320
      без искажения/растяжения пропорций (uniform scale).
    """
    cleaned = trim_letterbox_black_bars(img)
    cw, ch = cleaned.size
    target_side = min(cw, ch)
    if max_side:
        if cw >= max_side or ch >= max_side:
            target_side = max_side
        elif target_side > max_side:
            target_side = max_side
    return ImageOps.fit(cleaned, (target_side, target_side), Image.Resampling.LANCZOS)


def _prepare_embedded_cover(raw_path: Path) -> Optional[Path]:
    """
    Подготавливает полноразмерную обложку для вшивания в ID3 APIC / MP4 covr:
    - Сохраняет максимальное разрешение оригинального арта (например 1000x1000, 1400x1400).
    - Для YouTube превью с леттербоксингом удаляет искусственные чёрные полосы и
      кадрирует в квадрат (center crop) БЕЗ сжатия до 320px.
    - Конвертирует в чистый JPEG максимального качества (quality=95).
    """
    if not raw_path or not raw_path.exists():
        return None
    try:
        target_path = raw_path.with_name(f"embedded_{raw_path.stem}.jpg")
        with Image.open(raw_path) as img:
            rgb_img = img.convert("RGB")
            # max_side=None: сохраняет полное исходное разрешение
            square_img = make_square_cover(rgb_img, max_side=None)
            square_img.save(target_path, "JPEG", quality=95)
        return target_path
    except Exception:
        return raw_path if raw_path.suffix.lower() in [".jpg", ".jpeg"] else None


def _convert_thumbnail_to_jpg(thumb_path: Path) -> Optional[Path]:
    """
    Конвертирует обложку в компактный JPEG с максимальным разрешением до 320x320
    специально для передачи в Telegram Bot API sendAudio (лимит <= 320x320, < 200 KB)
    с сохранением исходных пропорций через center crop.
    """
    if not thumb_path or not thumb_path.exists():
        return None
    try:
        target_path = thumb_path.with_name(f"thumb_{thumb_path.stem}.jpg")
        with Image.open(thumb_path) as img:
            rgb_img = img.convert("RGB")
            square_img = make_square_cover(rgb_img, max_side=320)
            square_img.save(target_path, "JPEG", quality=85)
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

        # Вшиваем полноразмерную обложку в тег ID3 APIC
        if cover_path and cover_path.exists():
            try:
                with open(cover_path, "rb") as albumart:
                    c_data = albumart.read()
                    mime_type = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
                    id3.delall("APIC")
                    id3.add(
                        APIC(
                            encoding=3,
                            mime=mime_type,
                            type=3,  # 3 is for album front cover
                            desc="Cover",
                            data=c_data
                        )
                    )
            except Exception:
                pass

        # Одиночный сброс на диск
        id3.save(audio_path, v2_version=3)
    except Exception:
        pass


def _cleanup_temp_candidate_files(output_dir: Path):
    """Безопасная очистка временных аудиофайлов кандидата, сохраняя обложку и бэкапы."""
    try:
        if not output_dir.exists():
            return
        for temp_f in list(output_dir.iterdir()):
            if temp_f.is_file() and not temp_f.name.startswith("cover") and not temp_f.name.startswith("backup_") and not temp_f.name.startswith("thumb_") and not temp_f.name.startswith("embedded_"):
                try:
                    temp_f.unlink(missing_ok=True)
                except Exception:
                    pass
            elif temp_f.is_dir() and (temp_f.name.startswith("cand_") or temp_f.name.startswith("sc_cand_")):
                shutil.rmtree(temp_f, ignore_errors=True)
    except Exception:
        pass


def _promote_candidate_assets(source_dir: Path, target_dir: Path, audio_file: Path) -> Path:
    """
    Продвигает аудиофайл и обложку победителя из изолированной директории кандидата в целевую директорию.
    Гарантирует, что обложки yt-dlp (*.webp, *.jpg, *.png) не теряются при удалении source_dir.
    """
    winner_path = target_dir / audio_file.name
    if winner_path.exists():
        winner_path.unlink(missing_ok=True)
    shutil.move(str(audio_file), str(winner_path))

    for img_pattern in ("*.webp", "*.jpg", "*.jpeg", "*.png"):
        for img_f in list(source_dir.glob(img_pattern)):
            target_img = target_dir / img_f.name
            if target_img.exists():
                target_img.unlink(missing_ok=True)
            try:
                shutil.move(str(img_f), str(target_img))
            except Exception:
                pass
            break

    shutil.rmtree(source_dir, ignore_errors=True)
    return winner_path


def _extract_info_with_timeout(
    ydl_opts: dict,
    url: str,
    timeout_sec: float,
    cand_cancel_event: threading.Event,
    parent_cancel_event: Optional[threading.Event] = None
) -> dict:
    """
    Выполняет вызов yt_dlp.YoutubeDL.extract_info(url, download=True) с жестким ограничением по времени.
    В случае превышения лимита времени или отмены пользователем, немедленно прерывает ожидание,
    устанавливает cand_cancel_event и завершает фоновый поток без блокировки вызывающего воркера.
    """
    if parent_cancel_event and parent_cancel_event.is_set():
        raise RuntimeError("Download cancelled by user")
    if timeout_sec <= 0:
        cand_cancel_event.set()
        raise TimeoutError("Candidate timeout budget exhausted before start")

    def _worker():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_worker)
        return future.result(timeout=timeout_sec)
    except concurrent.futures.TimeoutError:
        cand_cancel_event.set()
        raise TimeoutError(f"Candidate download exceeded timeout ({timeout_sec:.1f}s)")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _process_remote_cover_bytes(data: bytes, target_path: Path) -> tuple[Optional[Path], Optional[Path]]:
    """
    Атомарная синхронная обработка обложки из байтов в один проход в памяти:
    1. Декодирует изображение через PIL.
    2. Устраняет леттербоксинг (чёрные полосы) один раз.
    3. Создает полноразмерный квадратный JPEG (quality=95) для ID3 APIC / MP4 covr.
    4. Создает уменьшенную копию <=320x320 (quality=85) для Telegram sendAudio.
    """
    try:
        import io
        highres_path = target_path.with_name(f"embedded_{target_path.stem}.jpg")
        thumb_path = target_path.with_name(f"thumb_{target_path.stem}.jpg")
        with Image.open(io.BytesIO(data)) as img:
            rgb_img = img.convert("RGB")
            cleaned = trim_letterbox_black_bars(rgb_img)
            highres_img = make_square_cover(cleaned, max_side=None)
            highres_img.save(highres_path, "JPEG", quality=95)

            tg_img = make_square_cover(highres_img, max_side=320)
            tg_img.save(thumb_path, "JPEG", quality=85)
            return thumb_path, highres_path
    except Exception:
        raw_thumb = target_path.with_suffix(".raw_img")
        raw_thumb.write_bytes(data)
        highres_cover = _prepare_embedded_cover(raw_thumb)
        tg_thumb = _convert_thumbnail_to_jpg(highres_cover or raw_thumb)
        if raw_thumb.exists() and raw_thumb not in (highres_cover, tg_thumb):
            raw_thumb.unlink(missing_ok=True)
        return tg_thumb, highres_cover


async def _download_remote_thumbnail(url: str, target_path: Path) -> tuple[Optional[Path], Optional[Path]]:
    """
    Скачивает обложку по URL через shared session.
    Возвращает кортеж: (telegram_thumbnail_320, embedded_highres_cover).
    - telegram_thumbnail_320: JPEG <= 320x320 специально для отправки в Telegram sendAudio.
    - embedded_highres_cover: полноразмерный арт (1000x1000) для ID3 APIC / MP4 covr.
    """
    try:
        session = get_shared_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.read()
                return await asyncio.to_thread(_process_remote_cover_bytes, data, target_path)
    except Exception:
        pass
    return None, None




def is_candidate_download_eligible(
    candidate: dict,
    custom_artist: Optional[str] = None,
    custom_title: Optional[str] = None,
    core_title_words: Optional[set] = None,
    expected_duration: Optional[int] = None,
    requested_modifiers: Optional[set] = None,
    is_apple_music: bool = False,
    is_text_input: bool = False,
    clean_search: str = ""
) -> tuple[bool, Optional[str]]:
    """
    Дешёвая предварительная валидация кандидата по поисковым метаданным ДО вызова extract_info(download=True).
    Возвращает (True, None), если кандидат достаточно перспективен для загрузки или является сомнительным (AMBIGUOUS).
    Возвращает (False, reason), если кандидат заведомо не подходит (artist inversion, artist mismatch,
    unwanted modifier, title mismatch, duration mismatch).
    """
    e = candidate
    cand_title = e.get("_norm_title") or unicodedata.normalize("NFKC", e.get("title") or "").strip()
    cand_uploader = e.get("_norm_uploader") or unicodedata.normalize("NFKC", e.get("uploader") or "").strip()
    cand_channel = e.get("_norm_channel") or unicodedata.normalize("NFKC", e.get("channel") or "").strip()
    cand_artist = e.get("artist") or e.get("creator")

    if not custom_artist and not custom_title and clean_search:
        p_art, p_tit = parse_query_artist_title(clean_search)
        if p_art and p_tit:
            custom_artist = p_art
            custom_title = p_tit

    # 1. Проверка явной инверсии артиста в названии чужой песни (например "Joe Inferno - Tribal Church")
    if custom_artist and is_artist_in_title_inversion(
        expected_artist=custom_artist,
        candidate_title=cand_title,
        expected_title=custom_title,
        candidate_uploader=cand_uploader,
        candidate_channel=cand_channel
    ):
        return False, "artist inversion"

    # 2. Валидация артиста
    if custom_artist:
        is_art_valid = validate_candidate_artist(
            expected_artist=custom_artist,
            candidate_title=cand_title,
            candidate_uploader=cand_uploader,
            candidate_channel=cand_channel,
            expected_title=custom_title,
            candidate_artist=cand_artist
        )
        if not is_art_valid:
            # Кандидат не подтверждён как принадлежащий ожидаемому артисту.
            # Проверяем, является ли он 100% ДОКАЗАННЫМ чужим артистом (confirmed mismatch).
            # Если нет (лейбл, шоу, фанатский канал, название без артиста, uploader=None) —
            # кандидат считается AMBIGUOUS и допускается к загрузке для post-validation!
            is_confirmed_mismatch = False

            uploader_l = (cand_uploader or "").lower().strip()
            channel_l = (cand_channel or "").lower().strip()
            is_topic_channel = uploader_l.endswith("- topic") or channel_l.endswith("- topic") or " - topic" in uploader_l or " - topic" in channel_l
            is_universal_topic = (
                uploader_l in {"release - topic", "various artists - topic", "release", "various artists"} or
                channel_l in {"release - topic", "various artists - topic", "release", "various artists"}
            )
            if is_topic_channel and not is_universal_topic:
                # Topic-канал конкретного исполнителя гарантированно содержит треки ТОЛЬКО этого исполнителя
                is_confirmed_mismatch = True

            is_vevo = uploader_l.endswith("vevo") or channel_l.endswith("vevo") or "vevo" in uploader_l or "vevo" in channel_l
            if is_vevo and not validate_artist_match(custom_artist, cand_uploader) and not validate_artist_match(custom_artist, cand_channel):
                is_confirmed_mismatch = True

            if not is_confirmed_mismatch and cand_title:
                cand_art_part, cand_tit_part = parse_query_artist_title(cand_title)
                if cand_art_part and cand_tit_part:
                    if not validate_artist_match(custom_artist, cand_art_part):
                        is_left_title = False
                        if custom_title:
                            core_title = extract_core_title_words(custom_title, custom_artist)
                            if core_title and compute_title_match_ratio(cand_art_part, core_title) >= 0.5:
                                is_left_title = True
                        if not is_left_title:
                            if custom_title:
                                core_title = extract_core_title_words(custom_title, custom_artist)
                                if core_title and compute_title_match_ratio(cand_tit_part, core_title) >= 0.5:
                                    descriptors = {"official", "video", "audio", "lyrics", "lyric", "hd", "hq", "remaster", "remastered", "clean", "explicit", "visualizer"}
                                    art_words = set(re.findall(r'[\w]+', cand_art_part.lower()))
                                    if art_words - descriptors:
                                        is_confirmed_mismatch = True

            if not is_confirmed_mismatch and cand_artist:
                if not validate_artist_match(custom_artist, cand_artist):
                    is_confirmed_mismatch = True

            if is_confirmed_mismatch:
                return False, "artist mismatch"

    # 3. Совпадение ключевых слов названия трека
    if not core_title_words and custom_title:
        core_title_words = extract_core_title_words(custom_title, custom_artist)

    if core_title_words:
        ratio = compute_title_match_ratio(cand_title, core_title_words)
        if ratio < 0.25:
            return False, f"title mismatch ({ratio:.2f} < 0.25)"

    # 4. Проверка модификаторов (Live, Acoustic, Remix, Slowed, etc.)
    ignore_cand_words = (core_title_words or set()) | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())
    cand_text = f"{cand_title} {cand_uploader} {cand_channel}"
    cand_modifiers = extract_modifiers(cand_text, ignore_words=ignore_cand_words)

    disqualifying_unrequested = {
        "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix", "club mix", "dance mix",
        "live", "лайв", "концерт", "performance", "cover", "кавер", "acoustic", "акустика",
        "drum edit", "drums", "dnb", "драмка", "с драмкой",
        "slowed", "slow", "sped up", "speed up", "nightcore", "daycore",
        "instrumental", "инструментал", "minus", "минус", "karaoke", "караоке",
        "chopped and screwed", "low pitch", "high pitch", "bass boost", "8d", "16d"
    }

    if not requested_modifiers:
        if (is_apple_music or is_text_input) and cand_modifiers:
            bad_mods = cand_modifiers & disqualifying_unrequested
            if not bad_mods:
                harmless = {"remaster", "remastered", "clean", "explicit"}
                bad_mods = cand_modifiers - harmless
            if bad_mods:
                return False, f"modifier={','.join(sorted(bad_mods))}"
    else:
        unrequested_mods = cand_modifiers - requested_modifiers
        conflicting_mods = unrequested_mods & disqualifying_unrequested
        if conflicting_mods:
            return False, f"conflicting modifier={','.join(sorted(conflicting_mods))}"
        if not is_candidate_matching_modifiers(requested_modifiers, cand_modifiers):
            return False, f"missing modifier={','.join(sorted(requested_modifiers))}"

    # 5. Грубая проверка длительности по поисковым метаданным
    cand_dur = int(e.get("duration") or 0)
    if expected_duration and expected_duration > 35 and cand_dur > 0:
        dur_diff = abs(cand_dur - expected_duration)
        max_pre_diff = 60 if not requested_modifiers else 90
        if dur_diff > max_pre_diff:
            return False, f"duration mismatch (diff={dur_diff}s > {max_pre_diff}s)"

    return True, None


def _is_candidate_promising(
    candidate: dict,
    custom_artist: Optional[str] = None,
    custom_title: Optional[str] = None,
    core_title_words: Optional[set] = None,
    clean_search: str = "",
    requested_modifiers: Optional[set] = None,
    is_apple_music: bool = False,
    is_text_input: bool = False
) -> bool:
    """
    Быстрая и легковесная проверка: является ли YouTube-кандидат перспективным.
    Используется для обоснованного сокращения ожидания медленного SoundCloud (grace period).
    """
    eligible, _ = is_candidate_download_eligible(
        candidate=candidate,
        custom_artist=custom_artist,
        custom_title=custom_title,
        core_title_words=core_title_words,
        requested_modifiers=requested_modifiers,
        is_apple_music=is_apple_music,
        is_text_input=is_text_input,
        clean_search=clean_search
    )
    return eligible


def is_valid_topic_channel(cand_uploader: str, cand_channel: str, custom_artist: Optional[str] = None) -> bool:
    """Проверяет, является ли канал официальным Topic-каналом артиста или релизом дистрибьютора."""
    u_low = (cand_uploader or "").lower()
    c_low = (cand_channel or "").lower()
    is_topic_name = (
        u_low.endswith("- topic")
        or c_low.endswith("- topic")
        or " - topic" in u_low
        or " - topic" in c_low
    )
    if not is_topic_name:
        return False
    if not custom_artist:
        return True
    if validate_artist_match(custom_artist, cand_uploader) or validate_artist_match(custom_artist, cand_channel):
        return True
    if any(v in u_low or v in c_low for v in ["various artists", "release"]):
        return True
    return False


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
    """Вычисляет штрафные баллы для ранжирования кандидата с мемоизацией результатов."""
    e = candidate

    cand_title = e.get("_norm_title")
    if cand_title is None:
        cand_title = unicodedata.normalize("NFKC", e.get("title") or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
        e["_norm_title"] = cand_title

    cand_uploader = e.get("_norm_uploader")
    if cand_uploader is None:
        cand_uploader = unicodedata.normalize("NFKC", e.get("uploader") or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
        e["_norm_uploader"] = cand_uploader

    cand_channel = e.get("_norm_channel")
    if cand_channel is None:
        cand_channel = unicodedata.normalize("NFKC", e.get("channel") or "").lower().replace("’", "'").replace("‘", "'").replace("`", "'")
        e["_norm_channel"] = cand_channel

    dur = e.get("duration") or 0
    fmt_str = (str(e.get("formats", "")) + str(e.get("format_id", ""))).lower()
    is_prev = "preview" in fmt_str or (expected_duration and expected_duration > 50 and 0 < dur <= 35)
    if is_prev:
        e["_penalty"] = 5000.0
        return 5000.0

    penalty = 0.0
    cand_text = f"{cand_title} {cand_uploader} {cand_channel}"

    # Если артист и название не переданы явно, но есть поисковый запрос (например "Tribal Church - Pt.02"):
    if not custom_artist and not custom_title and clean_search:
        p_art, p_tit = parse_query_artist_title(clean_search)
        if p_art and p_tit:
            custom_artist = p_art
            custom_title = p_tit

    # 0. Строгая валидация исполнителя (Artist Validation)
    is_cand_art_valid = True
    is_inv = False
    if custom_artist:
        is_cand_art_valid = validate_candidate_artist(
            expected_artist=custom_artist,
            candidate_title=cand_title,
            candidate_uploader=cand_uploader,
            candidate_channel=cand_channel,
            expected_title=custom_title,
            candidate_artist=e.get("artist") or e.get("creator")
        )
        if not is_cand_art_valid:
            is_inv = is_artist_in_title_inversion(
                expected_artist=custom_artist,
                candidate_title=cand_title,
                expected_title=custom_title,
                candidate_uploader=cand_uploader,
                candidate_channel=cand_channel
            )
            if is_inv:
                # Исполнитель из запроса оказался названием песни другого артиста (например Joe Inferno - Tribal Church)
                penalty += 20000.0
            else:
                # Чужой исполнитель не совпадает
                penalty += 12000.0
        else:
            penalty -= 80.0
    e["_is_art_valid"] = is_cand_art_valid
    e["_is_inversion"] = is_inv

    # 1. Семантическое соответствие названия трека (Core Title Matching)
    core_title_words = extract_core_title_words(custom_title, artist=custom_artist) if custom_title else set()
    match_ratio = 1.0
    if core_title_words:
        match_ratio = compute_title_match_ratio(cand_title, core_title_words)
        if match_ratio >= 0.8:
            penalty -= 120.0
        elif match_ratio >= 0.5:
            penalty -= 30.0
        elif match_ratio >= 0.25:
            penalty += 6000.0
        else:
            # 0% совпадение названия трека: чужая песня другого названия!
            penalty += 15000.0
    e["_title_ratio"] = match_ratio

    ignore_cand_words = core_title_words | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())
    cand_modifiers = extract_modifiers(cand_text, ignore_words=ignore_cand_words)
    e["_cand_modifiers"] = cand_modifiers
    req_mods = requested_modifiers or set()

    # Деприоритет очевидных DJ-сетов, подкастов и длинных миксов для одиночных треков
    cand_lower = cand_text.lower()
    if not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "set", "сет", "podcast", "подкаст", "1 hour", "час"]):
        if any(k in cand_lower for k in ["dj set", "continuous mix", "podcast", "подкаст", "full set", "radio show"]):
            penalty += 5000.0

    cookies_info = get_cookies_info()

    if is_apple_music:
        if req_mods:
            if is_candidate_matching_modifiers(req_mods, cand_modifiers):
                matching = req_mods & cand_modifiers
                penalty -= 800.0 * max(1, len(matching))
                if (req_mods & SUPER_SLOWED_GROUP) and (cand_modifiers & SUPER_SLOWED_GROUP):
                    penalty -= 500.0
            else:
                penalty += 8000.0
        else:
            if cand_modifiers:
                penalty += 5000.0

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
            if dur > 1800 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час", "podcast", "сет", "set"]):
                penalty += 8000.0
            elif dur > 1200 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час", "podcast", "сет", "set"]):
                penalty += 4500.0
            elif dur > 900 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                penalty += 2500.0
            elif dur >= 45:
                penalty += 0.0
            else:
                penalty += 100.0 + (45 - dur) * 10.0

        cand_src = e.get("_source") or source
        if cand_src == "youtube":
            is_topic = is_valid_topic_channel(cand_uploader, cand_channel, custom_artist)
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
            if is_candidate_matching_modifiers(req_mods, cand_modifiers):
                matching = req_mods & cand_modifiers
                unrequested = cand_modifiers - req_mods
                if req_mods & SUPER_SLOWED_GROUP:
                    unrequested -= SLOWED_GROUP
                penalty -= 800.0 * max(1, len(matching))
                if (req_mods & SUPER_SLOWED_GROUP) and (cand_modifiers & SUPER_SLOWED_GROUP):
                    penalty -= 500.0
                if unrequested:
                    penalty += 300.0 * len(unrequested)
            else:
                penalty += 8000.0
        else:
            if cand_modifiers:
                penalty += 5000.0

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
            if dur > 1800 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час", "podcast", "сет", "set"]):
                penalty += 8000.0
            elif dur > 1200 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час", "podcast", "сет", "set"]):
                penalty += 4500.0
            elif dur > 900 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                penalty += 2500.0
            elif dur >= 45:
                penalty += 0.0
            else:
                penalty += 100.0 + (45 - dur) * 10.0

        cand_src = e.get("_source") or source
        if cand_src == "youtube":
            is_topic = is_valid_topic_channel(cand_uploader, cand_channel, custom_artist)
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
            if is_candidate_matching_modifiers(req_mods, cand_modifiers):
                matching = req_mods & cand_modifiers
                penalty -= 800.0 * max(1, len(matching))
                if (req_mods & SUPER_SLOWED_GROUP) and (cand_modifiers & SUPER_SLOWED_GROUP):
                    penalty -= 500.0
            else:
                penalty += 8000.0
        else:
            if cand_modifiers:
                penalty += 5000.0

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
            if dur > 1800 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час", "podcast", "сет", "set"]):
                penalty += 8000.0
            elif dur > 1200 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час", "podcast", "сет", "set"]):
                penalty += 4500.0
            elif dur > 900 and not any(k in clean_search.lower() for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                penalty += 2500.0
            elif dur >= 45:
                penalty += 0.0
            else:
                penalty += 100.0 + (45 - dur) * 10.0

        cand_src = e.get("_source") or source
        if cand_src == "youtube":
            is_topic = is_valid_topic_channel(cand_uploader, cand_channel, custom_artist)
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

    e["_penalty"] = penalty
    return penalty


def _find_or_convert_candidate_audio(
    output_dir: Path,
    cand_idx: int = 0,
    cand_title: str = "",
    req_tag: str = ""
) -> list[Path]:
    """
    Находит скачанный аудиофайл в директории кандидата.
    Если yt-dlp сохранил альтернативный поток (.webm, .opus, .ogg, .flac, .wav),
    выполняет аварийную перекодировку в .m4a через FFmpeg (-threads 1 -vn).
    """
    audio_files = [
        f for f in output_dir.iterdir()
        if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"] and not f.name.startswith("backup_")
    ]
    if audio_files:
        if len(audio_files) > 1:
            audio_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return audio_files

    alt_audio = [
        f for f in output_dir.iterdir()
        if f.is_file() and f.suffix.lower() in [".webm", ".opus", ".ogg", ".flac", ".wav"] and not f.name.startswith("backup_")
    ]
    if alt_audio:
        cand_label = f"Кандидат #{cand_idx+1}: " if cand_title else ""
        print(f"{req_tag}[DOWNLOADER] {cand_label}обнаружен альтернативный аудиопоток {alt_audio[0].name}. Запускаем конвертацию в M4A (-threads 1)...", flush=True)
        emergency_out = output_dir / f"{alt_audio[0].stem}.m4a"
        try:
            import subprocess
            cmd = ["ffmpeg", "-y", "-i", str(alt_audio[0]), "-c:a", "aac", "-b:a", "192k", "-threads", "1", "-vn", str(emergency_out)]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and emergency_out.exists() and emergency_out.stat().st_size > 1000:
                alt_audio[0].unlink(missing_ok=True)
                print(f"{req_tag}[DOWNLOADER] {cand_label}успешная конвертация в {emergency_out.name} ({emergency_out.stat().st_size} байт)", flush=True)
                return [emergency_out]
        except Exception as em_err:
            print(f"{req_tag}[DOWNLOADER] {cand_label}конвертация не удалась: {em_err}", flush=True)

    return []


def _track_download_concurrency(func):
    """Декоратор для безопасного инкремента/декремента счетчика активных задач загрузки."""
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _DownloadCounter():
            return func(*args, **kwargs)
    return wrapper


@_track_download_concurrency
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
    is_text_input: bool = False,
    requested_variant: Optional[str] = None,
    custom_album: Optional[str] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None
) -> DownloadedAudio:
    """Синхронный процесс ускоренной загрузки и конвертации через yt-dlp."""
    req_tag = f"[MUSIC][request_id={request_id}] " if request_id else ""
    outtmpl = str(output_dir / "%(title).100B.%(ext)s")

    cookies_info = get_cookies_info()

    ydl_opts = {
        # Максимальный допустимый размер файла для предотвращения переполнения диска
        "max_filesize": MAX_FILE_SIZE_BYTES,
        # Приоритет отдаем прямому M4A (AAC) аудиопотоку: без долгой перекодировки FFmpeg в MP3 (-3..5 сек)
        # Исключительно чистые аудиопотоки! Скачивание видеодорожек строго запрещено.
        "format": "ba[ext=m4a]/ba[ext=mp3]/ba",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "writethumbnail": not skip_thumbnail,
        "quiet": True,
        "no_warnings": True,
        # Ультра-быстрая сеть: увеличенный буфер и последовательная загрузка фрагментов потока
        "buffersize": 256 * 1024,
        "concurrent_fragment_downloads": 1,
        "socket_timeout": 5,
        "retries": 1,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
        # Энергоэффективная конфигурация: строго 1 поток для исключения OOM на многоядерных хостах Render (512 MB)
        "postprocessor_args": {
            "FFmpegExtractAudio": [
                "-threads", "1",
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
        print(f"{req_tag}[DOWNLOADER] Быстрый режим с cookies: {cookies_info['path']}", flush=True)
    elif is_youtube:
        print(f"{req_tag}[DOWNLOADER] Режим без cookies (клиенты по умолчанию)", flush=True)


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
        nonlocal custom_artist, custom_title
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

            clean_artist_str = (custom_artist or "").strip()
            clean_title_str = (custom_title or "").strip()
            if not clean_artist_str and not clean_title_str and clean_search:
                p_art, p_tit = parse_query_artist_title(clean_search)
                if p_art and p_tit:
                    clean_artist_str = p_art
                    clean_title_str = p_tit
                    custom_artist = p_art
                    custom_title = p_tit

            # Анализируем, запрашивал ли пользователь явно модификаторы (slowed, sped up, remix, cover и т.д.)
            req_context = f"{custom_artist or ''} {custom_title or ''} {clean_search} {requested_variant or ''}".lower()
            requested_modifiers = extract_modifiers(req_context) or set()

            # Ключевые слова названия трека для семантической проверки
            core_title_words = extract_core_title_words(custom_title or clean_search, custom_artist)
            if not core_title_words and custom_title:
                core_title_words = set(re.findall(r'[\w]+', custom_title.lower()))

            clean_q_simple = f"{clean_artist_str} {clean_title_str}".strip() if (clean_artist_str and clean_title_str) else clean_search
            ignore_cand_words = core_title_words | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())


            try:
                from services.search import register_ytmsearch_extractor
                register_ytmsearch_extractor()
            except Exception:
                pass

            import concurrent.futures
            if is_apple_music or is_text_input:
                if requested_modifiers:
                    mod_term = requested_variant if (requested_variant and requested_variant != "original") else " ".join(sorted(requested_modifiers))
                    variant_query = clean_q_simple
                    if mod_term.lower() not in clean_q_simple.lower():
                        variant_query = f"{clean_q_simple} {mod_term}".strip()

                    search_tasks = [
                        ("youtube", f"ytsearch6:{variant_query}"),
                        ("youtube", f"ytsearch4:{clean_artist_str} - {clean_title_str} ({mod_term})".strip()),
                        ("soundcloud", f"scsearch5:{variant_query}")
                    ]
                else:
                    search_tasks = [
                        ("youtube", f"ytmsearch5:{clean_q_simple}"),
                        ("youtube", f"ytsearch8:{clean_q_simple}"),
                        ("soundcloud", f"scsearch5:{clean_q_simple}")
                    ]
                    if clean_search and clean_search != clean_q_simple:
                        search_tasks.append(("youtube", f"ytsearch3:{clean_search}"))
            else:
                if requested_modifiers:
                    mod_term = requested_variant if (requested_variant and requested_variant != "original") else " ".join(sorted(requested_modifiers))
                    variant_query = clean_q_simple
                    if mod_term.lower() not in clean_q_simple.lower():
                        variant_query = f"{clean_q_simple} {mod_term}".strip()
                    search_tasks = [
                        ("youtube", f"ytsearch6:{variant_query}"),
                        ("soundcloud", f"scsearch5:{variant_query}")
                    ]
                else:
                    search_tasks = [
                        ("youtube", f"ytmsearch5:{clean_q_simple}"),
                        ("youtube", f"ytsearch8:{clean_q_simple}"),
                        ("soundcloud", f"scsearch5:{clean_q_simple}")
                    ]

            clean_core_title = " ".join(core_title_words) if core_title_words else ""
            if clean_core_title and custom_artist and requested_modifiers:
                mod_term = requested_variant if (requested_variant and requested_variant != "original") else " ".join(sorted(requested_modifiers))
                search_tasks.append(("soundcloud", f"scsearch3:{custom_artist} {clean_core_title} {mod_term}"))

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(search_tasks))
            try:
                futures = {executor.submit(_fetch_candidates, q, src): src for src, q in search_tasks}
                yt_futures = {f for f, src in futures.items() if src == "youtube"}

                t_deadline = time.perf_counter() + 7.0
                while futures and time.perf_counter() < t_deadline:
                    remaining_time = max(0.05, t_deadline - time.perf_counter())
                    done_batch, _ = concurrent.futures.wait(
                        list(futures.keys()),
                        timeout=min(0.15, remaining_time),
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for f in done_batch:
                        futures.pop(f, None)
                        try:
                            res = f.result()
                            if res:
                                entries.extend(res)
                        except Exception:
                            pass

                    # Адаптивное завершение: если все YouTube-задачи завершились и вернули хотя бы одного
                    # действительно перспективного кандидата, даем медленным SoundCloud-задачам льготный лимит 0.8с.
                    # Если все кандидаты YouTube мусорные/сомнительные (инверсии, несовпадение названия/артиста), НЕ обрываем SoundCloud на 0.8с.
                    has_promising_yt = any(
                        e.get("_source") == "youtube" and _is_candidate_promising(
                            e,
                            custom_artist=custom_artist,
                            custom_title=custom_title,
                            core_title_words=core_title_words,
                            clean_search=clean_search,
                            requested_modifiers=requested_modifiers,
                            is_apple_music=is_apple_music,
                            is_text_input=is_text_input
                        )
                        for e in entries
                    )
                    all_yt_done = not any(f in futures for f in yt_futures)
                    if all_yt_done and has_promising_yt:
                        if t_deadline > time.perf_counter() + 0.8:
                            t_deadline = time.perf_counter() + 0.8
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            # Если параллельный опрос не вернул ни одного YouTube-кандидата,
            # выполняем чистый последовательный fallback-запрос к YouTube без cookies:
            # 1. "{artist} - {title}"
            # 2. "{title} {artist}"
            # 3. "{artist} {title}"
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
                        fb_queries = []
                        if clean_search:
                            fb_queries.append(clean_search)
                        if clean_title_str and clean_artist_str:
                            inv_fb = f"{clean_title_str} {clean_artist_str}".strip()
                            if inv_fb not in fb_queries:
                                fb_queries.append(inv_fb)
                        if clean_q_simple and clean_q_simple not in fb_queries:
                            fb_queries.append(clean_q_simple)

                        for fb_q in fb_queries:
                            info_yt = ydl_yt_fb.extract_info(f"ytsearch5:{fb_q}", download=False)
                            fb_items = [e for e in (info_yt.get("entries") or []) if e]
                            if fb_items:
                                for fe in fb_items:
                                    fe["_source"] = "youtube"
                                entries.extend(fb_items)
                                print(f"{req_tag}[SEARCH] YouTube standalone fallback '{fb_q}' вернул {len(fb_items)} кандидатов.", flush=True)
                                break
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

            # 2. Интеллектуальный Query-Aware скоринг кандидатов
            has_usable_candidate = False
            last_cand_error = None
            youtube_blocked = False
            best_fallback_info = None

            if not entries:
                ranked_candidates = []
            else:
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
            t_global_deadline = t_start_all + GLOBAL_EXTRACTION_TIMEOUT
            for cand_idx, selected_entry in enumerate(ranked_candidates):
                cand_source = selected_entry.get("_source") or source
                if youtube_blocked and cand_source == "youtube":
                    continue

                t_now = time.perf_counter()
                if t_now >= t_global_deadline - GLOBAL_SC_FALLBACK_RESERVE:
                    print(f"{req_tag}[YTDLP] Исчерпан общий бюджет времени экстракции ({t_global_deadline - t_now:.1f}s осталось); прерываем перебор кандидатов.", flush=True)
                    break

                target_url = selected_entry.get("webpage_url") or selected_entry.get("url") or selected_entry.get("id")
                if target_url and not target_url.startswith("http") and "soundcloud" not in cand_source:
                    target_url = f"https://www.youtube.com/watch?v={target_url}"
                if not target_url:
                    continue

                cand_title = selected_entry.get("title") or target_url
                cand_dur = selected_entry.get("duration") or 0

                # 0) Предварительная дешёвая валидация кандидата ДО запуска загрузки (Pre-download validation):
                is_eligible, reject_reason = is_candidate_download_eligible(
                    candidate=selected_entry,
                    custom_artist=custom_artist,
                    custom_title=custom_title,
                    core_title_words=core_title_words,
                    expected_duration=expected_duration,
                    requested_modifiers=requested_modifiers,
                    is_apple_music=is_apple_music,
                    is_text_input=is_text_input,
                    clean_search=clean_search
                )
                if not is_eligible:
                    print(f"{req_tag}[YTDLP] Candidate #{cand_idx+1} pre-validation rejected: {reject_reason}", flush=True)
                    continue

                try:
                    print(f"{req_tag}[YTDLP] Попытка загрузки кандидата #{cand_idx+1} ({cand_source}): '{cand_title}' ({cand_dur}s) url='{target_url}'", flush=True)
                except UnicodeEncodeError:
                    safe_cand_title = cand_title.encode("ascii", errors="replace").decode("ascii")
                    print(f"{req_tag}[YTDLP] Попытка загрузки кандидата #{cand_idx+1} ({cand_source}): '{safe_cand_title}' ({cand_dur}s) url='{target_url}'", flush=True)


                cand_token = uuid.uuid4().hex[:6]
                cand_dir = output_dir / f"cand_{cand_idx}_{cand_token}"
                cand_dir.mkdir(parents=True, exist_ok=True)
                cand_dl_opts = dict(options)
                cand_dl_opts["extract_flat"] = False
                cand_dl_opts["outtmpl"] = str(cand_dir / "%(title).100B.%(ext)s")
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

                t_cand_start = time.perf_counter()
                remaining_global = max(0.5, t_global_deadline - t_cand_start - GLOBAL_SC_FALLBACK_RESERVE)
                cand_budget = min(CANDIDATE_DOWNLOAD_TIMEOUT, remaining_global)
                cand_deadline = t_cand_start + cand_budget
                cand_cancel_event = threading.Event()

                hook_times = {"dl_start": 0, "dl_end": 0, "pp_start": 0, "pp_end": 0}
                def p_hook(d):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Download cancelled by user")
                    if cand_cancel_event.is_set() or time.perf_counter() > cand_deadline:
                        cand_cancel_event.set()
                        raise TimeoutError(f"Candidate #{cand_idx+1} exceeded candidate deadline")
                    if d.get("status") == "downloading":
                        if not hook_times["dl_start"]:
                            hook_times["dl_start"] = time.perf_counter()
                        if progress_callback:
                            total = d.get("total_bytes") or d.get("total_bytes_estimate")
                            downloaded = d.get("downloaded_bytes") or 0
                            if total and total > 0:
                                pct = max(0, min(100, int(downloaded / total * 100)))
                                try:
                                    progress_callback("downloading", pct)
                                except Exception:
                                    pass
                    elif d.get("status") == "finished":
                        hook_times["dl_end"] = time.perf_counter()
                        if progress_callback:
                            try:
                                progress_callback("downloading", 100)
                            except Exception:
                                pass

                def pp_hook(d):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Download cancelled by user")
                    if cand_cancel_event.is_set() or time.perf_counter() > cand_deadline:
                        cand_cancel_event.set()
                        raise TimeoutError(f"Candidate #{cand_idx+1} exceeded candidate deadline")
                    if d.get("status") == "started":
                        if not hook_times["pp_start"]:
                            hook_times["pp_start"] = time.perf_counter()
                        if progress_callback:
                            try:
                                progress_callback("processing", 100)
                            except Exception:
                                pass
                    elif d.get("status") == "finished":
                        hook_times["pp_end"] = time.perf_counter()

                cand_dl_opts["progress_hooks"] = [p_hook]
                cand_dl_opts["postprocessor_hooks"] = [pp_hook]

                ignore_cand_words = core_title_words | (set(re.findall(r'[\w]+', custom_artist.lower())) if custom_artist else set())

                inv_idx2 = len(invocations) + 1
                t_d0 = time.perf_counter()
                try:
                    t_cand_remain = max(0.1, cand_deadline - time.perf_counter())
                    res_info = _extract_info_with_timeout(
                        ydl_opts=cand_dl_opts,
                        url=target_url,
                        timeout_sec=t_cand_remain,
                        cand_cancel_event=cand_cancel_event,
                        parent_cancel_event=cancel_event
                    )
                    t_d1 = time.perf_counter()
                    dur_dl_all = t_d1 - t_d0

                    # Проверяем появление готового аудиофайла в изолированной директории кандидата
                    audio_files = _find_or_convert_candidate_audio(cand_dir, cand_idx, cand_title, req_tag=req_tag)
                    if not audio_files and output_dir.exists():
                        direct_audio = _find_or_convert_candidate_audio(output_dir, cand_idx, cand_title, req_tag=req_tag)
                        if direct_audio:
                            for df in direct_audio:
                                target_p = cand_dir / df.name
                                shutil.move(str(df), str(target_p))
                            audio_files = _find_or_convert_candidate_audio(cand_dir, cand_idx, cand_title, req_tag=req_tag)

                    if not audio_files:
                        existing_files = [f.name for f in cand_dir.iterdir() if f.is_file() and not f.name.startswith("backup_")]
                        is_likely_filesize = bool(cand_dur and cand_dur > 1200)
                        print(
                            f"{req_tag}[DOWNLOADER] Кандидат #{cand_idx+1} '{cand_title}' ({cand_dur}s) не создал аудиопоток "
                            f"(файлы в cand_dir: {existing_files}, max_filesize_abort={is_likely_filesize}). Пропускаем кандидата.",
                            flush=True
                        )
                        shutil.rmtree(cand_dir, ignore_errors=True)
                        _cleanup_temp_candidate_files(output_dir)
                        last_cand_error = ValueError(f"Кандидат #{cand_idx+1} '{cand_title}' не предоставил доступного аудиопотока.")
                        continue

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
                        shutil.rmtree(cand_dir, ignore_errors=True)
                        _cleanup_temp_candidate_files(output_dir)
                        continue

                    # 1b. Жесткая валидация исполнителя: кандидат должен принадлежать ожидаемому артисту!
                    cand_uploader_val = res_info.get("uploader") or selected_entry.get("uploader")
                    cand_channel_val = res_info.get("channel") or selected_entry.get("channel")
                    cand_artist_val = res_info.get("artist") or selected_entry.get("artist") or res_info.get("creator") or selected_entry.get("creator")
                    if custom_artist and not validate_candidate_artist(
                        expected_artist=custom_artist,
                        candidate_title=cand_entry_title,
                        candidate_uploader=cand_uploader_val,
                        candidate_channel=cand_channel_val,
                        expected_title=custom_title,
                        candidate_artist=cand_artist_val
                    ):
                        print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' не принадлежит исполнителю '{custom_artist}'. Отклоняем как неаутентичный.", flush=True)
                        shutil.rmtree(cand_dir, ignore_errors=True)
                        _cleanup_temp_candidate_files(output_dir)
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
                                shutil.rmtree(cand_dir, ignore_errors=True)
                                _cleanup_temp_candidate_files(output_dir)
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
                                shutil.rmtree(cand_dir, ignore_errors=True)
                                _cleanup_temp_candidate_files(output_dir)
                                continue

                            # Если запрошен любой модификатор (slowed, super slowed, sped up, reverb, acoustic и т.д.),
                            # а кандидат его НЕ удовлетворяет — отклоняем, чтобы не отдать обычный студийный трек!
                            if requested_modifiers and not is_candidate_matching_modifiers(requested_modifiers, cand_modifiers):
                                print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_entry_title}' не удовлетворяет запрошенным модификаторам {requested_modifiers} (кандидат: {cand_modifiers}). Отклоняем.", flush=True)
                                shutil.rmtree(cand_dir, ignore_errors=True)
                                _cleanup_temp_candidate_files(output_dir)
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
                    elif requested_modifiers and is_candidate_matching_modifiers(requested_modifiers, cand_modifiers):
                        # Для готовых релизов с модификациями (slowed, super slowed, sped up, live и др.)
                        # хронометраж закономерно отличается от студийного оригинала, поэтому принимаем готовый релиз
                        is_duration_acceptable = True
                    elif is_apple_music or is_text_input:
                        # Строгий допуск для студийного оригинала:
                        # Для верифицированных официальных релизов допускаем до 2% (макс 7с).
                        # Для неофициальных/сомнительных источников оставляем строгий лимит 4 секунды.
                        cand_uploader_l = (res_info.get("uploader") or selected_entry.get("uploader") or "").lower()
                        cand_channel_l = (res_info.get("channel") or selected_entry.get("channel") or "").lower()
                        is_official_high_confidence = (
                            is_valid_topic_channel(cand_uploader_l, cand_channel_l, custom_artist) or
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
                        # Продвигаем победившую аудиодорожку и обложку в output_dir:
                        winner_path = _promote_candidate_assets(cand_dir, output_dir, audio_files[0])
                        audio_files = [winner_path]

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
                        has_usable_candidate = True
                        res_info["_source_title"] = cand_entry_title or cand_title
                        res_info["_source_modifiers"] = cand_modifiers
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
                    if requested_modifiers:
                        has_required_mods = is_candidate_matching_modifiers(
                            requested_modifiers,
                            cand_modifiers
                        )
                    if (cand_match_ratio >= 0.5 or not core_title_words) and has_required_mods and (requested_modifiers or not cand_modifiers) and diff <= max_backup_diff:
                        print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_title}' имеет допустимую резервную разницу длительности {diff}s (<= {max_backup_diff}s). Сохраняем как резерв.", flush=True)
                        if best_fallback_info is None or diff < best_fallback_info.get("diff", 99999):
                            for af in audio_files:
                                backup_p = output_dir / f"backup_{af.name}"
                                shutil.copy2(af, backup_p)
                            res_info["_source_title"] = cand_entry_title or cand_title
                            res_info["_source_modifiers"] = cand_modifiers
                            best_fallback_info = {"res_info": res_info, "diff": diff}
                            has_usable_candidate = True

                    shutil.rmtree(cand_dir, ignore_errors=True)
                    _cleanup_temp_candidate_files(output_dir)
                    continue
                except Exception as cand_err:
                    if cancel_event and cancel_event.is_set():
                        shutil.rmtree(output_dir, ignore_errors=True)
                        raise
                    last_cand_error = cand_err
                    cand_err_str = str(cand_err).lower()
                    is_timeout = isinstance(cand_err, TimeoutError) or "exceeded candidate deadline" in cand_err_str
                    if is_timeout:
                        print(f"{req_tag}[YTDLP] Candidate #{cand_idx+1} exceeded candidate deadline; aborting", flush=True)
                        shutil.rmtree(cand_dir, ignore_errors=True)
                        _cleanup_temp_candidate_files(output_dir)
                        continue

                    is_drm = any(k in cand_err_str for k in ["drm protected", "drm", "copyright", "georestricted"])
                    if is_drm:
                        print(f"{req_tag}[AUTHENTICITY] Кандидат #{cand_idx+1} '{cand_title}' ({cand_source}) защищён DRM. Пропускаем.", flush=True)
                    else:
                        print(f"{req_tag}[DOWNLOADER] Кандидат #{cand_idx+1} не удался ({cand_err}).", flush=True)

                    # Если кандидат YouTube завершился ошибкой формата/клиента/бота, пробуем резервный вызов без cookies
                    if cand_source == "youtube" and not getattr(selected_entry, "_retried", False):
                        if any(k in cand_err_str for k in ["reload", "format", "sign in", "bot", "403", "429"]):
                            selected_entry["_retried"] = True
                            t_global_remain = t_global_deadline - time.perf_counter()
                            t_retry_remain = min(CANDIDATE_DOWNLOAD_TIMEOUT, max(0.0, t_global_remain - GLOBAL_SC_FALLBACK_RESERVE))
                            if t_retry_remain < MIN_RETRY_TIME_REMAINING:
                                print(f"{req_tag}[DOWNLOADER] Недостаточно времени для повторной загрузки кандидата #{cand_idx+1} без cookies ({t_retry_remain:.1f}s осталось). Пропускаем.", flush=True)
                                shutil.rmtree(cand_dir, ignore_errors=True)
                                _cleanup_temp_candidate_files(output_dir)
                                continue

                            print(f"{req_tag}[DOWNLOADER] Пробуем резервный запуск для кандидата #{cand_idx+1} без cookies (лимит {t_retry_remain:.1f}s)...", flush=True)
                            retry_cand_opts = dict(cand_dl_opts)
                            retry_cand_opts.pop("cookiefile", None)
                            retry_cand_opts.pop("extractor_args", None)

                            retry_cancel_event = threading.Event()
                            retry_deadline = time.perf_counter() + t_retry_remain

                            def retry_p_hook(d):
                                if cancel_event and cancel_event.is_set():
                                    raise RuntimeError("Download cancelled by user")
                                if retry_cancel_event.is_set() or time.perf_counter() > retry_deadline:
                                    retry_cancel_event.set()
                                    raise TimeoutError(f"Candidate #{cand_idx+1} retry exceeded candidate deadline")
                                if d.get("status") == "downloading":
                                    if progress_callback:
                                        tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                                        dl = d.get("downloaded_bytes") or 0
                                        if tot and tot > 0:
                                            pct = max(0, min(100, int(dl / tot * 100)))
                                            try:
                                                progress_callback("downloading", pct)
                                            except Exception:
                                                pass
                                elif d.get("status") == "finished":
                                    if progress_callback:
                                        try:
                                            progress_callback("downloading", 100)
                                        except Exception:
                                            pass

                            def retry_pp_hook(d):
                                if cancel_event and cancel_event.is_set():
                                    raise RuntimeError("Download cancelled by user")
                                if retry_cancel_event.is_set() or time.perf_counter() > retry_deadline:
                                    retry_cancel_event.set()
                                    raise TimeoutError(f"Candidate #{cand_idx+1} retry exceeded candidate deadline")
                                if d.get("status") == "started":
                                    if progress_callback:
                                        try:
                                            progress_callback("processing", 100)
                                        except Exception:
                                            pass

                            retry_cand_opts["progress_hooks"] = [retry_p_hook]
                            retry_cand_opts["postprocessor_hooks"] = [retry_pp_hook]

                            try:
                                try:
                                    res_info = _extract_info_with_timeout(
                                        ydl_opts=retry_cand_opts,
                                        url=target_url,
                                        timeout_sec=t_retry_remain,
                                        cand_cancel_event=retry_cancel_event,
                                        parent_cancel_event=cancel_event
                                    )
                                except Exception as first_retry_err:
                                    err_first_str = str(first_retry_err).lower()
                                    rem_after_first = t_global_deadline - time.perf_counter() - GLOBAL_SC_FALLBACK_RESERVE
                                    if any(k in err_first_str for k in ["403", "forbidden", "format", "unavailable"]) and rem_after_first >= MIN_RETRY_TIME_REMAINING:
                                        print(f"{req_tag}[DOWNLOADER] Ошибка 403 при основном формате (140/251). Пробуем альтернативный аудиопоток без SABR-блокировки...", flush=True)
                                        alt_cand_opts = dict(retry_cand_opts)
                                        alt_cand_opts["format"] = "ba[format_id!*=140][format_id!*=251]/ba[ext=m4a]/ba"
                                        retry_cancel_event.clear()
                                        alt_deadline = time.perf_counter() + min(CANDIDATE_DOWNLOAD_TIMEOUT, rem_after_first)
                                        retry_deadline = alt_deadline
                                        res_info = _extract_info_with_timeout(
                                            ydl_opts=alt_cand_opts,
                                            url=target_url,
                                            timeout_sec=min(CANDIDATE_DOWNLOAD_TIMEOUT, rem_after_first),
                                            cand_cancel_event=retry_cancel_event,
                                            parent_cancel_event=cancel_event
                                        )
                                    else:
                                        raise first_retry_err
                                audio_files = _find_or_convert_candidate_audio(cand_dir, cand_idx, cand_title, req_tag=req_tag)
                                if not audio_files and output_dir.exists():
                                    direct_audio = _find_or_convert_candidate_audio(output_dir, cand_idx, cand_title, req_tag=req_tag)
                                    if direct_audio:
                                        for df in direct_audio:
                                            target_p = cand_dir / df.name
                                            shutil.move(str(df), str(target_p))
                                        audio_files = _find_or_convert_candidate_audio(cand_dir, cand_idx, cand_title, req_tag=req_tag)
                                if audio_files:
                                    retry_title = unicodedata.normalize("NFKC", res_info.get("title") or cand_title or "")
                                    retry_match_ratio = compute_title_match_ratio(retry_title, core_title_words)
                                    if core_title_words and retry_match_ratio < 0.5:
                                        print(f"{req_tag}[AUTHENTICITY] Резервный запуск: кандидат '{retry_title}' не соответствует названию ({retry_match_ratio:.2f} < 0.5). Отклоняем.", flush=True)
                                        shutil.rmtree(cand_dir, ignore_errors=True)
                                        _cleanup_temp_candidate_files(output_dir)
                                        continue

                                    cand_art_val = res_info.get("artist") or selected_entry.get("artist") or res_info.get("creator") or selected_entry.get("creator")
                                    if custom_artist and not validate_candidate_artist(
                                        expected_artist=custom_artist,
                                        candidate_title=retry_title,
                                        candidate_uploader=res_info.get("uploader") or selected_entry.get("uploader"),
                                        candidate_channel=res_info.get("channel") or selected_entry.get("channel"),
                                        expected_title=custom_title,
                                        candidate_artist=cand_art_val
                                    ):
                                        print(f"{req_tag}[AUTHENTICITY] Резервный запуск: кандидат '{retry_title}' не принадлежит исполнителю '{custom_artist}'. Отклоняем.", flush=True)
                                        shutil.rmtree(cand_dir, ignore_errors=True)
                                        _cleanup_temp_candidate_files(output_dir)
                                        continue

                                    retry_mods = extract_modifiers(f"{retry_title} {selected_entry.get('uploader') or ''}", ignore_words=ignore_cand_words)
                                    if not requested_modifiers and retry_mods:
                                        shutil.rmtree(cand_dir, ignore_errors=True)
                                        _cleanup_temp_candidate_files(output_dir)
                                        continue

                                    if requested_modifiers:
                                        if not is_candidate_matching_modifiers(requested_modifiers, retry_mods):
                                            shutil.rmtree(cand_dir, ignore_errors=True)
                                            _cleanup_temp_candidate_files(output_dir)
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
                                        winner_path = _promote_candidate_assets(cand_dir, output_dir, audio_files[0])
                                        audio_files = [winner_path]
                                        print(f"{req_tag}[DOWNLOADER] Резервный запуск кандидата #{cand_idx+1} успешен!", flush=True)
                                        has_usable_candidate = True
                                        res_info["_source_title"] = retry_title
                                        res_info["_source_modifiers"] = retry_mods
                                        return res_info
                            except Exception as sub_retry_err:
                                sub_err_str = str(sub_retry_err).lower()
                                if isinstance(sub_retry_err, TimeoutError) or "exceeded candidate deadline" in sub_err_str:
                                    print(f"{req_tag}[YTDLP] Candidate #{cand_idx+1} retry exceeded candidate deadline; aborting", flush=True)
                                else:
                                    print(f"{req_tag}[DOWNLOADER] Резервный запуск кандидата #{cand_idx+1} также завершился ошибкой ({sub_retry_err}).", flush=True)
                                shutil.rmtree(cand_dir, ignore_errors=True)
                                _cleanup_temp_candidate_files(output_dir)
                                continue

                    shutil.rmtree(cand_dir, ignore_errors=True)
                    _cleanup_temp_candidate_files(output_dir)

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
            if not has_usable_candidate and not is_direct_yt:
                print(f"{req_tag}[DOWNLOADER] Экстренный Fallback: поиск трека '{clean_search}' в SoundCloud...", flush=True)
                try:
                    sc_opts = dict(options)
                    sc_opts.pop("cookiefile", None)
                    sc_opts.pop("extractor_args", None)
                    sc_opts.pop("proxy", None)  # Прокси применяется только к YouTube
                    sc_opts["extract_flat"] = True
                    sc_opts["noplaylist"] = True
                    sc_opts["ignoreerrors"] = True
                    sc_opts["socket_timeout"] = 6
                    sc_opts["retries"] = 1
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
                            for s_idx, s_cand in enumerate(sc_ranked):
                                s_url = s_cand.get("webpage_url") or s_cand.get("url")
                                s_title = unicodedata.normalize("NFKC", s_cand.get("title") or "")
                                is_sc_el, sc_r_reason = is_candidate_download_eligible(
                                    candidate=s_cand,
                                    custom_artist=custom_artist,
                                    custom_title=custom_title,
                                    core_title_words=core_title_words,
                                    expected_duration=expected_duration,
                                    requested_modifiers=requested_modifiers,
                                    is_apple_music=is_apple_music,
                                    is_text_input=is_text_input,
                                    clean_search=clean_search
                                )
                                if not is_sc_el:
                                    print(f"{req_tag}[AUTHENTICITY] SoundCloud кандидат '{s_title}' отклонён до загрузки: {sc_r_reason}", flush=True)
                                    continue

                                if s_url:
                                    sc_token = uuid.uuid4().hex[:6]
                                    sc_cand_dir = output_dir / f"sc_cand_{s_idx}_{sc_token}"
                                    sc_cand_dir.mkdir(parents=True, exist_ok=True)
                                    sc_opts_dl = dict(options)
                                    sc_opts_dl.pop("cookiefile", None)
                                    sc_opts_dl.pop("extractor_args", None)
                                    sc_opts_dl.pop("proxy", None)  # Прокси только для YouTube
                                    sc_opts_dl["extract_flat"] = False
                                    sc_opts_dl["outtmpl"] = str(sc_cand_dir / "%(title).100B.%(ext)s")
                                    sc_opts_dl["socket_timeout"] = 8
                                    sc_opts_dl["retries"] = 1
                                    if progress_callback:
                                        def sc_p_hook(d):
                                            if d.get("status") == "downloading":
                                                tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                                                dl = d.get("downloaded_bytes") or 0
                                                if tot and tot > 0:
                                                    pct = max(0, min(100, int(dl / tot * 100)))
                                                    try:
                                                        progress_callback("downloading", pct)
                                                    except Exception:
                                                        pass
                                            elif d.get("status") == "finished":
                                                try:
                                                    progress_callback("downloading", 100)
                                                except Exception:
                                                    pass

                                        def sc_pp_hook(d):
                                            if d.get("status") == "started":
                                                try:
                                                    progress_callback("processing", 100)
                                                except Exception:
                                                    pass

                                        sc_opts_dl["progress_hooks"] = [sc_p_hook]
                                        sc_opts_dl["postprocessor_hooks"] = [sc_pp_hook]
                                    try:
                                        sc_remain = max(2.0, t_global_deadline - time.perf_counter())
                                        sc_timeout = min(10.0, sc_remain)
                                        sc_cand_cancel = threading.Event()
                                        res_cand = _extract_info_with_timeout(
                                            ydl_opts=sc_opts_dl,
                                            url=s_url,
                                            timeout_sec=sc_timeout,
                                            cand_cancel_event=sc_cand_cancel,
                                            parent_cancel_event=cancel_event
                                        )
                                        audio_files = _find_or_convert_candidate_audio(sc_cand_dir, cand_idx=0, cand_title=s_title, req_tag=req_tag)
                                        if not audio_files and output_dir.exists():
                                            direct_audio = _find_or_convert_candidate_audio(output_dir, cand_idx=0, cand_title=s_title, req_tag=req_tag)
                                            if direct_audio:
                                                for df in direct_audio:
                                                    target_p = sc_cand_dir / df.name
                                                    shutil.move(str(df), str(target_p))
                                                audio_files = _find_or_convert_candidate_audio(sc_cand_dir, cand_idx=0, cand_title=s_title, req_tag=req_tag)
                                        if audio_files:
                                            sc_entry_title = unicodedata.normalize("NFKC", res_cand.get("title") or s_title or "")
                                            sc_match_ratio = compute_title_match_ratio(sc_entry_title, core_title_words)
                                            if core_title_words and sc_match_ratio < 0.5:
                                                print(f"{req_tag}[AUTHENTICITY] SoundCloud кандидат '{sc_entry_title}' недостаточно соответствует названию ({sc_match_ratio:.2f} < 0.5). Отклоняем.", flush=True)
                                                shutil.rmtree(sc_cand_dir, ignore_errors=True)
                                                continue
                                            sc_art_val = res_cand.get("artist") or s_cand.get("artist") or res_cand.get("creator") or s_cand.get("creator")
                                            if custom_artist and not validate_candidate_artist(
                                                expected_artist=custom_artist,
                                                candidate_title=sc_entry_title,
                                                candidate_uploader=res_cand.get("uploader") or s_cand.get("uploader"),
                                                candidate_channel=res_cand.get("channel") or s_cand.get("channel"),
                                                expected_title=custom_title,
                                                candidate_artist=sc_art_val
                                            ):
                                                print(f"{req_tag}[AUTHENTICITY] SoundCloud кандидат '{sc_entry_title}' не принадлежит исполнителю '{custom_artist}'. Отклоняем.", flush=True)
                                                shutil.rmtree(sc_cand_dir, ignore_errors=True)
                                                continue
                                            sc_dur = int(res_cand.get("duration") or 0)
                                            sc_diff = abs(sc_dur - expected_duration) if (expected_duration and expected_duration > 35 and sc_dur > 0) else 0
                                            if (is_apple_music or is_text_input) and expected_duration and not requested_modifiers and sc_diff > 4:
                                                print(f"{req_tag}[DOWNLOADER] SoundCloud track '{s_title}' diff={sc_diff}s > 4s. Rejecting.", flush=True)
                                                shutil.rmtree(sc_cand_dir, ignore_errors=True)
                                                continue
                                            sc_dl_mods = extract_modifiers(f"{sc_entry_title} {res_cand.get('uploader') or s_cand.get('uploader') or ''}", ignore_words=ignore_cand_words)
                                            if is_apple_music or is_text_input:
                                                sc_unrequested = sc_dl_mods - requested_modifiers
                                                if not requested_modifiers:
                                                    if sc_dl_mods:
                                                        print(f"{req_tag}[AUTHENTICITY] SoundCloud кандидат '{sc_entry_title}' содержит нежелательные модификаторы {sc_dl_mods}. Отклоняем.", flush=True)
                                                        shutil.rmtree(sc_cand_dir, ignore_errors=True)
                                                        continue
                                                else:
                                                    sc_conflicting = sc_unrequested & {
                                                        "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix", "club mix", "dance mix",
                                                        "live", "лайв", "концерт", "performance", "cover", "кавер", "acoustic", "акустика",
                                                        "drum edit", "drums", "dnb", "драмка", "с драмкой",
                                                        "slowed", "slow", "sped up", "speed up", "nightcore",
                                                        "instrumental", "инструментал", "minus", "минус", "karaoke"
                                                    }
                                                    if sc_conflicting or not is_candidate_matching_modifiers(requested_modifiers, sc_dl_mods):
                                                        print(f"{req_tag}[AUTHENTICITY] SoundCloud кандидат '{sc_entry_title}' не удовлетворяет запрошенным модификаторам {requested_modifiers}. Отклоняем.", flush=True)
                                                        shutil.rmtree(sc_cand_dir, ignore_errors=True)
                                                        continue

                                            new_sc_dur = _apply_audio_modifier_if_needed(audio_files[0], requested_modifiers, sc_dl_mods, req_tag, req_query=clean_search)
                                            if new_sc_dur > 0:
                                                res_cand["duration"] = new_sc_dur
                                            res_cand["_source_title"] = s_title
                                            res_cand["_source_modifiers"] = sc_dl_mods

                                            winner_path = _promote_candidate_assets(sc_cand_dir, output_dir, audio_files[0])
                                            audio_files = [winner_path]

                                            has_usable_candidate = True
                                            return res_cand
                                    except Exception as s_err:
                                        print(f"{req_tag}[DOWNLOADER] SoundCloud fallback candidate '{s_url}' не удался: {s_err}", flush=True)
                                        shutil.rmtree(sc_cand_dir, ignore_errors=True)
                                        _cleanup_temp_candidate_files(output_dir)
                                        continue
                except Exception as sc_err:
                    print(f"{req_tag}[DOWNLOADER] Экстренный поиск SoundCloud не удался: {sc_err}", flush=True)
                if last_cand_error:
                    if any(k in str(last_cand_error).lower() for k in ["drm protected", "drm"]):
                        raise ValueError("Возникла ошибка 45. Попробуйте другой источник.")
                    if any(k in str(last_cand_error).lower() for k in ["не был создан", "не предоставил доступного аудиопотока", "превышен max_filesize"]):
                        raise ValueError("Возникла ошибка 46. Попробуйте другой трек.")
                    raise last_cand_error
            if not entries:
                raise ValueError("Возникла ошибка 47. Попробуйте другой запрос.")
            raise ValueError("Возникла ошибка 48. Попробуйте изменить запрос.")
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
                if d.get("status") == "downloading":
                    if not hook_times["dl_start"]:
                        hook_times["dl_start"] = time.perf_counter()
                    if progress_callback:
                        total = d.get("total_bytes") or d.get("total_bytes_estimate")
                        downloaded = d.get("downloaded_bytes") or 0
                        if total and total > 0:
                            pct = max(0, min(100, int(downloaded / total * 100)))
                            try:
                                progress_callback("downloading", pct)
                            except Exception:
                                pass
                elif d.get("status") == "finished":
                    hook_times["dl_end"] = time.perf_counter()
                    if progress_callback:
                        try:
                            progress_callback("downloading", 100)
                        except Exception:
                            pass

            def pp_hook(d):
                if d.get("status") == "started":
                    if not hook_times["pp_start"]:
                        hook_times["pp_start"] = time.perf_counter()
                    in_f = d.get("info_dict", {}).get("filepath")
                    log_memory_stage("before FFmpeg", req_id=request_id, source=source, file_path=in_f)
                    if progress_callback:
                        try:
                            progress_callback("processing", 100)
                        except Exception:
                            pass
                elif d.get("status") == "finished":
                    hook_times["pp_end"] = time.perf_counter()
                    out_f = d.get("info_dict", {}).get("filepath")
                    log_memory_stage("after FFmpeg", req_id=request_id, source=source, file_path=out_f)

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
            res_info["_source_title"] = res_info.get("title") or ""
            direct_text = f"{res_info.get('title') or ''} {res_info.get('uploader') or ''} {res_info.get('channel') or ''}".lower()
            res_info["_source_modifiers"] = extract_modifiers(direct_text)
            return res_info

    log_memory_stage("before yt-dlp", req_id=request_id, source="youtube" if is_youtube else "soundcloud")
    try:
        info = _execute_extraction(ydl_opts)
    except Exception as extract_err:
        err_msg = str(extract_err).lower()
        def _fallback_direct_search(last_err):
            # Точная прямая ссылка не должна молча заменяться другим треком (Section 11, 15)
            print(f"[DOWNLOADER] Прямая ссылка недоступна: {last_err}", flush=True)
            raise last_err

        is_retryable_yt = is_youtube and any(
            m in err_msg for m in [
                "sign in", "bot", "cookie", "reload", "403",
                "requested format", "format", "not available", "unavailable"
            ]
        )

        if is_retryable_yt:
            print(f"[DOWNLOADER] Первичный запуск YouTube вызвал ошибку ({extract_err}). Пробуем чистый запуск без cookies...", flush=True)
            ydl_opts_retry = dict(ydl_opts)
            ydl_opts_retry.pop("cookiefile", None)
            ydl_opts_retry.pop("extractor_args", None)
            ydl_opts_retry["format"] = "ba[ext=m4a]/ba[ext=mp3]/ba"
            try:
                info = _execute_extraction(ydl_opts_retry)
            except Exception as retry_err:
                if any(k in str(retry_err).lower() for k in ["403", "forbidden", "format", "unavailable"]):
                    print(f"[DOWNLOADER] Ошибка 403 при чистом запуске прямой ссылки. Пробуем альтернативный аудиопоток...", flush=True)
                    ydl_opts_alt = dict(ydl_opts_retry)
                    ydl_opts_alt["format"] = "ba[format_id!*=140][format_id!*=251]/ba[ext=m4a]/ba"
                    try:
                        info = _execute_extraction(ydl_opts_alt)
                    except Exception as alt_err:
                        return _fallback_direct_search(alt_err)
                else:
                    return _fallback_direct_search(retry_err)
        else:
            return _fallback_direct_search(extract_err)
    log_memory_stage("after yt-dlp", req_id=request_id, source="youtube" if is_youtube else "soundcloud")

    if "entries" in info:
        if not info["entries"]:
            raise ValueError("Возникла ошибка 47. Попробуйте другой запрос.")
        info = info["entries"][0]

    audio_files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"]]
    if not audio_files:
        existing = [f.name for f in output_dir.iterdir() if f.is_file()]
        print(f"{req_tag}[DOWNLOADER] ERROR: Аудиофайл не найден среди файлов в {output_dir}: {existing}", flush=True)
        # Аварийная проверка: возможно yt-dlp сохранил аудио (.webm, .opus, .ogg), но постпроцессор не завершился
        alt_audio = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".webm", ".opus", ".ogg", ".flac", ".wav"]]
        if alt_audio:
            print(f"{req_tag}[DOWNLOADER] Обнаружен альтернативный аудиопоток {alt_audio[0].name}. Запускаем аварийное извлечение M4A через FFmpeg (-threads 1)...", flush=True)
            emergency_out = output_dir / f"{alt_audio[0].stem}.m4a"
            try:
                import subprocess
                cmd = ["ffmpeg", "-y", "-i", str(alt_audio[0]), "-c:a", "aac", "-b:a", "192k", "-threads", "1", "-vn", str(emergency_out)]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0 and emergency_out.exists() and emergency_out.stat().st_size > 1000:
                    audio_files = [emergency_out]
                    alt_audio[0].unlink(missing_ok=True)
            except Exception as em_err:
                print(f"{req_tag}[DOWNLOADER] Аварийная конвертация не удалась: {em_err}", flush=True)

        if not audio_files:
            raise FileNotFoundError(f"Аудиофайл не был создан после обработки. (найдены файлы: {existing})")

    audio_path = audio_files[0]
    filesize = audio_path.stat().st_size

    # Находим обложку
    thumb_candidates = list(output_dir.glob("*.webp")) + list(output_dir.glob("*.jpg")) + list(output_dir.glob("*.png"))
    thumbnail_path = None
    embedded_cover_path = None
    if thumb_candidates:
        embedded_cover_path = _prepare_embedded_cover(thumb_candidates[0])
        thumbnail_path = _convert_thumbnail_to_jpg(embedded_cover_path or thumb_candidates[0])

    # 1. Authoritative Title: если передан валидный custom_title, YouTube никогда его не подменяет
    extracted_title = None
    if custom_title and custom_title.strip() and custom_title.strip().lower() != "unknown track":
        extracted_title = _clean_audio_branding(custom_title.strip())

    # 2. Authoritative Artist: если передан валидный custom_artist, YouTube никогда его не подменяет
    extracted_artist = None
    if custom_artist and custom_artist.strip() and not is_generic_artist_name(custom_artist):
        extracted_artist = _clean_audio_branding(custom_artist.strip())

    # 3. Если artist или title не были заданы через authoritative metadata:
    if not extracted_artist or not extracted_title:
        # Проверяем структурированные теги yt-dlp (YouTube Music structured track/artist)
        yt_track = info.get("track")
        yt_artist = info.get("artist") or info.get("creator")
        if yt_artist and is_generic_artist_name(yt_artist):
            yt_artist = None

        raw_info_title = unicodedata.normalize("NFKC", info.get("title") or "").strip()
        cand_art_from_title, cand_tit_from_title = None, None
        for sep in [" - ", " — ", " – ", " -- "]:
            if sep in raw_info_title:
                pts = raw_info_title.split(sep, 1)
                a_cand = _clean_audio_branding(pts[0].strip())
                t_cand = _clean_audio_branding(pts[1].strip())
                if a_cand and not is_generic_artist_name(a_cand):
                    cand_art_from_title = a_cand
                    cand_tit_from_title = t_cand
                    break

        if not extracted_artist:
            if yt_artist and not is_generic_artist_name(yt_artist):
                extracted_artist = _clean_audio_branding(yt_artist)
            elif cand_art_from_title:
                extracted_artist = cand_art_from_title
            else:
                raw_up = (info.get("uploader") or info.get("channel") or "").replace(" - Topic", "").replace("- Topic", "").replace(" – Topic", "").strip()
                if raw_up and not is_generic_artist_name(raw_up):
                    extracted_artist = _clean_audio_branding(raw_up)
                else:
                    extracted_artist = "Unknown Artist"

        if not extracted_title:
            extracted_title = _clean_audio_branding(yt_track or cand_tit_from_title or raw_info_title or "Unknown Track")

    # 4. Authoritative Album: если передан custom_album, YouTube никогда его не подменяет.
    # Если custom_album не передан, берем структурированный album из info (если не generic).
    extracted_album = None
    if custom_album and custom_album.strip():
        extracted_album = _clean_audio_branding(custom_album.strip())
    elif info.get("album") and not is_generic_artist_name(str(info.get("album"))):
        extracted_album = _clean_audio_branding(str(info.get("album")).strip())

    raw_source_title = info.get("_source_title") or info.get("title") or info.get("track") or extracted_title
    source_title = _clean_audio_branding(raw_source_title) or raw_source_title

    raw_source_mods = info.get("_source_modifiers")
    if raw_source_mods is not None:
        source_modifiers = set(raw_source_mods)
    else:
        src_text = f"{source_title} {info.get('uploader') or ''} {info.get('channel') or ''}".lower()
        source_modifiers = extract_modifiers(src_text) or set()

    duration = int(info.get("duration") or 0)
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(audio_path)
        if mf and mf.info and hasattr(mf.info, "length"):
            duration = int(round(mf.info.length))
    except Exception:
        pass

    # Финальная валидация хронометража перед отдачей DownloadedAudio (Apple Music, Spotify, Deezer, Text search)
    is_direct_media_url = bool(not query_or_url.startswith(("ytsearch", "scsearch")) and any(d in query_or_url.lower() for d in ("youtube.com", "youtu.be", "soundcloud.com", "bandcamp.com", "tiktok.com")))
    if not is_direct_media_url and expected_duration and expected_duration > 35:
        all_req_mods = extract_modifiers(f"{custom_artist or ''} {custom_title or ''} {query_or_url} {requested_variant or ''}")
        if not all_req_mods and (not requested_variant or requested_variant == "original") and duration > 0:
            final_dl_diff = abs(duration - expected_duration)
            max_final_gate = max(4, min(7, int(expected_duration * 0.02)))
            if final_dl_diff > max_final_gate:
                raise ValueError("Возникла ошибка 49. Не удалось найти подходящую версию трека.")

    log_memory_stage("before metadata", req_id=request_id, source=source_title or "track", file_path=audio_path)
    t_tag0 = time.perf_counter()
    _apply_custom_metadata(audio_path, extracted_title, extracted_artist, embedded_cover_path or thumbnail_path, album=extracted_album)
    perf_timings["tags"] = time.perf_counter() - t_tag0
    log_memory_stage("after metadata", req_id=request_id, source=source_title or "track", file_path=audio_path)

    return DownloadedAudio(
        file_path=audio_path,
        title=extracted_title,
        artist=extracted_artist,
        duration=duration,
        thumbnail_path=thumbnail_path,
        filesize=filesize,
        folder_path=output_dir,
        perf_timings=perf_timings,
        invocations=invocations,
        source_title=source_title,
        source_modifiers=source_modifiers,
        cover_path=embedded_cover_path or thumbnail_path,
        album=extracted_album
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


GENERIC_ARTIST_NAMES = {
    "release",
    "various artists",
    "various artists - topic",
    "various artists – topic",
    "various artists — topic",
    "release - topic",
    "release – topic",
    "release — topic",
    "artist",
    "artist - topic",
    "artist – topic",
    "artist — topic",
    "исполнитель",
    "исполнитель - тема",
    "top tracks",
    "vevo",
    "official audio",
    "soundcloud",
    "youtube",
    "topics",
    "topic",
    "тема",
    "различные исполнители",
    "unknown artist",
}


def is_generic_artist_name(name: Optional[str]) -> bool:
    """
    Проверяет, является ли переданная строка служебным/техническим каналом YouTube/дистрибьютора,
    который ни при каких обстоятельствах не должен становиться музыкальным исполнителем трека.
    """
    if not name:
        return True
    cleaned = _clean_audio_branding(name).strip().lower()
    cleaned = re.sub(r'[-\s–—]+', ' ', cleaned).strip()
    return cleaned in GENERIC_ARTIST_NAMES or cleaned.endswith(" topic") or cleaned.endswith(" тема")


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
    is_text_input: bool = False,
    requested_variant: Optional[str] = None,
    custom_album: Optional[str] = None,
    progress_callback: Optional[Callable[[str, int], None]] = None
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
            is_text_input,
            requested_variant,
            custom_album,
            progress_callback
        )

        # Если результат подозрительно короткий (< 35s), а ожидался полноценный трек (> 60s)
        if expected_duration and expected_duration > 60 and audio.duration <= 35:
            raise ValueError("Возникла ошибка 50. Не удалось получить полный трек.")

        if thumb_task:
            try:
                downloaded_thumb, highres_cover = await thumb_task
                if downloaded_thumb and downloaded_thumb.exists():
                    audio.thumbnail_path = downloaded_thumb
                cover_to_embed = highres_cover if (highres_cover and highres_cover.exists()) else audio.thumbnail_path
                if cover_to_embed and cover_to_embed.exists():
                    audio.cover_path = cover_to_embed
                    _apply_custom_metadata(audio.file_path, audio.title, audio.artist, cover_to_embed, album=custom_album or audio.album)
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
                if item.is_file() and not item.name.startswith("cover") and not item.name.startswith("thumb_") and not item.name.startswith("embedded_"):
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
        clean_init_q = query_or_url.split(":", 1)[1].strip() if ":" in query_or_url else query_or_url.strip()
        available_yt_fallbacks = [
            q for q in fallback_queries
            if q.strip().lower() != clean_init_q.lower()
        ]
        if available_yt_fallbacks and not is_bot_blocked:
            yt_query = available_yt_fallbacks[0]
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
                    is_text_input,
                    None,
                    custom_album
                )
                if expected_duration and expected_duration > 60 and audio.duration <= 35:
                    raise ValueError("Возникла ошибка 51. Не удалось получить полный трек.")

                if thumb_task:
                    try:
                        downloaded_thumb, highres_cover = await thumb_task
                        if downloaded_thumb and downloaded_thumb.exists() and not audio.thumbnail_path:
                            audio.thumbnail_path = downloaded_thumb
                        cover_to_embed = highres_cover if (highres_cover and highres_cover.exists()) else audio.thumbnail_path
                        if cover_to_embed and cover_to_embed.exists():
                            audio.cover_path = cover_to_embed
                            _apply_custom_metadata(audio.file_path, audio.title, audio.artist, cover_to_embed, album=custom_album or audio.album)
                    except Exception:
                        pass
                if audio.thumbnail_path and not audio.thumbnail_path.exists():
                    audio.thumbnail_path = None
                elapsed_fb = time.time() - t_start
                print(f"{req_tag}[DOWNLOADER] [OK] Трек получен через YouTube Search Fallback за {elapsed_fb:.2f} сек: {audio.title}", flush=True)
                return audio
            except Exception as yt_err:
                safe_yt_err = str(yt_err).encode("ascii", errors="replace").decode("ascii")
                print(f"{req_tag}[DOWNLOADER] Fallback YouTube Search не удался: {safe_yt_err}", flush=True)
        is_direct_yt = bool(("youtube.com" in query_or_url or "youtu.be" in query_or_url) and not query_or_url.startswith("ytsearch"))
        if is_direct_yt:
            print(f"{req_tag}[DOWNLOADER] Прямая ссылка YouTube завершилась ошибкой ({primary_error}). Fallback в SoundCloud запрещён.", flush=True)
            shutil.rmtree(output_dir, ignore_errors=True)
            raise primary_error

        # 2. Fallback в SoundCloud (выбирает полный трек среди лучших вариантов запроса)
        # Применяется для сторонних каталогов (Spotify, Apple Music и др.) либо если исходный запрос не был прямым YouTube
        can_try_sc = not is_direct_yt and (is_apple_music or bool(custom_artist and custom_title) or not query_or_url.startswith(("ytsearch", "scsearch")))
        if can_try_sc:
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
                        is_text_input,
                        None,
                        custom_album
                    )
                    if expected_duration and expected_duration > 60 and audio.duration <= 35:
                        raise ValueError("Возникла ошибка 52. Не удалось получить полный трек.")

                    if thumb_task:
                        try:
                            downloaded_thumb, highres_cover = await thumb_task
                            if downloaded_thumb and downloaded_thumb.exists() and not audio.thumbnail_path:
                                audio.thumbnail_path = downloaded_thumb
                            cover_to_embed = highres_cover if (highres_cover and highres_cover.exists()) else audio.thumbnail_path
                            if cover_to_embed and cover_to_embed.exists():
                                audio.cover_path = cover_to_embed
                                _apply_custom_metadata(audio.file_path, audio.title, audio.artist, cover_to_embed, album=custom_album or audio.album)
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
