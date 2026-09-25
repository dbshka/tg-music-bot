import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.extractor import resolve_track_url, ExtractedTrack
from services.identity import validate_artist_match, validate_candidate_artist
from handlers.music import _execute_download_and_send, format_download_error


def test_spotify_artist_homoglyph_matching():
    """Проверяет корректное сопоставление омоглифов (l <-> I) для артиста DJ ZUP RAlii."""
    # Spotify: 'DJ ZUP RAlii' vs YouTube uploader/channel: '_DJ ZUP RAIii_'
    assert validate_artist_match("DJ ZUP RAlii", "_DJ ZUP RAIii_") is True
    assert validate_artist_match("DJ ZUP RAlii", "DJ ZUP RAlii") is True
    # Не должно ложно срабатывать на непохожих артистов
    assert validate_artist_match("Steve Aoki", "Eve") is False
    assert validate_artist_match("Ariana Grande", "Ian") is False
    # Негативные случаи: похожие имена, но совершенно разные исполнители (не должны матчиться!)
    assert validate_artist_match("Alia", "Alla Pugacheva") is False
    assert validate_artist_match("Mili", "Mill") is False
    assert validate_artist_match("Killa", "Kilia") is False
    assert validate_artist_match("Alina", "Allna") is False
    assert validate_artist_match("Lan", "Ian Brown") is False


def test_validate_candidate_artist_with_metadata_artist_on_retry():
    """Проверяет, что при повторной попытке без cookies наличие artist в метаданных валидирует кандидата."""
    # Даже если uploader имеет омоглифы или оформлен нестандартно
    is_valid = validate_candidate_artist(
        expected_artist="DJ ZUP RAlii",
        candidate_title="револьвер",
        candidate_uploader="_DJ ZUP RAIii_",
        candidate_channel="_DJ ZUP RAIii_",
        expected_title="револьвер",
        candidate_artist="DJ ZUP RAlii"
    )
    assert is_valid is True

    # И проверка только по uploader/channel с омоглифом
    is_valid_uploader_only = validate_candidate_artist(
        expected_artist="DJ ZUP RAlii",
        candidate_title="револьвер",
        candidate_uploader="_DJ ZUP RAIii_",
        candidate_channel="_DJ ZUP RAIii_",
        expected_title="револьвер",
        candidate_artist=None
    )
    assert is_valid_uploader_only is True


@pytest.mark.asyncio
async def test_spotify_track_regression_no_error_8():
    """
    Регрессионный тест для трека Spotify 08KUdDLKFbjepWXeGMg0aE:
    гарантирует, что обработчик успешно обрабатывает трек и не возвращает 'Возникла ошибка 8' (Timeout).
    """
    url = "https://open.spotify.com/track/08KUdDLKFbjepWXeGMg0aE?si=d2823991644d431f"

    message = MagicMock()
    message.from_user = MagicMock(id=99999, username="spot_tester", full_name="Spot Tester")
    message.chat = MagicMock(id=99999)
    message.bot = MagicMock()

    reply_msg = MagicMock()
    reply_msg.delete = AsyncMock()
    reply_msg.edit_text = AsyncMock()
    message.reply = AsyncMock(return_value=reply_msg)

    sent_audio_msg = MagicMock()
    sent_audio_msg.audio = MagicMock(file_id="MOCK_SPOTIFY_FILE_ID")
    message.answer_audio = AsyncMock(return_value=sent_audio_msg)

    # Мокаем resolve_track_url чтобы тест был детерминированным и быстрым в CI
    mock_extracted = ExtractedTrack(
        platform="Spotify",
        target="ytsearch5:DJ ZUP RAlii - револьвер",
        is_search=True,
        title="револьвер",
        artist="DJ ZUP RAlii",
        thumbnail_url="https://image-cdn-fa.spotifycdn.com/image/test",
        duration=72
    )

    mock_downloaded = MagicMock()
    mock_downloaded.title = "револьвер"
    mock_downloaded.artist = "DJ ZUP RAlii"
    mock_downloaded.duration = 73
    mock_downloaded.filesize = 1200000
    mock_downloaded.file_path = MagicMock()
    mock_downloaded.thumbnail_path = None
    mock_downloaded.album = None
    mock_downloaded.perf_timings = {}
    mock_downloaded.cleanup = MagicMock()

    with patch("handlers.music.resolve_track_url", new=AsyncMock(return_value=mock_extracted)), \
         patch("handlers.music.download_track", new=AsyncMock(return_value=mock_downloaded)), \
         patch("handlers.music.get_cached_track_async", new=AsyncMock(return_value=None)), \
         patch("handlers.music.save_cached_track_async", new=AsyncMock()):

        await _execute_download_and_send(
            message=message,
            raw_query=url,
            url=url,
            variant="original"
        )

    # Проверяем, что аудио успешно отправлено
    assert message.answer_audio.call_count == 1
    # Проверяем, что не было вызова с Ошибкой 8
    for call in reply_msg.edit_text.call_args_list:
        text = call.args[0] if call.args else ""
        assert "Возникла ошибка 8" not in text
        assert "Возникла ошибка 48" not in text


def test_candidate_retry_403_sabr_fallback_success(tmp_path):
    """
    Проверяет, что при блокировке 403 на основном потоке (140/251)
    кандидат переключается на альтернативный аудиопоток и успешно скачивается
    без ошибки 48.
    """
    from services.downloader import _sync_download
    from pathlib import Path

    extract_calls = []

    def mock_extract(ydl_opts, url, timeout_sec=None, cand_cancel_event=None, parent_cancel_event=None):
        fmt = ydl_opts.get("format", "")
        extract_calls.append(fmt)
        if "[format_id!*=140]" in fmt:
            out_tmpl = ydl_opts.get("outtmpl", {})
            default_tmpl = out_tmpl.get("default", "") if isinstance(out_tmpl, dict) else str(out_tmpl)
            parent_dir = Path(default_tmpl).parent
            parent_dir.mkdir(parents=True, exist_ok=True)
            audio_f = parent_dir / "candidate_0.m4a"
            audio_f.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4096)
            return {
                "id": "RkYikO_-ztw",
                "title": "револьвер",
                "uploader": "_DJ ZUP RAIii_",
                "artist": "DJ ZUP RAlii",
                "duration": 73,
                "ext": "m4a",
                "filepath": str(audio_f)
            }
        else:
            raise RuntimeError("HTTP Error 403: Forbidden")

    mock_ydl = MagicMock()
    mock_ydl.__enter__.return_value = mock_ydl
    mock_ydl.extract_info.return_value = {
        "entries": [
            {
                "id": "RkYikO_-ztw",
                "title": "револьвер",
                "uploader": "_DJ ZUP RAIii_",
                "artist": "DJ ZUP RAlii",
                "duration": 73,
                "url": "https://www.youtube.com/watch?v=RkYikO_-ztw",
                "webpage_url": "https://www.youtube.com/watch?v=RkYikO_-ztw",
                "_source": "youtube"
            }
        ]
    }

    with patch("yt_dlp.YoutubeDL", return_value=mock_ydl), \
         patch("services.downloader._extract_info_with_timeout", side_effect=mock_extract), \
         patch("services.downloader._apply_custom_metadata", return_value=None):
        audio = _sync_download(
            query_or_url="ytsearch5:DJ ZUP RAlii - револьвер",
            output_dir=tmp_path,
            custom_title="револьвер",
            custom_artist="DJ ZUP RAlii",
            expected_duration=72,
            is_text_input=False
        )
        assert audio is not None
        assert audio.title == "револьвер"
        assert audio.artist == "DJ ZUP RAlii"
        assert any("[format_id!*=140][format_id!*=251]" in str(f) for f in extract_calls)


@pytest.mark.asyncio
async def test_can_try_sc_activated_for_catalog_tracks():
    """
    Проверяет, что для каталожных треков (например Spotify, query_or_url='ytsearch5:...', но заданы custom_artist/custom_title)
    SoundCloud fallback разрешён, если поиск на YouTube не дал результата.
    """
    from services.downloader import download_track

    sc_called = False

    def mock_sync(query_or_url, *args, **kwargs):
        nonlocal sc_called
        if "ytsearch" in query_or_url:
            raise ValueError("Возникла ошибка 48. Попробуйте изменить запрос.")
        if "scsearch" in query_or_url:
            sc_called = True
            mock_audio = MagicMock()
            mock_audio.title = "револьвер"
            mock_audio.artist = "DJ ZUP RAlii"
            mock_audio.duration = 72
            mock_audio.file_path = MagicMock()
            mock_audio.thumbnail_path = None
            mock_audio.album = None
            mock_audio.perf_timings = {}
            mock_audio.cleanup = MagicMock()
            return mock_audio
        raise RuntimeError(f"Unknown query: {query_or_url}")

    with patch("services.downloader._sync_download", side_effect=mock_sync), \
         patch("services.downloader._build_fallback_queries", return_value=["DJ ZUP RAlii револьвер"]):
        audio = await download_track(
            query_or_url="ytsearch5:DJ ZUP RAlii - револьвер",
            custom_title="револьвер",
            custom_artist="DJ ZUP RAlii",
            expected_duration=72
        )
        assert sc_called is True
        assert audio is not None
        assert audio.title == "револьвер"
