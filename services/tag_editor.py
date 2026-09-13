import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3


@dataclass
class AudioMetadata:
    title: str
    artist: str
    album: str
    has_cover: bool
    duration: int


def read_mp3_tags(file_path: Path) -> AudioMetadata:
    """Считывает текущие теги MP3-файла."""
    title = "Без названия"
    artist = "Неизвестный исполнитель"
    album = ""
    has_cover = False
    duration = 0

    try:
        mp3_audio = MP3(file_path)
        duration = int(mp3_audio.info.length or 0)
    except Exception:
        pass

    try:
        id3 = ID3(file_path)
        # Проверяем наличие обложки (APIC)
        for tag in id3.values():
            if isinstance(tag, APIC):
                has_cover = True
                break
    except Exception:
        pass

    try:
        easy = EasyID3(file_path)
        if "title" in easy and easy["title"]:
            title = easy["title"][0]
        if "artist" in easy and easy["artist"]:
            artist = easy["artist"][0]
        if "album" in easy and easy["album"]:
            album = easy["album"][0]
    except Exception:
        pass

    return AudioMetadata(
        title=title,
        artist=artist,
        album=album,
        has_cover=has_cover,
        duration=duration
    )


def prepare_cover_image(image_path: Path, output_path: Path) -> Path:
    """Приводит изображение к квадратному формату JPEG до 640x640 для Telegram и ID3."""
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
    Записывает обновленные ID3-теги и обложку в MP3.
    Возвращает (mp3_path, cover_jpg_path).
    """
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

    final_cover_jpg = None
    if cover_path and cover_path.exists():
        # Подготовим качественный JPEG
        final_cover_jpg = prepare_cover_image(cover_path, file_path.parent / "cover_converted")
        with open(final_cover_jpg, "rb") as albumart:
            id3.delall("APIC")  # удаляем старые обложки
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
