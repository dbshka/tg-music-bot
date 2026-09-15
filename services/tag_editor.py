import os
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image

import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover


@dataclass
class AudioMetadata:
    title: str
    artist: str
    album: str
    has_cover: bool
    duration: int
    cover_path: Optional[Path] = None


def read_mp3_tags(file_path: Path) -> AudioMetadata:
    """Считывает текущие теги MP3 или M4A файла и извлекает обложку, если она есть."""
    title = "Без названия"
    artist = "Неизвестный исполнитель"
    album = ""
    has_cover = False
    duration = 0
    cover_path: Optional[Path] = None

    ext = file_path.suffix.lower()
    if ext in [".m4a", ".mp4"]:
        try:
            mp4_audio = MP4(file_path)
            duration = int(mp4_audio.info.length or 0)
            if "\xa9nam" in mp4_audio and mp4_audio["\xa9nam"]:
                title = str(mp4_audio["\xa9nam"][0])
            if "\xa9ART" in mp4_audio and mp4_audio["\xa9ART"]:
                artist = str(mp4_audio["\xa9ART"][0])
            if "\xa9alb" in mp4_audio and mp4_audio["\xa9alb"]:
                album = str(mp4_audio["\xa9alb"][0])
            if "covr" in mp4_audio and mp4_audio["covr"]:
                has_cover = True
                try:
                    cover_data = bytes(mp4_audio["covr"][0])
                    extracted = file_path.parent / "extracted_cover.jpg"
                    with open(extracted, "wb") as f:
                        f.write(cover_data)
                    cover_path = extracted
                except Exception:
                    pass
        except Exception:
            pass
        return AudioMetadata(
            title=title,
            artist=artist,
            album=album,
            has_cover=has_cover,
            duration=duration,
            cover_path=cover_path
        )

    try:
        mp3_audio = MP3(file_path)
        duration = int(mp3_audio.info.length or 0)
        tags = mp3_audio.tags
        if tags:
            if "TIT2" in tags and tags["TIT2"].text:
                title = str(tags["TIT2"].text[0])
            if "TPE1" in tags and tags["TPE1"].text:
                artist = str(tags["TPE1"].text[0])
            if "TALB" in tags and tags["TALB"].text:
                album = str(tags["TALB"].text[0])
            for tag in tags.values():
                if isinstance(tag, APIC):
                    has_cover = True
                    try:
                        extracted = file_path.parent / "extracted_cover.jpg"
                        with open(extracted, "wb") as f:
                            f.write(tag.data)
                        cover_path = extracted
                    except Exception:
                        pass
                    break
    except Exception:
        pass

    return AudioMetadata(
        title=title,
        artist=artist,
        album=album,
        has_cover=has_cover,
        duration=duration,
        cover_path=cover_path
    )


async def read_mp3_tags_async(file_path: Path) -> AudioMetadata:
    """Асинхронное считывание тегов MP3/M4A без блокировки event loop."""
    return await asyncio.to_thread(read_mp3_tags, file_path)


def prepare_cover_image(image_path: Path, output_path: Path) -> Path:
    """Приводит изображение к квадратному формату JPEG до 640x640 для Telegram и ID3/MP4."""
    with Image.open(image_path) as img:
        rgb_img = img.convert("RGB")
        rgb_img.thumbnail((640, 640))
        target = output_path.with_suffix(".jpg")
        rgb_img.save(target, "JPEG", quality=88)
        return target


def apply_mp3_tags(
    file_path: Path,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    cover_path: Optional[Path] = None
) -> Tuple[Path, Optional[Path]]:
    """
    Записывает обновленные теги и обложку в MP3 или M4A.
    Возвращает (file_path, cover_jpg_path).
    """
    final_cover_jpg = None
    if cover_path and cover_path.exists():
        final_cover_jpg = prepare_cover_image(cover_path, file_path.parent / "cover_converted")

    ext = file_path.suffix.lower()
    if ext in [".m4a", ".mp4"]:
        try:
            mp4 = MP4(file_path)
            if title is not None:
                mp4["\xa9nam"] = [title]
            if artist is not None:
                mp4["\xa9ART"] = [artist]
            if album is not None:
                mp4["\xa9alb"] = [album]
            if final_cover_jpg and final_cover_jpg.exists():
                with open(final_cover_jpg, "rb") as f:
                    mp4["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]
            mp4.save()
            return file_path, final_cover_jpg
        except Exception:
            pass

    try:
        id3 = ID3(file_path)
    except ID3NoHeaderError:
        id3 = ID3()

    if title is not None:
        id3["TIT2"] = TIT2(encoding=3, text=title)
    if artist is not None:
        id3["TPE1"] = TPE1(encoding=3, text=artist)
    if album is not None:
        id3["TALB"] = TALB(encoding=3, text=album)

    if final_cover_jpg and final_cover_jpg.exists():
        with open(final_cover_jpg, "rb") as albumart:
            id3.delall("APIC")
            id3.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # Album front cover
                    desc="Cover",
                    data=albumart.read()
                )
            )

    id3.save(file_path, v2_version=3)
    return file_path, final_cover_jpg


async def apply_mp3_tags_async(
    file_path: Path,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    cover_path: Optional[Path] = None
) -> Tuple[Path, Optional[Path]]:
    """Асинхронная запись тегов и обложки без блокировки event loop."""
    return await asyncio.to_thread(apply_mp3_tags, file_path, title, artist, album, cover_path)

