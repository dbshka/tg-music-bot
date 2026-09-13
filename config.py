import os
from pathlib import Path
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    # Разрешаем запуск без токена только для проверочных скриптов
    pass

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Максимальный размер файла для Telegram Bot API (50 МБ)
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# Битрейт аудио по умолчанию (кбит/с)
DEFAULT_AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192")

# Настройки прокси или зеркала Telegram Bot API (актуально при блокировках провайдером)
# Настройки прокси или зеркала Telegram Bot API (актуально при блокировках провайдером)
PROXY = os.getenv("PROXY")
CUSTOM_API_SERVER = os.getenv("CUSTOM_API_SERVER")
if CUSTOM_API_SERVER:
    CUSTOM_API_SERVER = CUSTOM_API_SERVER.rstrip("/")

# ID администратора для просмотра статистики и управления
ADMIN_ID = int(os.getenv("ADMIN_ID", "6874119454"))
DB_PATH = BASE_DIR / "bot_database.db"

# Автоматическое создание cookies.txt из переменной окружения YOUTUBE_COOKIES
# (удобно для Hugging Face Spaces Secrets без коммита файла в репозиторий)
COOKIES_FILE = BASE_DIR / "cookies.txt"
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")
if YOUTUBE_COOKIES and not COOKIES_FILE.exists():
    try:
        COOKIES_FILE.write_text(YOUTUBE_COOKIES.strip(), encoding="utf-8")
    except Exception:
        pass
