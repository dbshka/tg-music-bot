import pytest
from pathlib import Path
from PIL import Image
from services.tag_editor import prepare_cover_image, apply_mp3_tags
from handlers.tag_editor import get_audio_edit_keyboard


def test_prepare_cover_image_square_crop(tmp_path):
    # Create rectangular image (e.g. 16:9 YouTube thumbnail 800x450)
    rect_img = tmp_path / "thumb.jpg"
    im = Image.new("RGB", (800, 450), color="blue")
    im.save(rect_img)

    out_img = tmp_path / "thumb_square.jpg"
    res = prepare_cover_image(rect_img, out_img)

    with Image.open(res) as square:
        w, h = square.size
        assert w == h, f"Image must be 1:1 square, got {w}x{h}"
        assert w <= 640


def test_m4a_no_id3_fallthrough(tmp_path):
    # If an M4A file is invalid or tagging fails, apply_mp3_tags must NEVER fall through to ID3!
    bad_m4a = tmp_path / "corrupted.m4a"
    bad_m4a.write_text("not a valid mp4 container")

    with pytest.raises(RuntimeError, match="Не удалось записать теги"):
        apply_mp3_tags(bad_m4a, title="Test", artist="Artist")


def test_owner_bound_keyboard():
    # Keyboard with owner
    kb = get_audio_edit_keyboard(12345678)
    button = kb.inline_keyboard[0][0]
    assert button.callback_data == "audio:edit:12345678"

    # Keyboard without owner
    kb_none = get_audio_edit_keyboard(None)
    button_none = kb_none.inline_keyboard[0][0]
    assert button_none.callback_data == "audio:edit"
