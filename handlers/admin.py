import asyncio
import html
import logging
from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError

from config import ADMIN_ID
from services.database import get_bot_stats_async, get_all_broadcast_chat_ids_async

logger = logging.getLogger(__name__)
router = Router(name="admin_router")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    # Доступ только для администратора
    if message.from_user.id != ADMIN_ID:
        return

    stats = await get_bot_stats_async()

    # Топ пользователей по скачиваниям
    top_lines = []
    for i, u in enumerate(stats["top_users"], 1):
        uname = f"@{u['username']}" if u["username"] else "Без юзернейма"
        name = html.escape(u["full_name"] or "Пользователь")
        top_lines.append(
            f"{i}. <b>{name}</b> ({uname}, ID: <code>{u['user_id']}</code>) — "
            f"скачано: {u['downloads_count']}, изменено тегов: {u['tags_edited_count']}"
        )
    top_block = "\n".join(top_lines) if top_lines else "Активных пользователей пока нет."

    # Последние зарегистрированные
    recent_lines = []
    for i, u in enumerate(stats["recent_users"], 1):
        uname = f"@{u['username']}" if u["username"] else "Без юзернейма"
        name = html.escape(u["full_name"] or "Пользователь")
        reg_date = str(u["first_seen"]).split(".")[0] if u.get("first_seen") else "-"
        recent_lines.append(f"• <b>{name}</b> ({uname}) — <i>{reg_date}</i>")
    recent_block = "\n".join(recent_lines) if recent_lines else "Данных пока нет."

    text = (
        "<b>Статистика</b>\n\n"
        f"Всего пользователей: <code>{stats['total_users']}</code>\n"
        f"Активных сегодня: <code>{stats['active_today']}</code>\n"
        f"Активных за 7 дней: <code>{stats['active_week']}</code>\n"
        f"Скачано треков: <code>{stats['total_downloads']}</code>\n"
        f"Изменено тегов: <code>{stats['total_tags_edited']}</code>\n\n"
        f"Топ по активности:\n{top_block}\n\n"
        f"Последние пользователи:\n{recent_block}"
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
            "<b>Рассылка сообщений</b>\n\n"
            "Использование:\n"
            "<code>/broadcast Ваш текст сообщения</code>\n\n"
            "Можно отправлять обычный текст и HTML-теги: b, i, code.",
            parse_mode="HTML"
        )
        return

    broadcast_text = parts[1].strip()
    chat_ids = await get_all_broadcast_chat_ids_async()
    total_users = len(chat_ids)

    if total_users == 0:
        await message.answer("Пользователей для рассылки пока нет.")
        return

    progress_msg = await message.answer(
        f"Начинаю рассылку для {total_users} пользователей..."
    )

    sent_count = 0
    blocked_count = 0
    failed_count = 0

    for cid in chat_ids:
        try:
            await bot.send_message(chat_id=cid, text=broadcast_text, parse_mode="HTML")
            sent_count += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            blocked_count += 1
        except TelegramAPIError as e:
            err_msg_lower = str(e).lower()
            if "blocked" in err_msg_lower or "user is deactivated" in err_msg_lower or "chat not found" in err_msg_lower:
                blocked_count += 1
                continue
            # Если HTML-разметка оказалась невалидной, пробуем отправить обычным текстом без parse_mode
            try:
                await bot.send_message(chat_id=cid, text=broadcast_text, parse_mode=None)
                sent_count += 1
            except TelegramForbiddenError:
                blocked_count += 1
            except Exception as inner_err:
                logger.warning("Ошибка отправки рассылки пользователю %s: %s", cid, inner_err)
                if "blocked" in str(inner_err).lower() or "deactivated" in str(inner_err).lower():
                    blocked_count += 1
                else:
                    failed_count += 1
        except Exception as e:
            err_str_lower = str(e).lower()
            if "blocked" in err_str_lower or "forbidden" in err_str_lower or "deactivated" in err_str_lower:
                blocked_count += 1
            else:
                logger.error("Непредвиденная ошибка рассылки %s: %s", cid, e)
                failed_count += 1

        # Небольшая пауза для соблюдения лимитов Telegram (не более 30 сообщений в секунду)
        await asyncio.sleep(0.04)

    await progress_msg.edit_text(
        "<b>Рассылка завершена.</b>\n\n"
        f"Всего получателей: <b>{total_users}</b>\n"
        f"Доставлено: <b>{sent_count}</b>\n"
        f"Заблокировали бота: <b>{blocked_count}</b>\n"
        f"Ошибки отправки: <b>{failed_count}</b>",
        parse_mode="HTML"
    )

