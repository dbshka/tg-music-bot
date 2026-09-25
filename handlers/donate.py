import html
import logging
from pathlib import Path
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)

from config import BASE_DIR

logger = logging.getLogger(__name__)

router = Router(name="donate_router")

CRYPTO_DIR = BASE_DIR / "assets" / "crypto"

CRYPTO_WALLETS = {
    "trc20": {
        "title": "USDT TRC20",
        "address": "TYKtkfee4rAsb6NpnbnaVq6MMykQ1KzDFH",
        "image_file": CRYPTO_DIR / "trc20.jpg",
    },
    "bnb": {
        "title": "BNB",
        "address": "0x118721e3E849478489b2DF0C92F01171E6b0a268",
        "image_file": CRYPTO_DIR / "bnb.jpg",
    },
    "btc": {
        "title": "Bitcoin",
        "address": "bc1qlm9dm3kqqwdehpstkj3uv095g7srpd3c0yuarz",
        "image_file": CRYPTO_DIR / "btc.jpg",
    },
}

DONATE_INTRO_TEXT = (
    "<b>Поддержать проект</b>\n\n"
    "Если бот оказался полезен, его можно поддержать криптовалютой."
)


def get_donate_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура со списком криптовалют для пожертвования."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="USDT TRC20", callback_data="donate:trc20")],
            [InlineKeyboardButton(text="BNB", callback_data="donate:bnb")],
            [InlineKeyboardButton(text="Bitcoin", callback_data="donate:btc")],
        ]
    )


def get_crypto_back_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-кнопка возврата в меню выбора валют."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="donate:menu")],
        ]
    )


def format_wallet_caption(wallet: dict) -> str:
    """Форматирует карточку с реквизитами кошелька для пользователя."""
    return (
        f"<b>{wallet['title']}</b>\n\n"
        f"Адрес:\n"
        f"<code>{wallet['address']}</code>"
    )


@router.message(Command("donate"))
@router.message(F.text.in_({"Поддержать проект", "Поддержать автора"}))
async def cmd_donate(message: Message, state: Optional[FSMContext] = None):
    """Показывает главное меню раздела поддержки проекта криптовалютой."""
    if state:
        await state.clear()
    await message.answer(
        text=DONATE_INTRO_TEXT,
        parse_mode="HTML",
        reply_markup=get_donate_inline_keyboard()
    )


@router.callback_query(F.data.startswith("donate:"))
async def on_donate_callback(callback: CallbackQuery):
    """Обрабатывает навигацию внутри раздела донатов (выбор валюты и кнопку Назад)."""
    await callback.answer()
    data = callback.data.split(":", 1)[1]

    if data == "menu":
        try:
            if callback.message:
                await callback.message.delete()
        except Exception:
            pass

        if callback.message:
            await callback.message.answer(
                text=DONATE_INTRO_TEXT,
                parse_mode="HTML",
                reply_markup=get_donate_inline_keyboard()
            )
        return

    wallet = CRYPTO_WALLETS.get(data)
    if not wallet or not callback.message:
        return

    caption = format_wallet_caption(wallet)
    img_path = wallet["image_file"]

    try:
        await callback.message.delete()
    except Exception:
        pass

    if img_path.exists():
        await callback.message.answer_photo(
            photo=FSInputFile(img_path),
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_crypto_back_keyboard()
        )
    else:
        logger.warning("QR-код для %s не найден по пути %s", wallet["title"], img_path)
        await callback.message.answer(
            text=caption,
            parse_mode="HTML",
            reply_markup=get_crypto_back_keyboard()
        )
