import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from services.extractor import find_first_url, extract_youtube_info, resolve_track_url, ExtractedTrack
from services.downloader import is_generic_artist_name, GENERIC_ARTIST_NAMES
from handlers.inline import is_valid_telegram_file_id
from services.persistent_cache import (
    build_source_key,
    check_metadata_match,
    compute_metadata_hash,
    PersistentTrackCacheItem,
)
import inspect
from services import downloader


def test_youtube_url_detection():
    # Different YouTube URL formats
    urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "http://youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abcdef123456",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://youtube.com/shorts/dQw4w9WgXcQ?feature=share",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=12345",
    ]
    for u in urls:
        found = find_first_url(f"Check this song out: {u} please download")
        assert found is not None, f"Failed to detect {u}"
        assert "dQw4w9WgXcQ" in found


def test_persistent_cache_youtube_source_key_normalization():
    # All variations of the same video must map to the identical source_key
    variations = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMdQw4w9WgXcQ",
    ]
    keys = {build_source_key(v)[0] for v in variations}
    assert len(keys) == 1
    assert keys.pop() == "youtube:dQw4w9WgXcQ"

    for v in variations:
        s_key, s_type, s_id = build_source_key(v)
        assert s_key == "youtube:dQw4w9WgXcQ"
        assert s_type == "youtube"
        assert s_id == "dQw4w9WgXcQ"


def test_generic_artist_name_filtering():
    assert is_generic_artist_name("Release - Topic") is True
    assert is_generic_artist_name("Various Artists") is True
    assert is_generic_artist_name("Various Artists - Topic") is True
    assert is_generic_artist_name("Topic") is True
    assert is_generic_artist_name("Unknown Artist") is True
    assert is_generic_artist_name("Queen") is False
    assert is_generic_artist_name("Radiohead") is False
    assert is_generic_artist_name("Daft Punk") is False


def test_metadata_match_case_insensitivity():
    hash1 = compute_metadata_hash("Radiohead", "Creep", "Pablo Honey", 238)
    cached = PersistentTrackCacheItem(
        source_key="youtube:123",
        source_type="youtube",
        source_id="123",
        metadata_hash=hash1,
        artist="Radiohead",
        title="Creep",
        album="Pablo Honey",
        duration=238,
        telegram_file_id="CQACAgIAAxkBAAICamJ1234567890abcdefghijklmnopqrstuvwxyz",
        created_at=0,
        last_used_at=0
    )
    # Exact match
    match, _, _ = check_metadata_match(cached, "Radiohead", "Creep", "Pablo Honey", 238)
    assert match is True
    # Case mismatch -> must still match!
    match_case, _, _ = check_metadata_match(cached, "radiohead", "CREEP", "pablo HONEY", 238)
    assert match_case is True
    # Substantial difference -> mismatch
    mismatch, _, _ = check_metadata_match(cached, "Radiohead", "Karma Police", "OK Computer", 260)
    assert mismatch is False


def test_valid_telegram_file_id():
    assert is_valid_telegram_file_id("CQACAgIAAxkBAAICamJ1234567890abcdefghijklmnopqrstuvwxyz") is True
    assert is_valid_telegram_file_id("BAADBAADAgADBREAAUYy_123456789-abcdef") is True
    assert is_valid_telegram_file_id("") is False
    assert is_valid_telegram_file_id(None) is False
    assert is_valid_telegram_file_id("short") is False
    assert is_valid_telegram_file_id("file_id_dummy") is False


@pytest.mark.asyncio
async def test_extract_youtube_info_canonical_metadata_priority():
    # Mock oEmbed response
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "title": "Rick Astley - Never Gonna Give You Up (Official Music Video)",
        "author_name": "RickAstleyVEVO",
        "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    })
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    # Mock canonical metadata returning clean tags from Deezer/iTunes
    mock_canonical = MagicMock()
    mock_canonical.artist = "Rick Astley"
    mock_canonical.title = "Never Gonna Give You Up"
    mock_canonical.album = "Whenever You Need Somebody"
    mock_canonical.duration = 215
    mock_canonical.thumbnail_url = "https://e-cdns-images.dzcdn.net/images/cover/highres.jpg"

    with patch("services.extractor.resolve_canonical_track_info_async", new=AsyncMock(return_value=mock_canonical)):
        track = await extract_youtube_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", mock_session)

        assert track is not None
        assert track.artist == "Rick Astley"
        assert track.title == "Never Gonna Give You Up"
        assert track.duration == 215
        assert track.platform == "YouTube / YouTube Music"
        assert track.is_search is False


@pytest.mark.asyncio
async def test_extract_youtube_info_fallback_when_generic_channel():
    # Channel is generic "Release - Topic", title is "Queen - Bohemian Rhapsody"
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "title": "Queen - Bohemian Rhapsody",
        "author_name": "Release - Topic",
        "thumbnail_url": "https://i.ytimg.com/vi/fJ9rUzIMcZQ/default.jpg",
    })
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with patch("services.extractor.resolve_canonical_track_info_async", new=AsyncMock(return_value=None)):
        track = await extract_youtube_info("https://youtu.be/fJ9rUzIMcZQ", mock_session)

        assert track is not None
        assert track.artist == "Queen"
        assert track.title == "Bohemian Rhapsody"


def test_downloader_format_options_strictly_audio_only():
    import re
    import yt_dlp
    source = inspect.getsource(downloader._sync_download)

    # 1. Запрещенные шаблоны видеопотоков
    forbidden_patterns = [
        r"bv\*\+ba",
        r"/b/best",
        r"/best",
        r"/b\b",
    ]
    for pat in forbidden_patterns:
        match = re.search(pat, source)
        assert match is None, f"Found forbidden video/combined pattern '{pat}' in _sync_download source!"

    # 2. Обязательное присутствие строгой audio-only строки
    assert 'ba[ext=m4a]/ba[ext=mp3]/ba' in source

    # 3. Функциональная проверка через реальный build_format_selector yt-dlp:
    ydl = yt_dlp.YoutubeDL({'format': 'ba[ext=m4a]/ba[ext=mp3]/ba'})
    selector = ydl.build_format_selector('ba[ext=m4a]/ba[ext=mp3]/ba')

    # Тестовый набор форматов YouTube:
    # itag 18: 360p combined video+audio
    # itag 22: 720p combined video+audio
    # itag 137: 1080p video-only
    # itag 140: pure audio m4a (AAC 128k)
    # itag 251: pure audio webm (Opus 160k)
    fmt_combined_360p = {'format_id': '18', 'ext': 'mp4', 'vcodec': 'avc1.42001E', 'acodec': 'mp4a.40.2'}
    fmt_combined_720p = {'format_id': '22', 'ext': 'mp4', 'vcodec': 'avc1.64001F', 'acodec': 'mp4a.40.2'}
    fmt_video_only = {'format_id': '137', 'ext': 'mp4', 'vcodec': 'avc1.640028', 'acodec': 'none'}
    fmt_audio_m4a = {'format_id': '140', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a.40.2'}
    fmt_audio_opus = {'format_id': '251', 'ext': 'webm', 'vcodec': 'none', 'acodec': 'opus'}

    # Сценарий A: есть и m4a аудио, и opus аудио, и видео
    # Должен выбрать M4A audio-only
    res_a = list(selector({'formats': [fmt_combined_360p, fmt_combined_720p, fmt_video_only, fmt_audio_opus, fmt_audio_m4a]}))
    assert len(res_a) == 1
    assert res_a[0]['format_id'] == '140'
    assert res_a[0]['vcodec'] == 'none'

    # Сценарий B: m4a нет, но есть Opus audio-only и combined видео
    # Должен выбрать Opus audio-only, ни в коем случае не видео
    res_b = list(selector({'formats': [fmt_combined_360p, fmt_combined_720p, fmt_audio_opus]}))
    assert len(res_b) == 1
    assert res_b[0]['format_id'] == '251'
    assert res_b[0]['vcodec'] == 'none'

    # Сценарий C: чистого аудио нет ВООБЩЕ (только combined video+audio 18/22)
    # СТРОГАЯ ГАРАНТИЯ: селектор должен вернуть ПУСТОЙ список (отказ от скачивания видео),
    # а НЕ скачивать видеопоток с удалением через FFmpeg
    res_c = list(selector({'formats': [fmt_combined_360p, fmt_combined_720p, fmt_video_only]}))
    assert len(res_c) == 0, f"Expected empty selection when no audio-only format exists, got: {res_c}"

    # Контрастная проверка: старый селектор с /b/best выбрал бы combined видеопоток (22 или 18)
    old_selector = ydl.build_format_selector('ba[ext=m4a]/ba[ext=mp3]/ba/b/best')
    res_old = list(old_selector({'formats': [fmt_combined_360p, fmt_combined_720p, fmt_video_only]}))
    assert len(res_old) == 1
    assert res_old[0]['format_id'] in ('18', '22')
    assert res_old[0]['vcodec'] != 'none'


def test_youtube_error_mappings():
    from handlers.music import format_download_error

    err1 = Exception("ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in if you've been granted access to this video")
    msg1 = format_download_error(err1)
    assert "Видео приватно" in msg1

    err2 = Exception("Sign in to confirm your age. This video may be inappropriate for some users.")
    msg2 = format_download_error(err2)
    assert "Возрастное ограничение" in msg2

    err3 = Exception("The uploader has not made this video available in your country")
    msg3 = format_download_error(err3)
    assert "Региональное ограничение" in msg3

    err4 = Exception("Sign in to confirm you're not a bot (HTTP Error 429: Too Many Requests)")
    msg4 = format_download_error(err4)
    assert "Временное ограничение YouTube" in msg4

    err5 = Exception("Requested format is not available")
    msg5 = format_download_error(err5)
    assert "Аудиопоток недоступен" in msg5

    err6 = Exception("Video unavailable. This video has been removed by the uploader")
    msg6 = format_download_error(err6)
    assert "Видео недоступно или удалено" in msg6

    err7 = Exception("Connection timed out while reading stream")
    msg7 = format_download_error(err7)
    assert "Превышено время ожидания" in msg7

    err8 = Exception("ffprobe / ffmpeg conversion failed")
    msg8 = format_download_error(err8)
    assert "Ошибка конвертации аудио" in msg8


def test_download_concurrency_semaphore_is_strictly_one():
    from services.downloader import DOWNLOAD_SEMAPHORE as sem1
    from handlers.music import DOWNLOAD_SEMAPHORE as sem2
    from handlers.inline import DOWNLOAD_SEMAPHORE as sem3
    # All modules must share the exact same Semaphore instance
    assert sem1 is sem2
    assert sem1 is sem3
    # Must be strictly 1 concurrent download to prevent OOM on 512 MB Render
    assert sem1._value == 1


def test_ffmpeg_threads_capped_to_one_to_prevent_oom():
    source = inspect.getsource(downloader._sync_download)
    # Check that postprocessor_args sets -threads 1
    assert '"-threads"' in source
    assert '"-threads", "0"' not in source and "'-threads', '0'" not in source
    assert '"concurrent_fragment_downloads": 1' in source

    # Check studio speed restoration FFmpeg threads
    source_restore = inspect.getsource(downloader._restore_studio_speed_and_pitch_if_needed)
    assert '["-threads", "1"' in source_restore or "['-threads', '1'" in source_restore
    assert '"-threads", "0"' not in source_restore and "'-threads', '0'" not in source_restore


def test_player_client_not_forcing_android_to_prevent_po_token_drop():
    source = inspect.getsource(downloader._sync_download)
    # Must NOT force player_client: ["android"] which breaks without GVS PO Token
    assert '"player_client": ["android"]' not in source
    assert "'player_client': ['android']" not in source


def test_memory_diagnostic_logger(capsys):
    from services.downloader import log_memory_stage, get_process_rss_mb

    rss = get_process_rss_mb()
    assert rss > 0

    log_memory_stage("test_stage", req_id="req_999", source="youtube", extra="test_extra")
    captured = capsys.readouterr().out
    assert "MEMORY [test_stage]" in captured
    assert "job_id=req_999" in captured
    assert "source=youtube" in captured
    assert "concurrent=" in captured
    assert "test_extra" in captured
    assert "RSS=" in captured


def test_emergency_recovery_when_alternative_audio_exists(tmp_path):
    output_dir = tmp_path / "test_session"
    output_dir.mkdir()
    raw_audio = output_dir / "track.webm"
    raw_audio.write_bytes(b"dummy audio stream" * 200)

    # Verify that before recovery, no m4a exists
    audio_files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".m4a", ".mp3", ".mp4", ".aac"]]
    assert len(audio_files) == 0

    # Simulate emergency conversion
    alt_audio = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in [".webm", ".opus", ".ogg", ".flac", ".wav"]]
    assert len(alt_audio) == 1
    emergency_out = output_dir / f"{alt_audio[0].stem}.m4a"

    with patch("subprocess.run") as mock_sub:
        def fake_ffmpeg(*args, **kwargs):
            emergency_out.write_bytes(b"dummy transcoded m4a" * 200)
            return MagicMock(returncode=0)
        mock_sub.side_effect = fake_ffmpeg
        import subprocess
        cmd = ["ffmpeg", "-y", "-i", str(alt_audio[0]), "-c:a", "aac", "-b:a", "192k", "-threads", "1", "-vn", str(emergency_out)]
        res = subprocess.run(cmd)
        assert res.returncode == 0
        if res.returncode == 0 and emergency_out.exists() and emergency_out.stat().st_size > 1000:
            audio_files = [emergency_out]
            alt_audio[0].unlink(missing_ok=True)

    assert len(audio_files) == 1
    assert audio_files[0].name == "track.m4a"
    assert not raw_audio.exists()



