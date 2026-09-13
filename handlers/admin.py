import asyncio
import html
import logging
from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError

from config import ADMIN_ID
from services.database import get_bot_stats, get_all_user_ids

logger = logging.getLogger(__name__)
router = Router(name="admin_router")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    # Доступ только для администратора
    if message.from_user.id != ADMIN_ID:
        return

    stats = get_bot_stats()

    # Топ пользователей по скачиваниям
    top_lines = []
    for i, u in enumerate(stats["top_users"], 1):
        uname = f"@{u['username']}" if u["username"] else "Без юзернейма"
        name = html.escape(u["full_name"] or "Пользователь")
        top_lines.append(
            f"{i}. <b>{name}</b> ({uname}, ID: <code>{u['user_id']}</code>) — "
            f"📥 {u['downloads_count']} треков, ✏️ {u['tags_edited_count']} тегов"
        )
    top_block = "\n".join(top_lines) if top_lines else "<i>Пока нет активных пользователей</i>"

    # Последние зарегистрированные
    recent_lines = []
    for i, u in enumerate(stats["recent_users"], 1):
        uname = f"@{u['username']}" if u["username"] else "Без юзернейма"
        name = html.escape(u["full_name"] or "Пользователь")
        reg_date = str(u["first_seen"]).split(".")[0] if u.get("first_seen") else "-"
        recent_lines.append(f"• <b>{name}</b> ({uname}) — <i>{reg_date}</i>")
    recent_block = "\n".join(recent_lines) if recent_lines else "<i>Пока нет данных</i>"

    text = (
        "📊 <b>Статистика музыкального бота</b>\n\n"
        f"👥 <b>Всего пользователей:</b> <code>{stats['total_users']}</code>\n"
        f"🟢 <b>Активных сегодня:</b> <code>{stats['active_today']}</code>\n"
        f"📅 <b>Активных за 7 дней:</b> <code>{stats['active_week']}</code>\n"
        f"📥 <b>Всего скачано треков:</b> <code>{stats['total_downloads']}</code>\n"
        f"✏️ <b>Отредактировано тегов:</b> <code>{stats['total_tags_edited']}</code>\n\n"
        f"🏆 <b>Топ по активности:</b>\n{top_block}\n\n"
        f"🆕 <b>Последние пользователи:</b>\n{recent_block}"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    # Доступ только для администратора
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "📢 <b>Рассылка сообщений пользователям</b>\n\n"
            "Использование:\n"
            "<code>/broadcast Ваш текст сообщения</code>\n\n"
            "<i>Поддерживается обычный текст и HTML-теги (&lt;b&gt;, &lt;i&gt;, &lt;code&gt;).</i>",
            parse_mode="HTML"
        )
        return

    broadcast_text = parts[1].strip()
    user_ids = get_all_user_ids()
    total_users = len(user_ids)

    if total_users == 0:
        await message.answer("⚠️ В базе данных пока нет пользователей для рассылки.")
        return

    progress_msg = await message.answer(
        f"🚀 <b>Начинаю рассылку для {total_users} пользователей...</b>",
        parse_mode="HTML"
    )

    sent_count = 0
    blocked_count = 0
    failed_count = 0

    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=broadcast_text, parse_mode="HTML")
            sent_count += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            blocked_count += 1
        except TelegramAPIError as e:
            logger.warning("Ошибка отправки рассылки пользователю %s: %s", uid, e)
            failed_count += 1
        except Exception as e:
            logger.error("Непредвиденная ошибка рассылки %s: %s", uid, e)
            failed_count += 1

        # Небольшая пауза для соблюдения лимитов Telegram (не более 30 сообщений в секунду)
        await asyncio.sleep(0.04)

    await progress_msg.edit_text(
        "✅ <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего получателей: <b>{total_users}</b>\n"
        f"📨 Доставлено: <b>{sent_count}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked_count}</b>\n"
        f"⚠️ Ошибки отправки: <b>{failed_count}</b>",
        parse_mode="HTML"
    )

