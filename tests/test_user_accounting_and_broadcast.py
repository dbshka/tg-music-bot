import asyncio
import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError

import services.database as db
from services.database import (
    init_db,
    register_user,
    register_user_async,
    is_user_registered,
    is_user_registered_async,
    log_user_activity,
    log_user_activity_async,
    increment_user_download,
    increment_user_tag_edit,
    get_bot_stats,
    get_bot_stats_async,
    get_all_broadcast_chat_ids,
    get_all_broadcast_chat_ids_async,
)
from handlers.music import cmd_start
import handlers.admin as admin_handler
from handlers.admin import cmd_stats, cmd_broadcast


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Изолированная база данных SQLite для каждого теста."""
    test_db_path = tmp_path / "test_accounting.db"
    monkeypatch.setattr(db, "DB_PATH", test_db_path)
    init_db()
    return test_db_path


@pytest.fixture
def mock_admin():
    return 777000111


@pytest.mark.asyncio
async def test_1_new_user_start_registration_stats_broadcast():
    """
    Тест 1:
    Новый пользователь вызывает /start.
    Ожидается:
    - появляется одна запись в users;
    - пользователь считается зарегистрированным;
    - пользователь попадает в /stats;
    - пользователь попадает в список /broadcast.
    """
    user_id = 10001
    chat_id = 90001  # чат может отличаться от user_id
    username = "new_listener"
    full_name = "New Listener"

    message = AsyncMock(spec=Message)
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.from_user.username = username
    message.from_user.full_name = full_name
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.answer = AsyncMock()

    state = AsyncMock(spec=FSMContext)
    state.clear = AsyncMock()

    # Вызываем /start
    await cmd_start(message, state)

    # 1. Проверяем, что пользователь зарегистрирован
    assert is_user_registered(user_id) is True
    assert (await is_user_registered_async(user_id)) is True

    # 2. Проверяем данные в БД
    with db.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row["user_id"] == user_id
        assert row["chat_id"] == chat_id
        assert row["username"] == username
        assert row["full_name"] == full_name
        assert row["downloads_count"] == 0
        assert row["tags_edited_count"] == 0

    # 3. Пользователь виден в /stats
    stats = await get_bot_stats_async()
    assert stats["total_users"] == 1
    assert stats["active_today"] == 1
    assert len(stats["recent_users"]) == 1
    assert stats["recent_users"][0]["user_id"] == user_id

    # 4. Пользователь в списке /broadcast по chat_id
    chat_ids = await get_all_broadcast_chat_ids_async()
    assert chat_ids == [chat_id]


@pytest.mark.asyncio
async def test_2_duplicate_start_does_not_increase_total_users():
    """
    Тест 2:
    Тот же пользователь вызывает /start второй раз.
    Ожидается:
    - количество пользователей не увеличивается;
    - дубликат не создаётся;
    - метаданные обновляются.
    """
    user_id = 10002
    chat_id = 90002

    message = AsyncMock(spec=Message)
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.from_user.username = "user_v1"
    message.from_user.full_name = "User V1"
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.answer = AsyncMock()

    state = AsyncMock(spec=FSMContext)
    state.clear = AsyncMock()

    # Первый вызов
    await cmd_start(message, state)
    stats_1 = get_bot_stats()
    assert stats_1["total_users"] == 1

    # Второй вызов с обновленным профилем
    message.from_user.username = "user_v2"
    message.from_user.full_name = "User V2 (Renamed)"
    await cmd_start(message, state)

    # Проверяем: пользователей всё ещё 1, дубликатов нет
    stats_2 = get_bot_stats()
    assert stats_2["total_users"] == 1

    with db.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE user_id = ?", (user_id,))
        count = cursor.fetchone()[0]
        assert count == 1

        cursor.execute("SELECT username, full_name FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        assert row["username"] == "user_v2"
        assert row["full_name"] == "User V2 (Renamed)"


def test_3_user_registered_month_ago_remains_in_total_users_and_broadcast():
    """
    Тест 3:
    Пользователь зарегистрирован месяц назад и больше ничего не делал.
    Ожидается:
    - он остаётся в total_users;
    - он попадает в /broadcast.
    """
    user_id = 10003
    chat_id = 90003
    month_ago = datetime.datetime.now() - datetime.timedelta(days=30)

    with db.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, chat_id, username, full_name, first_seen, last_seen, downloads_count, tags_edited_count)
            VALUES (?, ?, 'old_user', 'Old User', ?, ?, 0, 0)
        """, (user_id, chat_id, month_ago, month_ago))
        conn.commit()

    stats = get_bot_stats()
    assert stats["total_users"] == 1
    assert stats["active_today"] == 0
    assert stats["active_week"] == 0

    broadcast_recipients = get_all_broadcast_chat_ids()
    assert chat_id in broadcast_recipients


def test_4_user_active_today_counted_in_active_today_without_duplicate():
    """
    Тест 4:
    Пользователь активен сегодня.
    Ожидается:
    - он учитывается в active_today;
    - это не создаёт дополнительную регистрацию (total_users = 1).
    """
    user_id = 10004
    chat_id = 90004
    reg_date = datetime.datetime.now() - datetime.timedelta(days=10)
    register_user(user_id=user_id, chat_id=chat_id, username="active_user", full_name="Active User")

    # Искусственно сдвигаем first_seen назад
    with db.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET first_seen = ? WHERE user_id = ?", (reg_date, user_id))
        conn.commit()

    # Пользователь проявляет активность сегодня
    log_user_activity(user_id=user_id)

    stats = get_bot_stats()
    assert stats["total_users"] == 1
    assert stats["active_today"] == 1
    assert stats["active_week"] == 1


def test_5_user_inactive_long_ago_remains_registered_and_receives_broadcast():
    """
    Тест 5:
    Пользователь неактивен давно.
    Ожидается:
    - он остаётся зарегистрированным;
    - он не попадает в active_today и active_week;
    - он всё равно попадает в /broadcast.
    """
    user_id = 10005
    chat_id = 90005
    inactive_date = datetime.datetime.now() - datetime.timedelta(days=45)

    with db.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, chat_id, username, full_name, first_seen, last_seen, downloads_count, tags_edited_count)
            VALUES (?, ?, 'inactive', 'Inactive User', ?, ?, 5, 1)
        """, (user_id, chat_id, inactive_date, inactive_date))
        conn.commit()

    assert is_user_registered(user_id) is True
    stats = get_bot_stats()
    assert stats["total_users"] == 1
    assert stats["active_today"] == 0
    assert stats["active_week"] == 0
    # Сохраняется в топе активности благодаря историческим скачиваниям
    assert len(stats["top_users"]) == 1
    assert stats["top_users"][0]["user_id"] == user_id

    # Получает рассылку
    assert chat_id in get_all_broadcast_chat_ids()


@pytest.mark.asyncio
async def test_6_broadcast_continues_on_telegram_api_error(monkeypatch, mock_admin):
    """
    Тест 6:
    При отправке одному пользователю Telegram возвращает ошибку.
    Ожидается:
    - этот пользователь попадает в ошибочную категорию (failed_count);
    - рассылка продолжает работать для остальных пользователей.
    """
    monkeypatch.setattr(admin_handler, "ADMIN_ID", mock_admin)

    # 3 пользователя
    register_user(101, 101, "u1", "U1")
    register_user(102, 102, "u2", "U2")
    register_user(103, 103, "u3", "U3")

    bot = AsyncMock()

    async def mock_send_message(chat_id, text, parse_mode=None):
        if chat_id == 102:
            raise TelegramAPIError(method="sendMessage", message="Bad Gateway 502")
        return MagicMock()

    bot.send_message.side_effect = mock_send_message

    admin_msg = AsyncMock(spec=Message)
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = mock_admin
    admin_msg.text = "/broadcast Тестовое оповещение"
    progress_mock = AsyncMock()
    admin_msg.answer = AsyncMock(return_value=progress_mock)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await cmd_broadcast(admin_msg, bot)

    # Убеждаемся, что bot.send_message был вызван для всех 3 пользователей
    sent_cids = [call.kwargs.get("chat_id") for call in bot.send_message.call_args_list]
    assert 101 in sent_cids
    assert 102 in sent_cids
    assert 103 in sent_cids

    # Проверяем отчет рассылки
    progress_mock.edit_text.assert_called_once()
    report_text = progress_mock.edit_text.call_args[0][0]
    assert "Всего получателей: <b>3</b>" in report_text
    assert "Доставлено: <b>2</b>" in report_text
    assert "Ошибки отправки: <b>1</b>" in report_text
    assert "Заблокировали бота: <b>0</b>" in report_text


@pytest.mark.asyncio
async def test_7_broadcast_handles_blocked_user_forbidden_error(monkeypatch, mock_admin):
    """
    Тест 7:
    Пользователь заблокировал бота (TelegramForbiddenError).
    Ожидается:
    - отправка считается неуспешной;
    - пользователь учитывается в blocked_count;
    - остальные пользователи получают сообщение.
    """
    monkeypatch.setattr(admin_handler, "ADMIN_ID", mock_admin)

    register_user(201, 201, "u1", "U1")
    register_user(202, 202, "u2", "U2")
    register_user(203, 203, "u3", "U3")

    bot = AsyncMock()

    async def mock_send_message(chat_id, text, parse_mode=None):
        if chat_id == 201:
            raise TelegramForbiddenError(method="sendMessage", message="Forbidden: bot was blocked by the user")
        return MagicMock()

    bot.send_message.side_effect = mock_send_message

    admin_msg = AsyncMock(spec=Message)
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = mock_admin
    admin_msg.text = "/broadcast Тест блокировки"
    progress_mock = AsyncMock()
    admin_msg.answer = AsyncMock(return_value=progress_mock)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await cmd_broadcast(admin_msg, bot)

    # Все получатели были опрошены
    assert bot.send_message.call_count == 3

    # Отчет
    progress_mock.edit_text.assert_called_once()
    report_text = progress_mock.edit_text.call_args[0][0]
    assert "Всего получателей: <b>3</b>" in report_text
    assert "Доставлено: <b>2</b>" in report_text
    assert "Заблокировали бота: <b>1</b>" in report_text
    assert "Ошибки отправки: <b>0</b>" in report_text


@pytest.mark.asyncio
async def test_8_broadcast_targets_all_10_users_regardless_of_activity(monkeypatch, mock_admin):
    """
    Тест 8:
    В БД 10 зарегистрированных пользователей с разной активностью.
    Ожидается:
    - /broadcast пытается отправить сообщение всем 10;
    - активность каждого пользователя не влияет на число получателей.
    """
    monkeypatch.setattr(admin_handler, "ADMIN_ID", mock_admin)

    now = datetime.datetime.now()
    # Создаем 10 пользователей с кардинально разным профилем активности
    for i in range(1, 11):
        uid = 3000 + i
        cid = 4000 + i
        # 1-3 активны сегодня, 4-6 активны неделю назад, 7-10 неактивны месяц назад
        days_ago = 0 if i <= 3 else (5 if i <= 6 else 40)
        seen_dt = now - datetime.timedelta(days=days_ago)
        d_count = i * 2 if i % 2 == 0 else 0

        with db.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, chat_id, username, full_name, first_seen, last_seen, downloads_count, tags_edited_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """, (uid, cid, f"user_{i}", f"User {i}", seen_dt, seen_dt, d_count))
            conn.commit()

    all_chat_ids = get_all_broadcast_chat_ids()
    assert len(all_chat_ids) == 10

    bot = AsyncMock()
    admin_msg = AsyncMock(spec=Message)
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = mock_admin
    admin_msg.text = "/broadcast Всем привет!"
    progress_mock = AsyncMock()
    admin_msg.answer = AsyncMock(return_value=progress_mock)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await cmd_broadcast(admin_msg, bot)

    assert bot.send_message.call_count == 10
    called_chat_ids = [call.kwargs.get("chat_id") for call in bot.send_message.call_args_list]
    for i in range(1, 11):
        assert (4000 + i) in called_chat_ids

    report_text = progress_mock.edit_text.call_args[0][0]
    assert "Всего получателей: <b>10</b>" in report_text
    assert "Доставлено: <b>10</b>" in report_text


@pytest.mark.asyncio
async def test_cmd_stats_displays_metrics_correctly(monkeypatch, mock_admin):
    """
    Проверяет обработчик /stats: вывод общего числа пользователей, активностей и топа.
    """
    monkeypatch.setattr(admin_handler, "ADMIN_ID", mock_admin)

    register_user(501, 501, "u501", "User 501")
    increment_user_download(501)
    increment_user_tag_edit(501)

    admin_msg = AsyncMock(spec=Message)
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = mock_admin
    admin_msg.answer = AsyncMock()

    await cmd_stats(admin_msg)
    admin_msg.answer.assert_called_once()
    stats_text = admin_msg.answer.call_args[0][0]

    assert "Всего пользователей: <code>1</code>" in stats_text
    assert "Активных сегодня: <code>1</code>" in stats_text
    assert "Скачано треков: <code>1</code>" in stats_text
    assert "Изменено тегов: <code>1</code>" in stats_text
    assert "User 501" in stats_text


@pytest.mark.asyncio
async def test_9_critical_start_immediately_registers_before_answer():
    """
    Критический Тест 9:
    Новый пользователь вызывает /start -> сразу запрос статистики -> total_users увеличился на 1.
    Также проверяется порядок выполнения обработчика /start:
    запись пользователя в SQLite ГАРАНТИРОВАННО происходит ДО вызова message.answer.
    """
    user_id = 990001
    chat_id = 880001

    stats_before = get_bot_stats()
    assert stats_before["total_users"] == 0

    message = AsyncMock(spec=Message)
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.from_user.username = "instant_user"
    message.from_user.full_name = "Instant User"
    message.chat = MagicMock()
    message.chat.id = chat_id

    # Внутри message.answer проверяем, что пользователь УЖЕ в БД
    answered_verified = False

    async def mock_answer(*args, **kwargs):
        nonlocal answered_verified
        # В этот самый момент бот только готовит отправку приветствия:
        # проверяем, что в БД уже есть запись!
        assert is_user_registered(user_id) is True
        stats_during = get_bot_stats()
        assert stats_during["total_users"] == 1
        answered_verified = True

    message.answer.side_effect = mock_answer

    state = AsyncMock(spec=FSMContext)
    state.clear = AsyncMock()

    # Выполняем /start
    await cmd_start(message, state)

    assert answered_verified is True

    # Сразу после завершения /start проверяем total_users
    stats_after = get_bot_stats()
    assert stats_after["total_users"] == 1


def test_10_unregistered_activity_does_not_register_user():
    """
    Тест 10:
    Пользователь без вызова /start (например, скачивает трек или редактирует теги).
    Ожидается:
    - log_user_activity, increment_user_download, increment_user_tag_edit НЕ добавляют его в users;
    - пользователь НЕ считается зарегистрированным;
    - total_users остаётся 0.
    """
    ghost_user_id = 999888777

    # Вызовы обновления активности для незарегистрированного пользователя
    log_user_activity(user_id=ghost_user_id, username="ghost", full_name="Ghost")
    increment_user_download(user_id=ghost_user_id)
    increment_user_tag_edit(user_id=ghost_user_id)

    assert is_user_registered(ghost_user_id) is False
    stats = get_bot_stats()
    assert stats["total_users"] == 0
    assert len(get_all_broadcast_chat_ids()) == 0


@pytest.mark.asyncio
async def test_11_broadcast_html_error_fallback_plain_text(monkeypatch, mock_admin):
    """
    Тест 11:
    При ошибке HTML разметки бот делает fallback на отправку без parse_mode и успешно доставляет.
    """
    monkeypatch.setattr(admin_handler, "ADMIN_ID", mock_admin)

    register_user(601, 601, "u601", "U601")

    bot = AsyncMock()

    async def mock_send(chat_id, text, parse_mode=None):
        if parse_mode == "HTML":
            raise TelegramAPIError(method="sendMessage", message="Bad Request: can't parse entities: unclosed tag")
        # Без parse_mode успешно отправляется
        return MagicMock()

    bot.send_message.side_effect = mock_send

    admin_msg = AsyncMock(spec=Message)
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = mock_admin
    admin_msg.text = "/broadcast <b>Неправильный тег"
    progress_mock = AsyncMock()
    admin_msg.answer = AsyncMock(return_value=progress_mock)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await cmd_broadcast(admin_msg, bot)

    # Должно быть 2 вызова: 1 с HTML (ошибка), 2 без parse_mode (успех)
    assert bot.send_message.call_count == 2
    report_text = progress_mock.edit_text.call_args[0][0]
    assert "Доставлено: <b>1</b>" in report_text
    assert "Ошибки отправки: <b>0</b>" in report_text


@pytest.mark.asyncio
async def test_12_broadcast_deactivated_and_network_error(monkeypatch, mock_admin):
    """
    Тест 12:
    - Деактивированный пользователь или 'chat not found' учитывается в blocked_count.
    - Сетевая ошибка (Exception) учитывается в failed_count.
    - Рассылка не прерывается.
    """
    monkeypatch.setattr(admin_handler, "ADMIN_ID", mock_admin)

    register_user(701, 701, "u1", "U1")
    register_user(702, 702, "u2", "U2")
    register_user(703, 703, "u3", "U3")

    bot = AsyncMock()

    async def mock_send(chat_id, text, parse_mode=None):
        if chat_id == 701:
            raise TelegramAPIError(method="sendMessage", message="Bad Request: chat not found")
        if chat_id == 702:
            raise ConnectionResetError("Connection lost to Telegram API")
        return MagicMock()

    bot.send_message.side_effect = mock_send

    admin_msg = AsyncMock(spec=Message)
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = mock_admin
    admin_msg.text = "/broadcast Тест разных сбоев"
    progress_mock = AsyncMock()
    admin_msg.answer = AsyncMock(return_value=progress_mock)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await cmd_broadcast(admin_msg, bot)

    assert bot.send_message.call_count == 3
    report_text = progress_mock.edit_text.call_args[0][0]
    assert "Всего получателей: <b>3</b>" in report_text
    assert "Доставлено: <b>1</b>" in report_text
    assert "Заблокировали бота: <b>1</b>" in report_text
    assert "Ошибки отправки: <b>1</b>" in report_text


def test_13_custom_db_path_environment_variable(tmp_path, monkeypatch):
    """
    Тест 13:
    Поддержка внешней переменной DB_PATH (для подключения Render Persistent Disk).
    """
    custom_dir = tmp_path / "custom_mount" / "nested"
    custom_file = custom_dir / "custom_bot.db"

    monkeypatch.setattr(db, "DB_PATH", custom_file)
    init_db()

    assert custom_file.exists()
    assert custom_dir.is_dir()
