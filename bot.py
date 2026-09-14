import asyncio
import logging
import socket
import sys
import shutil
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand

from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, PROXY, CUSTOM_API_SERVER, DOWNLOADS_DIR, get_cookies_info
from services.database import init_db
from services.http_client import close_shared_session
from handlers.admin import router as admin_router
from handlers.music import router as music_router
from handlers.tag_editor import router as tag_editor_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class IPv4AiohttpSession(AiohttpSession):
    """
    Кастомная сессия aiohttp, принудительно использующая IPv4.
    Предотвращает зависание и ошибку 'Превышен таймаут семафора' (WinError 121) в Windows.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connector_init["family"] = socket.AF_INET


async def start_healthcheck_server(port: int = 7860):
    """
    Легковесный веб-сервер для Hugging Face / Render / Koyeb.
    Позволяет пинговать бота внешними сервисами (UptimeRobot, cron-job.org),
    чтобы бесплатный хостинг не усыплял контейнер.
    """
    from aiohttp import web
    import os

    async def handle_ping(request):
        return web.Response(text="Music Bot is running OK!", content_type="text/plain")


    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    server_port = int(os.getenv("PORT", port))
    try:
        site = web.TCPSite(runner, "0.0.0.0", server_port)
        await site.start()
        logger.info("Веб-сервер healthcheck запущен на порту %s", server_port)
        return runner
    except Exception as e:
        logger.warning("Не удалось запустить веб-сервер healthcheck: %s", e)
        return None



async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error(
            "Токен бота не задан!\n"
            "Пожалуйста, откройте файл .env и укажите токен от @BotFather в строке BOT_TOKEN=..."
        )
        return

    # Настройка сессии (IPv4 + поддержка Proxy / Custom API Server)
    api_server = TelegramAPIServer.from_base(CUSTOM_API_SERVER) if CUSTOM_API_SERVER else TelegramAPIServer.from_base("https://api.telegram.org")
    
    session_kwargs = {
        "api": api_server
    }
    if PROXY:
        logger.info("Используется прокси: %s", PROXY)
        session_kwargs["proxy"] = PROXY

    session = IPv4AiohttpSession(**session_kwargs)

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Инициализация базы данных SQLite
    init_db()

    # Регистрация обработчиков (admin_router и tag_editor_router перед music_router)
    dp.include_router(admin_router)
    dp.include_router(tag_editor_router)
    dp.include_router(music_router)

    logger.info("Бот запускается...")

    # Проверка статуса cookies для YouTube
    cookie_info = get_cookies_info()
    if cookie_info["active"]:
        logger.info(
            "🍪 Cookies активны: %s (%d байт, %d записей)",
            cookie_info["path"],
            cookie_info["size"],
            cookie_info["cookie_count"]
        )
    else:
        logger.warning(
            "⚠️ Cookies НЕ обнаружены! На сервере Render YouTube может требовать авторизацию (Sign in to confirm you're not a bot). "
            "Рекомендуется добавить Secret File 'cookies.txt' в панели Render (Environment -> Secret Files)."
        )

    # Очистка папки downloads от старых временных файлов
    try:
        for item in DOWNLOADS_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
    except Exception:
        pass

    # Запуск фонового веб-сервера (для UptimeRobot и защиты от сна на Hugging Face / Render / Koyeb)
    runner = await start_healthcheck_server()

    try:
        # Сброс висящих обновлений
        await bot.delete_webhook(drop_pending_updates=True)
        bot_user = await bot.get_me()
        logger.info("✅ Бот успешно подключен к Telegram как @%s!", bot_user.username)

        # Регистрация меню команд Telegram и карточки «Что умеет этот бот» (до нажатия Старт)
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="🚀 Начать / Меню"),
                BotCommand(command="help", description="📖 Инструкция по боту"),
            ])
            # Окно «Что умеет этот бот?», отображаемое по центру чата до нажатия «Старт»
            bot_description = (
                "👋 Я помогу скачать музыку в MP3 и настроить её под себя!\n\n"
                "🎵 Скачивание треков:\n"
                "Отправь ссылку (YouTube, Spotify, Яндекс Музыка, Apple Music, SoundCloud, VK) или название песни.\n\n"
                "✏️ Редактор тегов:\n"
                "Меняй название, исполнителя, альбом и обложку трека в пару кликов прямо в Telegram.\n\n"
                "📂 Поддерживается загрузка своих MP3-файлов для изменения тегов и обложки!"
            )
            await bot.set_my_description(description=bot_description)
            await bot.set_my_short_description(
                short_description="Скачивай треки из YouTube, Spotify, Я.Музыки, Apple Music и редактируй теги MP3 прямо в чате!"
            )
        except Exception as e:
            logger.debug("Не удалось обновить меню команд и описание: %s", e)

        await dp.start_polling(bot)

    except TelegramNetworkError as e:
        logger.error(
            "\n" + "=" * 65 + "\n"
            "❌ ОШИБКА СЕТИ: Не удалось подключиться к серверам Telegram (api.telegram.org)!\n\n"
            "Причина: Ваш интернет-провайдер блокирует прямые запросы к Telegram Bot API\n"
            "(ошибка соединения / таймаут семафора WinError 121).\n\n"
            "КАК РЕШИТЬ (выберите удобный вариант):\n"
            "1. Включите любой рабочий VPN на компьютере.\n"
            "2. Либо настройте локальный прокси в файле .env:\n"
            "   PROXY=socks5://127.0.0.1:10808   (или порт вашего клиента)\n"
            "   PROXY=http://127.0.0.1:10809\n"
            "3. Либо укажите зеркало / обратный прокси Telegram API в файле .env:\n"
            "   CUSTOM_API_SERVER=https://ваш-реверс-прокси\n"
            "=" * 65
        )
    except Exception as e:
        logger.exception("Непредвиденная ошибка при работе бота: %s", e)
    finally:
        if runner:
            await runner.cleanup()
        await close_shared_session()
        await bot.session.close()



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
