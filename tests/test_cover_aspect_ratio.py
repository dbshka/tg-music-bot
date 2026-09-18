import os
import io
import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from PIL import Image, ImageDraw

from services.downloader import (
    trim_letterbox_black_bars,
    make_square_cover,
    _convert_thumbnail_to_jpg,
    _prepare_embedded_cover,
    _apply_custom_metadata,
)


def test_square_cover_1000x800_without_black_borders():
    """
    1000x800 -> квадрат 800x800 (без max_side) или 320x320 (с max_side=320)
    без добавления искусственных черных рамок и без растяжения пропорций.
    """
    img = Image.new("RGB", (1000, 800), color=(240, 180, 120))
    
    # 1. Без ограничения max_side -> строго 800x800
    sq_raw = make_square_cover(img, max_side=None)
    assert sq_raw.size == (800, 800)
    pix_raw = sq_raw.load()
    assert pix_raw[0, 0] == (240, 180, 120)
    assert pix_raw[799, 799] == (240, 180, 120)

    # 2. С ограничением max_side=320 -> строго 320x320
    sq_capped = make_square_cover(img, max_side=320)
    assert sq_capped.size == (320, 320)
    pix_cap = sq_capped.load()
    assert pix_cap[0, 0] == (240, 180, 120)
    assert pix_cap[319, 319] == (240, 180, 120)


def test_square_cover_800x1000_without_black_borders():
    """
    800x1000 -> квадрат 800x800 (без max_side) или 320x320 (с max_side=320)
    без добавления искусственных черных рамок.
    """
    img = Image.new("RGB", (800, 1000), color=(120, 180, 240))
    
    sq_raw = make_square_cover(img, max_side=None)
    assert sq_raw.size == (800, 800)
    pix_raw = sq_raw.load()
    assert pix_raw[0, 0] == (120, 180, 240)
    assert pix_raw[799, 799] == (120, 180, 240)

    sq_capped = make_square_cover(img, max_side=320)
    assert sq_capped.size == (320, 320)
    pix_cap = sq_capped.load()
    assert pix_cap[0, 0] == (120, 180, 240)
    assert pix_cap[319, 319] == (120, 180, 240)


def test_square_cover_1000x1000_no_aspect_distortion():
    """
    1000x1000 -> квадрат без изменений пропорций (1000x1000 или 320x320).
    Геометрический тест: круг в центре остается идеальным кругом (D_x == D_y).
    """
    img = Image.new("RGB", (1000, 1000), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Рисуем круг в центре (500, 500), радиус 300
    draw.ellipse((200, 200, 800, 800), fill=(255, 0, 0))

    sq_raw = make_square_cover(img, max_side=None)
    assert sq_raw.size == (1000, 1000)

    pix = sq_raw.load()
    x_coords = [x for x in range(1000) if pix[x, 500] == (255, 0, 0)]
    d_x = max(x_coords) - min(x_coords) + 1

    y_coords = [y for y in range(1000) if pix[500, y] == (255, 0, 0)]
    d_y = max(y_coords) - min(y_coords) + 1

    assert d_x == 601
    assert d_y == 601
    assert d_x == d_y  # Пропорции строго сохранены 1:1


def test_trim_letterbox_cases_a_b_c_d_e():
    """
    Synthetic tests для всех 5 сценариев из спецификации:
    Case A: 480x360 + симметричные чёрные полосы сверху/снизу -> удалить.
    Case B: 1000x800 + настоящая чёрная рамка как часть artwork -> не удалять автоматически.
    Case C: 1000x800 + чёрная ночь/тёмное небо только сверху -> не удалять.
    Case D: 1000x800 + симметричная тёмная декоративная область сверху/снизу -> проверить, что алгоритм не делает агрессивный crop.
    Case E: квадратная официальная обложка 1000x1000 с чёрными краями -> полностью сохранить.
    """
    # Case A: 480x360 YouTube letterbox (45px black top, 45px black bottom)
    im_a = Image.new("RGB", (480, 360), color=(0, 0, 0))
    ImageDraw.Draw(im_a).rectangle((0, 45, 480, 314), fill=(200, 100, 50))
    trimmed_a = trim_letterbox_black_bars(im_a)
    assert trimmed_a.size == (480, 270)  # Полосы удалены!
    sq_a = make_square_cover(im_a, max_side=320)
    assert sq_a.size == (320, 320)  # Кадрирован и приведен к 320x320

    # Case B: 1000x800 + настоящая черная рамка как часть artwork (50px черная рамка со всех сторон)
    im_b = Image.new("RGB", (1000, 800), color=(0, 0, 0))
    ImageDraw.Draw(im_b).rectangle((50, 50, 950, 750), fill=(220, 180, 140))
    trimmed_b = trim_letterbox_black_bars(im_b)
    assert trimmed_b.size == (1000, 800)  # НЕ удалять автоматически!

    # Case C: 1000x800 + черная ночь / темное небо только сверху (100px черного неба сверху)
    im_c = Image.new("RGB", (1000, 800), color=(140, 200, 250))
    ImageDraw.Draw(im_c).rectangle((0, 0, 1000, 100), fill=(0, 0, 0))
    trimmed_c = trim_letterbox_black_bars(im_c)
    assert trimmed_c.size == (1000, 800)  # НЕ удалять!

    # Case D: 1000x800 + симметричная темная декоративная область сверху/снизу
    im_d = Image.new("RGB", (1000, 800), color=(250, 230, 200))
    ImageDraw.Draw(im_d).rectangle((0, 0, 1000, 60), fill=(10, 10, 10))
    ImageDraw.Draw(im_d).rectangle((0, 740, 1000, 800), fill=(10, 10, 10))
    trimmed_d = trim_letterbox_black_bars(im_d)
    assert trimmed_d.size == (1000, 800)  # Нет агрессивного crop!

    # Case E: квадратная официальная обложка 1000x1000 с черными краями
    im_e = Image.new("RGB", (1000, 1000), color=(0, 0, 0))
    ImageDraw.Draw(im_e).rectangle((100, 100, 900, 900), fill=(255, 255, 255))
    trimmed_e = trim_letterbox_black_bars(im_e)
    assert trimmed_e.size == (1000, 1000)  # Полностью сохранить без изменений!


def test_youtube_letterbox_black_bars_trimming_case_a():
    """
    Case A: Настоящий YouTube letterbox 480x360 (black top, active, black bot).
    Проверяет, что полосы леттербоксинга аккуратно срезаются.
    """
    img = Image.new("RGB", (480, 360), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 45, 480, 314), fill=(200, 100, 50))

    trimmed = trim_letterbox_black_bars(img)
    assert trimmed.size[1] == 270

    sq = make_square_cover(img, max_side=320)
    assert sq.size == (320, 320)
    sq_pix = sq.load()
    assert sq_pix[sq.size[0] // 2, 5] == (200, 100, 50)
    assert sq_pix[sq.size[0] // 2, sq.size[1] - 5] == (200, 100, 50)


def test_official_artwork_cases_b_c_e_protected():
    """
    Защита официальных обложек:
    Case B: Официальная квадратная обложка 1000x1000 с темным дизайном по краям.
    Case C: Официальная обложка с темным небом только сверху (асимметрия).
    Case E: Арт 1000x800 с черными полями.
    """
    # Case B: 1000x1000 с черными полями
    im_b = Image.new("RGB", (1000, 1000), color=(0, 0, 0))
    ImageDraw.Draw(im_b).rectangle((0, 100, 1000, 900), fill=(255, 255, 255))
    trimmed_b = trim_letterbox_black_bars(im_b)
    assert trimmed_b.size == (1000, 1000)

    # Case C: 1000x800 только верх темный
    im_c = Image.new("RGB", (1000, 800), color=(150, 200, 250))
    ImageDraw.Draw(im_c).rectangle((0, 0, 1000, 100), fill=(0, 0, 0))
    trimmed_c = trim_letterbox_black_bars(im_c)
    assert trimmed_c.size == (1000, 800)

    # Case E: 1000x1000 полностью сохраняется
    im_e = Image.new("RGB", (1000, 1000), color=(0, 0, 0))
    trimmed_e = trim_letterbox_black_bars(im_e)
    assert trimmed_e.size == (1000, 1000)


def test_convert_thumbnail_to_jpg_creates_320x320_file(tmp_path):
    """
    Проверяет, что _convert_thumbnail_to_jpg сохраняет strictly 320x320 JPEG файл на диске (< 200 KB).
    """
    source_png = tmp_path / "raw_thumb.png"
    img = Image.new("RGB", (1280, 720), color=(100, 150, 200))
    img.save(source_png, "PNG")

    target_jpg = _convert_thumbnail_to_jpg(source_png)
    assert target_jpg is not None
    assert target_jpg.exists()
    assert target_jpg.suffix.lower() == ".jpg"
    assert target_jpg.stat().st_size < 200 * 1024  # < 200 KB

    with Image.open(target_jpg) as out_img:
        assert out_img.format == "JPEG"
        assert out_img.size == (320, 320)  # Строго 320x320 под Telegram Bot API!


def test_convert_thumbnail_to_jpg_youtube_hqdefault_removes_borders(tmp_path):
    """
    Проверяет, что _convert_thumbnail_to_jpg для YouTube hqdefault (480x360 с черными полосами)
    создает чистый 320x320 JPEG без черных полос сверху и снизу.
    """
    source_yt = tmp_path / "hqdefault.jpg"
    img = Image.new("RGB", (480, 360), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 45, 480, 314), fill=(180, 220, 250))
    img.save(source_yt, "JPEG")

    res_jpg = _convert_thumbnail_to_jpg(source_yt)
    assert res_jpg is not None
    assert res_jpg.exists()
    assert res_jpg.stat().st_size < 200 * 1024

    with Image.open(res_jpg) as out_im:
        assert out_im.size == (320, 320)
        pix = out_im.load()
        top_pix = pix[160, 5]
        bot_pix = pix[160, 315]
        # Из-за JPEG сжатия на резких границах допускаем небольшую погрешность
        assert all(abs(c1 - c2) <= 15 for c1, c2 in zip(top_pix, (180, 220, 250)))
        assert all(abs(c1 - c2) <= 15 for c1, c2 in zip(bot_pix, (180, 220, 250)))
        assert sum(top_pix) > 500
        assert sum(bot_pix) > 500


def test_id3_apic_preserves_high_resolution_studio_artwork(tmp_path):
    """
    Проверяет архитектурное разделение:
    1. Исходная студийная обложка 1000x1000 сохраняется в полном разрешении в ID3 APIC (без сжатия до 320px).
    2. Telegram thumbnail формируется как отдельный компактный файл <= 320x320 (< 200 KB).
    """
    import mutagen.id3

    # 1. Создаем исходную студийную обложку 1000x1000
    orig_cover = tmp_path / "studio_cover.jpg"
    img = Image.new("RGB", (1000, 1000), color=(50, 100, 150))
    img.save(orig_cover, "JPEG")

    # 2. Подготавливаем embedded cover и Telegram thumbnail
    embedded_cover = _prepare_embedded_cover(orig_cover)
    tg_thumb = _convert_thumbnail_to_jpg(orig_cover)

    assert embedded_cover is not None
    assert tg_thumb is not None

    # Проверяем размеры файлов и геометрию
    with Image.open(embedded_cover) as emb_im:
        assert emb_im.size == (1000, 1000)  # Полноразмерное студийное качество сохранено!
    with Image.open(tg_thumb) as tg_im:
        assert tg_im.size == (320, 320)  # Сжато строго под спецификацию Telegram Bot API!
    assert tg_thumb.stat().st_size < 200 * 1024  # < 200 KB

    # 3. Создаем минимальный валидный MP3 файл и вшиваем метаданные
    mp3_path = tmp_path / "test_track.mp3"
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 414
    mp3_path.write_bytes(frame * 10)

    _apply_custom_metadata(
        audio_path=mp3_path,
        title="Test Title",
        artist="Test Artist",
        cover_path=embedded_cover,
        album="Test Album"
    )

    # 4. Проверяем тег ID3 APIC напрямую через mutagen
    tags = mutagen.id3.ID3(mp3_path)
    apic_frames = tags.getall("APIC")
    assert len(apic_frames) > 0, "ID3 APIC frame отсутствует в MP3!"

    apic = apic_frames[0]
    assert apic.mime == "image/jpeg"
    with Image.open(io.BytesIO(apic.data)) as apic_img:
        assert apic_img.format == "JPEG"
        # КРИТИЧНО: вшитая в MP3 обложка имеет полное студийное разрешение 1000x1000!
        assert apic_img.size == (1000, 1000)
        assert apic_img.size != (320, 320)



@pytest.mark.asyncio
async def test_inline_prioritizes_canonical_studio_cover():
    """
    Проверяет приоритет обложек в Inline Mode:
    authoritative/canonical studio cover (Deezer/iTunes 1000x1000) > YouTube thumbnail fallback.
    """
    from handlers.inline import process_inline_download
    from services.database import save_inline_candidate
    from services.extractor import ExtractedTrack

    u_id = uuid.uuid4().hex[:8]
    cand_id = f"cand_test_{u_id}"
    test_target = f"https://www.youtube.com/watch?v=unique_{u_id}"
    test_title = f"UniqueTrack_{u_id}"
    test_artist = f"UniqueArtist_{u_id}"

    save_inline_candidate(
        cand_id=cand_id,
        target=test_target,
        title=test_title,
        artist=test_artist,
        album=None,
        duration=200,
        thumbnail_url=f"https://i.ytimg.com/vi/unique_{u_id}/hqdefault.jpg"
    )

    mock_canonical = ExtractedTrack(
        platform="Canonical/Deezer",
        target=f"ytsearch5:{test_artist} - {test_title}",
        is_search=True,
        title=test_title,
        artist=test_artist,
        thumbnail_url="https://cdn-images.dzcdn.net/images/cover/studio_art/1000x1000.jpg",
        duration=200
    )

    mock_bot = AsyncMock()
    mock_audio = MagicMock()
    mock_audio.file_id = "BQACAgQAAxkBAAICaW_valid_length_telegram_file_id_99999"
    mock_msg = MagicMock()
    mock_msg.audio = mock_audio
    mock_bot.send_audio = AsyncMock(return_value=mock_msg)
    mock_bot.edit_message_media = AsyncMock()

    mock_dl = MagicMock()
    mock_dl.file_path = Path("fake.mp3")
    mock_dl.title = "UniqueTrack"
    mock_dl.artist = "UniqueArtist"
    mock_dl.duration = 200
    mock_dl.thumbnail_path = Path("fake_thumb.jpg")

    with patch("handlers.inline.resolve_canonical_track_info_async", AsyncMock(return_value=mock_canonical)) as mock_canon:
        with patch("handlers.inline.download_track", AsyncMock(return_value=mock_dl)) as mock_dl_func:
            with patch.object(Path, "exists", return_value=True):
                await process_inline_download(
                    cand_id=cand_id,
                    inline_message_id="inl_msg_test_canon",
                    bot=mock_bot
                )

                mock_canon.assert_awaited_once()
                mock_dl_func.assert_awaited_once()
                _, dl_kwargs = mock_dl_func.call_args
                # download_track получил официальную 1000x1000 студийную обложку вместо YouTube hqdefault!
                assert dl_kwargs["thumbnail_url"] == "https://cdn-images.dzcdn.net/images/cover/studio_art/1000x1000.jpg"
