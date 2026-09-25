import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
from PIL import Image

from handlers.donate import (
    CRYPTO_WALLETS,
    DONATE_INTRO_TEXT,
    get_donate_inline_keyboard,
    get_crypto_back_keyboard,
    format_wallet_caption,
    cmd_donate,
    on_donate_callback,
)
from handlers.music import get_main_reply_keyboard, cmd_start, cmd_help
from config import BASE_DIR


def test_crypto_wallets_addresses():
    """
    Проверяет:
    1. Наличие всех трёх валют;
    2. Точное соответствие адресов без искажений и сокращений;
    3. Точные названия валют/сетей.
    """
    expected = {
        "trc20": {
            "title": "USDT TRC20",
            "address": "TYKtkfee4rAsb6NpnbnaVq6MMykQ1KzDFH",
            "filename": "trc20.jpg",
        },
        "bnb": {
            "title": "BNB",
            "address": "0x118721e3E849478489b2DF0C92F01171E6b0a268",
            "filename": "bnb.jpg",
        },
        "btc": {
            "title": "Bitcoin",
            "address": "bc1qlm9dm3kqqwdehpstkj3uv095g7srpd3c0yuarz",
            "filename": "btc.jpg",
        },
    }

    assert set(CRYPTO_WALLETS.keys()) == set(expected.keys())

    for key, data in expected.items():
        wallet = CRYPTO_WALLETS[key]
        assert wallet["title"] == data["title"]
        assert wallet["address"] == data["address"]
        assert wallet["image_file"].name == data["filename"]


def test_crypto_qr_images_exist_and_valid():
    """
    Проверяет наличие и валидность всех прикрепленных QR-изображений:
    1. Изображения существуют на диске в assets/crypto/;
    2. Размер файлов адекватный (не огромные и не пустые);
    3. Изображения читаются как валидные картинки.
    """
    crypto_dir = BASE_DIR / "assets" / "crypto"
    assert crypto_dir.exists() and crypto_dir.is_dir()

    for key, wallet in CRYPTO_WALLETS.items():
        img_file = wallet["image_file"]
        assert img_file.exists(), f"QR-файл {img_file} не найден!"
        sz = img_file.stat().st_size
        assert sz > 1000, f"Файл {img_file.name} слишком мал ({sz} байт)"
        assert sz < 200_000, f"Файл {img_file.name} слишком большой ({sz} байт)"

        # Проверка целостности картинки через Pillow
        with Image.open(img_file) as im:
            assert im.size[0] >= 320 and im.size[1] >= 320
            assert im.format in ("JPEG", "PNG")


def test_donate_keyboards_structure():
    """
    Проверяет кнопки и структуру клавиатур раздела донатов:
    1. get_main_reply_keyboard содержит 'Найти песню' и 'Поддержать проект';
    2. get_donate_inline_keyboard содержит 3 валюты с правильными callback_data;
    3. get_crypto_back_keyboard содержит кнопку '← Назад' с callback_data 'donate:menu'.
    """
    # 1. Reply-клавиатура (кнопка 'Поддержать проект' убрана с главного экрана)
    main_kb = get_main_reply_keyboard()
    button_texts = [btn.text for row in main_kb.keyboard for btn in row]
    assert "Найти песню" in button_texts
    assert "Поддержать проект" not in button_texts

    # 2. Главная inline-клавиатура донатов
    donate_kb = get_donate_inline_keyboard()
    inline_buttons = {btn.text: btn.callback_data for row in donate_kb.inline_keyboard for btn in row}
    assert inline_buttons.get("USDT TRC20") == "donate:trc20"
    assert inline_buttons.get("BNB") == "donate:bnb"
    assert inline_buttons.get("Bitcoin") == "donate:btc"

    # 3. Кнопка возврата
    back_kb = get_crypto_back_keyboard()
    back_buttons = {btn.text: btn.callback_data for row in back_kb.inline_keyboard for btn in row}
    assert back_buttons.get("← Назад") == "donate:menu"


def test_format_wallet_caption():
    """Проверяет форматирование карточки реквизитов (название, тег code для копирования)."""
    for key, wallet in CRYPTO_WALLETS.items():
        caption = format_wallet_caption(wallet)
        assert wallet["title"] in caption
        assert f"<code>{wallet['address']}</code>" in caption


@pytest.mark.asyncio
async def test_cmd_donate():
    """Проверяет отправку приветственного сообщения раздела донатов при вызове cmd_donate."""
    message = MagicMock()
    message.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()

    await cmd_donate(message, state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()
    call_kwargs = message.answer.call_args.kwargs
    assert call_kwargs.get("text") == DONATE_INTRO_TEXT
    assert call_kwargs.get("parse_mode") == "HTML"
    assert call_kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_on_donate_callback_currency_selection():
    """
    Проверяет переход по callback_data к конкретной валюте:
    удаление предыдущего сообщения и отправка фото с QR-кодом и адресом.
    """
    for currency_key in ["trc20", "bnb", "btc"]:
        callback = MagicMock()
        callback.data = f"donate:{currency_key}"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.delete = AsyncMock()
        callback.message.answer_photo = AsyncMock()
        callback.message.answer = AsyncMock()

        await on_donate_callback(callback)

        callback.answer.assert_awaited_once()
        callback.message.delete.assert_awaited_once()
        callback.message.answer_photo.assert_awaited_once()

        photo_call = callback.message.answer_photo.call_args
        caption = photo_call.kwargs.get("caption") or ""
        expected_addr = CRYPTO_WALLETS[currency_key]["address"]
        assert expected_addr in caption
        assert f"<code>{expected_addr}</code>" in caption


@pytest.mark.asyncio
async def test_on_donate_callback_back_navigation():
    """Проверяет работу кнопки '← Назад' (donate:menu): возврат в главное меню донатов."""
    callback = MagicMock()
    callback.data = "donate:menu"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.delete = AsyncMock()
    callback.message.answer = AsyncMock()

    await on_donate_callback(callback)

    callback.answer.assert_awaited_once()
    callback.message.delete.assert_awaited_once()
    callback.message.answer.assert_awaited_once()

    ans_kwargs = callback.message.answer.call_args.kwargs
    assert ans_kwargs.get("text") == DONATE_INTRO_TEXT


@pytest.mark.asyncio
async def test_main_menu_and_help_commands():
    """Проверяет отсутствие поломки существующего меню /start и /help."""
    # 1. /start
    msg_start = MagicMock()
    msg_start.chat = MagicMock(id=12345)
    msg_start.from_user = MagicMock(id=12345, username="tester", full_name="Tester")
    msg_start.answer = AsyncMock()
    state_start = MagicMock()
    state_start.clear = AsyncMock()

    await cmd_start(msg_start, state_start)
    msg_start.answer.assert_awaited_once()
    reply_kb = msg_start.answer.call_args.kwargs.get("reply_markup")
    assert reply_kb is not None
    button_texts = [btn.text for row in reply_kb.keyboard for btn in row]
    assert "Найти песню" in button_texts
    assert "Поддержать проект" not in button_texts

    # 2. /help
    msg_help = MagicMock()
    msg_help.answer = AsyncMock()
    state_help = MagicMock()
    state_help.clear = AsyncMock()

    await cmd_help(msg_help, state_help)
    msg_help.answer.assert_awaited_once()
    help_text = msg_help.answer.call_args.args[0]
    assert "/donate" in help_text
