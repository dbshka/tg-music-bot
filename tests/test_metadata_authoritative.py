"""
Regression tests for Authoritative Music Metadata vs YouTube Audio Source.
Ensures YouTube channel/uploader names (e.g. 'Release', '... - Topic', labels)
never overwrite true musical artist, title, or album in inline results, downloader, and ID3 tags.
"""
import io
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from mutagen.id3 import ID3, TIT2, TPE1, TALB

from services.downloader import (
    _apply_custom_metadata,
    _sync_download,
    is_generic_artist_name,
    _clean_audio_branding,
)
from handlers.inline import (
    extract_artist_title_from_query,
    _parse_candidate_title_artist,
    handle_inline_query,
)
from services.search import SearchItem


# ============================================================================
# 1. ТЕСТЫ ДЛЯ ФИЛЬТРАЦИИ СЛУЖЕБНЫХ КАНАЛОВ И ИЗВЛЕЧЕНИЯ ИЗ ЗАПРОСА
# ============================================================================

def test_is_generic_artist_name():
    """Служебные каналы YouTube распознаются и отклоняются как исполнители."""
    assert is_generic_artist_name("Release") is True
    assert is_generic_artist_name("Release - Topic") is True
    assert is_generic_artist_name("Various Artists") is True
    assert is_generic_artist_name("Various Artists - Topic") is True
    assert is_generic_artist_name("Top Tracks") is True
    assert is_generic_artist_name("Unknown Artist") is True
    assert is_generic_artist_name("") is True
    assert is_generic_artist_name(None) is True

    # Реальные артисты не должны считаться generic
    assert is_generic_artist_name("Макулатура") is False
    assert is_generic_artist_name("Макулатура - Topic") is False
    assert is_generic_artist_name("Radiohead") is False
    assert is_generic_artist_name("The Weeknd") is False
    assert is_generic_artist_name("Original Artist") is False


def test_extract_artist_title_from_query():
    """Корректно извлекает артиста и название из текста запроса с различными разделителями."""
    # Двойной дефис (как в запросе пользователя)
    a1, t1 = extract_artist_title_from_query("Макулатура -- Запястья")
    assert a1 == "Макулатура"
    assert t1 == "Запястья"

    # Длинное тире (em-dash)
    a2, t2 = extract_artist_title_from_query("Макулатура — Запястья")
    assert a2 == "Макулатура"
    assert t2 == "Запястья"

    # Обычный дефис с пробелами
    a4, t4 = extract_artist_title_from_query("Original Artist - Track Title")
    assert a4 == "Original Artist"
    assert t4 == "Track Title"

    # Одиночное слово или без дефиса не должно ложно срабатывать
    a5, t5 = extract_artist_title_from_query("Blink-182")
    assert a5 is None
    assert t5 is None

    a6, t6 = extract_artist_title_from_query("Макулатура Запястья")
    assert a6 is None
    assert t6 is None


# ============================================================================
# 2. ТЕСТЫ ДЛЯ ПАРСИНГА КАНДИДАТОВ И INLINE РЕЗУЛЬТАТОВ
# ============================================================================

def test_parse_candidate_title_artist_rejects_release():
    """
    Candidate parser никогда не устанавливает artist='Release'
    даже если на YouTube канал называется 'Release - Topic' или 'Release'.
    """
    # 1. Запрос содержит исполнителя, YouTube вернул 'Release - Topic'
    a1, t1 = _parse_candidate_title_artist(
        raw_title="Запястья",
        uploader="Release - Topic",
        query_artist="Макулатура",
        query_title="Запястья"
    )
    assert a1 == "Макулатура"
    assert t1 == "Запястья"

    # 2. Запрос содержит исполнителя, YouTube вернул 'Release'
    a2, t2 = _parse_candidate_title_artist(
        raw_title="Запястья",
        uploader="Release",
        query_artist="Макулатура",
        query_title="Запястья"
    )
    assert a2 == "Макулатура"
    assert t2 == "Запястья"

    # 3. YouTube вернул 'Artist - Topic'
    a3, t3 = _parse_candidate_title_artist(
        raw_title="Запястья",
        uploader="Макулатура - Topic",
        query_artist="Макулатура",
        query_title="Запястья"
    )
    assert a3 == "Макулатура"
    assert t3 == "Запястья"

    # 4. Запроса нет (query_artist=None), но uploader='Release'
    # Должен возвращаться 'Unknown Artist', а НЕ 'Release'!
    a4, t4 = _parse_candidate_title_artist(
        raw_title="Запястья",
        uploader="Release - Topic"
    )
    assert a4 == "Unknown Artist"
    assert t4 == "Запястья"

    # 5. В самом названии видео есть 'Исполнитель - Название', а uploader='Release'
    a5, t5 = _parse_candidate_title_artist(
        raw_title="Макулатура - Запястья",
        uploader="Release"
    )
    assert a5 == "Макулатура"
    assert t5 == "Запястья"


@pytest.mark.asyncio
async def test_inline_query_displays_authoritative_artist_not_release():
    """
    Inline search для '@bot Макулатура -- Запястья' выводит карточку
    с исполнителем 'Макулатура', даже если YouTube вернул uploader='Release'.
    """
    mock_items = [
        SearchItem(
            index=0,
            title="Запястья",
            uploader="Release - Topic",
            duration=195,
            url="https://www.youtube.com/watch?v=release123",
            source="yt"
        )
    ]

    mock_inline_query = MagicMock()
    mock_inline_query.query = "Макулатура -- Запястья"
    mock_inline_query.answer = AsyncMock()

    with patch("handlers.inline.search_cached_tracks_async", return_value=[]),          patch("handlers.inline.search_tracks_async", return_value=(mock_items, None)),          patch("handlers.inline.save_inline_candidate") as mock_save:

        await handle_inline_query(mock_inline_query)

        assert mock_inline_query.answer.called
        results = mock_inline_query.answer.call_args[1]["results"]
        assert len(results) == 1
        article = results[0]

        # Проверяем, что в описании и тексте сообщения правильный артист, а НЕ Release
        assert "Макулатура" in article.description
        assert "Release" not in article.description
        assert "Макулатура" in article.input_message_content.message_text
        assert "Release" not in article.input_message_content.message_text

        # Проверяем, что в базу сохранен правильный артист
        mock_save.assert_called_once()
        save_kwargs = mock_save.call_args[1]
        assert save_kwargs["artist"] == "Макулатура"
        assert save_kwargs["title"] == "Запястья"


@pytest.mark.asyncio
async def test_inline_query_with_artist_topic_uploader():
    """Inline query с uploader='Artist - Topic' сохраняет реального исполнителя."""
    mock_items = [
        SearchItem(
            index=0,
            title="Запястья",
            uploader="Artist - Topic",
            duration=195,
            url="https://www.youtube.com/watch?v=artist123",
            source="yt"
        )
    ]

    mock_inline_query = MagicMock()
    mock_inline_query.query = "Макулатура — Запястья"
    mock_inline_query.answer = AsyncMock()

    with patch("handlers.inline.search_cached_tracks_async", return_value=[]),          patch("handlers.inline.search_tracks_async", return_value=(mock_items, None)),          patch("handlers.inline.save_inline_candidate") as mock_save:

        await handle_inline_query(mock_inline_query)

        results = mock_inline_query.answer.call_args[1]["results"]
        article = results[0]
        assert "Макулатура" in article.description
        assert "Artist" not in article.description
        assert "Макулатура" in article.input_message_content.message_text


# ============================================================================
# 3. ТЕСТЫ ДЛЯ DOWNLOADER И ID3 ТЕГОВ (MUTAGEN TPE1 / TIT2 / TALB)
# ============================================================================

def _create_dummy_mp3(file_path: Path) -> Path:
    """Создает валидный минимальный MP3-файл для тестирования mutagen ID3."""
    frame_header = b"\xff\xfb\x90\x04" + b"\x00" * 414
    with open(file_path, "wb") as f:
        f.write(frame_header * 10)
    return file_path


def _make_mock_ydl(tmp_path: Path, fake_info: dict):
    def fake_extract_info(url, download=False):
        if download:
            af = tmp_path / "test_audio.mp3"
            _create_dummy_mp3(af)
        return fake_info

    mock_ydl = MagicMock()
    mock_ydl.__enter__.return_value = mock_ydl
    mock_ydl.extract_info.side_effect = fake_extract_info
    return mock_ydl


def test_downloader_authoritative_metadata_preservation_release_uploader(tmp_path):
    """
    Тест 1: Yandex metadata: artist='Original Artist'
    YouTube uploader: 'Release'
    Результат: artist == 'Original Artist', TPE1 == 'Original Artist'
    """
    mock_info = {
        "title": "Release - Track 1",
        "uploader": "Release",
        "channel": "Release",
        "artist": "Release",
        "duration": 180,
    }

    mock_ydl = _make_mock_ydl(tmp_path, mock_info)
    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=release123",
            output_dir=tmp_path,
            custom_title="Original Title",
            custom_artist="Original Artist",
            custom_album="Original Album",
            skip_thumbnail=True,
        )

        assert downloaded.artist == "Original Artist"
        assert downloaded.title == "Original Title"

        id3 = ID3(downloaded.file_path)
        assert str(id3.get("TPE1")) == "Original Artist"
        assert str(id3.get("TIT2")) == "Original Title"
        assert str(id3.get("TALB")) == "Original Album"
        assert "Release" not in str(id3.get("TPE1"))


def test_downloader_authoritative_metadata_preservation_topic_uploader(tmp_path):
    """
    Тест 2: Yandex metadata: artist='Original Artist'
    YouTube uploader: 'Original Artist - Topic'
    Результат: artist == 'Original Artist', TPE1 == 'Original Artist'
    """
    mock_info = {
        "title": "Original Title",
        "uploader": "Original Artist - Topic",
        "channel": "Original Artist - Topic",
        "duration": 180,
    }

    mock_ydl = _make_mock_ydl(tmp_path, mock_info)
    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=topic123",
            output_dir=tmp_path,
            custom_title="Original Title",
            custom_artist="Original Artist",
            custom_album="Original Album",
            skip_thumbnail=True,
        )

        assert downloaded.artist == "Original Artist"
        id3 = ID3(downloaded.file_path)
        assert str(id3.get("TPE1")) == "Original Artist"
        assert str(id3.get("TALB")) == "Original Album"


def test_downloader_authoritative_metadata_preservation_label_uploader(tmp_path):
    """
    Тест 3: Yandex metadata: artist='Original Artist'
    YouTube uploader: 'Some Label'
    Результат: artist == 'Original Artist', TPE1 == 'Original Artist'
    """
    mock_info = {
        "title": "Original Artist - Original Title (Official Audio)",
        "uploader": "Some Label",
        "channel": "Some Label Records",
        "duration": 180,
    }

    mock_ydl = _make_mock_ydl(tmp_path, mock_info)
    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=label123",
            output_dir=tmp_path,
            custom_title="Original Title",
            custom_artist="Original Artist",
            custom_album="Original Album",
            skip_thumbnail=True,
        )

        assert downloaded.artist == "Original Artist"
        id3 = ID3(downloaded.file_path)
        assert str(id3.get("TPE1")) == "Original Artist"
        assert "Some Label" not in str(id3.get("TPE1"))


def test_downloader_preserves_title_and_album_unoverwritten(tmp_path):
    """
    Тест 4: Проверка, что title и album также не перезаписываются YouTube-метаданными.
    """
    mock_info = {
        "title": "Completely Different Video Title",
        "uploader": "Random Channel",
        "album": "YouTube Video Album",
        "duration": 180,
    }

    mock_ydl = _make_mock_ydl(tmp_path, mock_info)
    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=album123",
            output_dir=tmp_path,
            custom_title="Authoritative Title",
            custom_artist="Authoritative Artist",
            custom_album="Authoritative Album",
            skip_thumbnail=True,
        )

        assert downloaded.title == "Authoritative Title"
        assert downloaded.artist == "Authoritative Artist"

        id3 = ID3(downloaded.file_path)
        assert str(id3.get("TIT2")) == "Authoritative Title"
        assert str(id3.get("TPE1")) == "Authoritative Artist"
        assert str(id3.get("TALB")) == "Authoritative Album"


def test_id3_physical_tpe1_tag(tmp_path):
    """
    Тест 5: Проверка фактически записываемого ID3 TPE1 через _apply_custom_metadata.
    Гарантирует, что 'Release' физически не окажется в MP3.
    """
    dummy_mp3 = _create_dummy_mp3(tmp_path / "check_tpe1.mp3")

    _apply_custom_metadata(
        audio_path=dummy_mp3,
        title="Запястья",
        artist="Макулатура",
        album="Утопия"
    )

    id3 = ID3(dummy_mp3)
    assert str(id3["TPE1"]) == "Макулатура"
    assert str(id3["TIT2"]) == "Запястья"
    assert str(id3["TALB"]) == "Утопия"
    assert "Release" not in str(id3["TPE1"])


def test_text_search_fallback_without_external_metadata(tmp_path):
    """
    Тест 6: Обычный текстовый поиск без Yandex/VK metadata.
    Если видео на YouTube имеет формат 'Исполнитель - Название', а uploader='Release',
    downloader правильно извлекает артиста из названия ролика, а не берет 'Release'.
    """
    mock_info = {
        "title": "Макулатура — Запястья",
        "uploader": "Release",
        "channel": "Release - Topic",
        "duration": 180,
    }

    mock_ydl = _make_mock_ydl(tmp_path, mock_info)
    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
        downloaded = _sync_download(
            query_or_url="https://www.youtube.com/watch?v=release456",
            output_dir=tmp_path,
            custom_title=None,
            custom_artist=None,
            skip_thumbnail=True,
        )

        assert downloaded.artist == "Макулатура"
        assert downloaded.title == "Запястья"

        id3 = ID3(downloaded.file_path)
        assert str(id3.get("TPE1")) == "Макулатура"
        assert str(id3.get("TIT2")) == "Запястья"
        assert "Release" not in str(id3.get("TPE1"))
