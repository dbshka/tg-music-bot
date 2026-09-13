import html
import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile
)

from config import DOWNLOADS_DIR, MAX_FILE_SIZE_BYTES
from services.tag_editor import read_mp3_tags, apply_mp3_tags
from services.database import log_user_activity, increment_user_tag_edit

logger = logging.getLogger(__name__)
router = Router(name="tag_editor_router")


class TagEditorStates(StatesGroup):
    menu = State()
    waiting_for_title = State()
    waiting_for_artist = State()
    waiting_for_album = State()
    waiting_for_cover = State()


def get_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура главного меню редактирования тегов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Артист", callback_data="tag:edit:artist"),
                InlineKeyboardButton(text="🎶 Название", callback_data="tag:edit:title"),
            ],
            [
                InlineKeyboardButton(text="💿 Альбом", callback_data="tag:edit:album"),
                InlineKeyboardButton(text="🖼 Обложка", callback_data="tag:edit:cover"),
            ],
            [
                InlineKeyboardButton(text="🚀 Применить и отправить", callback_data="tag:save"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="tag:cancel"),
            ]
        ]
    )


def get_audio_edit_keyboard() -> InlineKeyboardMarkup:
    """Кнопка 'Изменить теги' под отправленным аудиофайлом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить теги", callback_data="audio:edit")]
        ]
    )



def get_back_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура кнопки «Назад» при вводе данных."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="tag:back")]
        ]
    )


def format_menu_text(data: dict) -> str:
    """Генерирует текст меню с текущими значениями тегов."""
    title = html.escape(data.get("title") or "—")
    artist = html.escape(data.get("artist") or "—")
    album = html.escape(data.get("album") or "—")

    if data.get("cover_path"):
        cover_status = "🖼 Загружена новая обложка"
    elif data.get("has_original_cover"):
        cover_status = "🖼 Исходная обложка присутствует"
    else:
        cover_status = "⚪ Без обложки"

    return (
        "🎵 <b>Редактор тегов аудио</b>\n\n"
        f"👤 <b>Исполнитель:</b> {artist}\n"
        f"🎶 <b>Название:</b> {title}\n"
        f"💿 <b>Альбом:</b> {album}\n"
        f"Обложка: <i>{cover_status}</i>\n\n"
        "<i>Нажмите на нужную кнопку ниже, чтобы изменить поле, или «Применить и отправить»:</i>"
    )


async def render_menu(bot: Bot, chat_id: int, state: FSMContext):
    """Обновляет сообщение меню редактора."""
    data = await state.get_data()
    menu_msg_id = data.get("menu_message_id")
    text = format_menu_text(data)
    kb = get_menu_keyboard()

    await state.set_state(TagEditorStates.menu)
    if menu_msg_id:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=menu_msg_id,
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        except Exception:
            pass

    new_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(menu_message_id=new_msg.message_id)


# -------------------------------------------------------------
# 1. Приём входящего MP3-файла (аудио или документ)
# -------------------------------------------------------------
@router.message(F.audio | (F.document & F.document.file_name.endswith(".mp3")))
async def handle_incoming_audio(message: Message, state: FSMContext, bot: Bot):
    if message.from_user:
        log_user_activity(message.from_user.id, message.from_user.username, message.from_user.full_name)

    audio_obj = message.audio or message.document
    if audio_obj.file_size and audio_obj.file_size > MAX_FILE_SIZE_BYTES:
        await message.reply(
            "❌ <b>Файл слишком большой!</b>\n"
            "Telegram разрешает ботам обрабатывать файлы размером до 50 МБ.",
            parse_mode="HTML"
        )
        return

    # Очистим предыдущее состояние, если было
    prev_data = await state.get_data()
    if prev_data.get("folder_path"):
        shutil.rmtree(prev_data["folder_path"], ignore_errors=True)
    await state.clear()

    status_msg = await message.reply("📥 <i>Загружаю аудиофайл для редактирования...</i>", parse_mode="HTML")

    session_id = uuid.uuid4().hex
    session_dir = DOWNLOADS_DIR / f"edit_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    local_file_path = session_dir / "track.mp3"

    try:
        await bot.download(audio_obj, destination=local_file_path)
        meta = read_mp3_tags(local_file_path)

        # Если Telegram предоставил теги из заголовка сообщения
        initial_title = getattr(audio_obj, "title", None) or meta.title
        initial_performer = getattr(audio_obj, "performer", None) or meta.artist
        duration = getattr(audio_obj, "duration", None) or meta.duration

        await state.update_data(
            folder_path=str(session_dir),
            file_path=str(local_file_path),
            title=initial_title,
            artist=initial_performer,
            album=meta.album,
            duration=duration,
            cover_path=None,
            has_original_cover=meta.has_cover,
            menu_message_id=status_msg.message_id
        )

        await render_menu(bot, message.chat.id, state)

    except Exception as e:
        logger.exception("Ошибка при обработке аудиофайла")
        shutil.rmtree(session_dir, ignore_errors=True)
        await status_msg.edit_text(
            f"❌ <b>Не удалось обработать аудиофайл:</b>\n<i>{html.escape(str(e))}</i>",
            parse_mode="HTML"
        )


# -------------------------------------------------------------
# 2. Нажатие кнопки «Изменить теги» под аудиофайлом в чате
# -------------------------------------------------------------
@router.callback_query(F.data == "audio:edit")
async def cb_start_edit_from_audio(callback: CallbackQuery, state: FSMContext, bot: Bot):
    audio_obj = callback.message.audio
    if not audio_obj:
        await callback.answer("❌ Аудиофайл не найден.", show_alert=True)
        return

    await callback.answer()

    # Очистим предыдущее состояние, если было
    prev_data = await state.get_data()
    if prev_data.get("folder_path"):
        shutil.rmtree(prev_data["folder_path"], ignore_errors=True)
    await state.clear()

    status_msg = await callback.message.reply("📥 <i>Загружаю аудиофайл для редактирования...</i>", parse_mode="HTML")

    session_id = uuid.uuid4().hex
    session_dir = DOWNLOADS_DIR / f"edit_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    local_file_path = session_dir / "track.mp3"

    try:
        await bot.download(audio_obj, destination=local_file_path)
        meta = read_mp3_tags(local_file_path)

        initial_title = audio_obj.title or meta.title
        initial_performer = audio_obj.performer or meta.artist
        duration = audio_obj.duration or meta.duration

        await state.update_data(
            folder_path=str(session_dir),
            file_path=str(local_file_path),
            title=initial_title,
            artist=initial_performer,
            album=meta.album,
            duration=duration,
            cover_path=None,
            has_original_cover=meta.has_cover,
            menu_message_id=status_msg.message_id
        )

        await render_menu(bot, callback.message.chat.id, state)

    except Exception as e:
        logger.exception("Ошибка при обработке аудиофайла по кнопке 'Изменить'")
        shutil.rmtree(session_dir, ignore_errors=True)
        await status_msg.edit_text(
            f"❌ <b>Не удалось загрузить аудио:</b>\n<i>{html.escape(str(e))}</i>",
            parse_mode="HTML"
        )


# -------------------------------------------------------------
# 3. Обработка нажатий на инлайн-кнопки меню
# -------------------------------------------------------------

@router.callback_query(F.data == "tag:edit:artist")
async def cb_edit_artist(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TagEditorStates.waiting_for_artist)
    await callback.message.edit_text(
        "👤 <b>Введите нового исполнителя (артиста):</b>\n\n"
        "<i>Например: Queen, Michael Jackson, Miyagi</i>\n\n"
        "Либо нажмите «Назад», чтобы оставить текущее значение.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "tag:edit:title")
async def cb_edit_title(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TagEditorStates.waiting_for_title)
    await callback.message.edit_text(
        "🎶 <b>Введите новое название песни:</b>\n\n"
        "<i>Например: Bohemian Rhapsody, Billie Jean</i>\n\n"
        "Либо нажмите «Назад», чтобы оставить текущее значение.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "tag:edit:album")
async def cb_edit_album(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TagEditorStates.waiting_for_album)
    await callback.message.edit_text(
        "💿 <b>Введите название альбома:</b>\n\n"
        "<i>Либо нажмите «Назад», чтобы оставить текущее значение.</i>",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "tag:edit:cover")
async def cb_edit_cover(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TagEditorStates.waiting_for_cover)
    await callback.message.edit_text(
        "🖼 <b>Отправьте изображение (фотографию) для обложки трека:</b>\n\n"
        "<i>Изображение будет автоматически обрезано и вшито в MP3-файл.</i>\n\n"
        "Либо нажмите «Назад», чтобы оставить текущее значение.",
        reply_markup=get_back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "tag:back")
async def cb_back(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await render_menu(bot, callback.message.chat.id, state)
    await callback.answer()


@router.callback_query(F.data == "tag:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    folder = data.get("folder_path")
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    await state.clear()
    await callback.message.edit_text("❌ <i>Редактирование тегов отменено.</i>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "tag:save")
async def cb_save(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    file_path = data.get("file_path")
    folder_path = data.get("folder_path")

    if not file_path or not Path(file_path).exists():
        await callback.message.edit_text("❌ <i>Файл устарел или был удален. Отправьте аудио заново.</i>", parse_mode="HTML")
        await state.clear()
        return

    await callback.message.edit_text("⏳ <i>Применяю теги и отправляю MP3...</i>", parse_mode="HTML")
    await callback.answer()

    try:
        mp3_path = Path(file_path)
        cover_path = Path(data["cover_path"]) if data.get("cover_path") else None

        updated_mp3, final_cover = apply_mp3_tags(
            file_path=mp3_path,
            title=data.get("title"),
            artist=data.get("artist"),
            album=data.get("album"),
            cover_path=cover_path
        )

        audio_file = FSInputFile(updated_mp3)
        thumb_file = FSInputFile(final_cover) if final_cover and final_cover.exists() else None

        await callback.message.answer_audio(
            audio=audio_file,
            title=data.get("title"),
            performer=data.get("artist"),
            duration=data.get("duration") or 0,
            thumbnail=thumb_file,
            reply_markup=get_audio_edit_keyboard()
        )

        if callback.from_user:
            increment_user_tag_edit(callback.from_user.id)

        await callback.message.delete()

    except Exception as e:
        logger.exception("Ошибка при сохранении тегов")
        await callback.message.edit_text(
            f"❌ <b>Ошибка при сохранении тегов:</b>\n<i>{html.escape(str(e))}</i>",
            parse_mode="HTML"
        )
    finally:
        if folder_path:
            shutil.rmtree(folder_path, ignore_errors=True)
        await state.clear()


# -------------------------------------------------------------
# 3. Приём новых значений (текст / фото) в состояниях FSM
# -------------------------------------------------------------
@router.message(TagEditorStates.waiting_for_artist, F.text)
async def process_artist(message: Message, state: FSMContext, bot: Bot):
    new_artist = message.text.strip()
    await state.update_data(artist=new_artist)
    try:
        await message.delete()
    except Exception:
        pass
    await render_menu(bot, message.chat.id, state)


@router.message(TagEditorStates.waiting_for_title, F.text)
async def process_title(message: Message, state: FSMContext, bot: Bot):
    new_title = message.text.strip()
    await state.update_data(title=new_title)
    try:
        await message.delete()
    except Exception:
        pass
    await render_menu(bot, message.chat.id, state)


@router.message(TagEditorStates.waiting_for_album, F.text)
async def process_album(message: Message, state: FSMContext, bot: Bot):
    new_album = message.text.strip()
    await state.update_data(album=new_album)
    try:
        await message.delete()
    except Exception:
        pass
    await render_menu(bot, message.chat.id, state)


@router.message(TagEditorStates.waiting_for_cover, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def process_cover(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    folder_path = data.get("folder_path")
    if not folder_path:
        await message.reply("Сессия устарела. Отправьте аудиофайл снова.")
        await state.clear()
        return

    # Берём самое качественное фото
    if message.photo:
        photo = message.photo[-1]
    else:
        photo = message.document

    cover_target = Path(folder_path) / "raw_cover.jpg"
    await bot.download(photo, destination=cover_target)
    await state.update_data(cover_path=str(cover_target))

    try:
        await message.delete()
    except Exception:
        pass

    await render_menu(bot, message.chat.id, state)
