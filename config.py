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

# Настройки cookies для YouTube (обход блокировок хостинга)
COOKIES_FILE = BASE_DIR / "cookies.txt"

# 1. Проверяем расположение Secret Files на Render (/etc/secrets/cookies.txt)
# Важно: раздел /etc/secrets смонтирован в Render как read-only, а yt-dlp обновляет
# сессионные куки в файле. Поэтому копируем его в рабочую папку бота (доступную для записи).
render_secret_cookies = Path("/etc/secrets/cookies.txt")
if render_secret_cookies.exists() and render_secret_cookies.stat().st_size > 50:
    try:
        content = render_secret_cookies.read_text(encoding="utf-8", errors="ignore")
        COOKIES_FILE.write_text(content, encoding="utf-8")
    except Exception:
        pass

# 2. Если файл еще не найден, проверяем переменные окружения
if not COOKIES_FILE.exists() or COOKIES_FILE.stat().st_size < 50:
    import base64
    raw_b64 = os.getenv("YOUTUBE_COOKIES_BASE64")
    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64.strip()).decode("utf-8")
            COOKIES_FILE = BASE_DIR / "cookies.txt"
            COOKIES_FILE.write_text(decoded.strip(), encoding="utf-8")
        except Exception:
            pass

    raw_env = os.getenv("YOUTUBE_COOKIES")
    if raw_env and (not COOKIES_FILE.exists() or COOKIES_FILE.stat().st_size < 50):
        try:
            cleaned = raw_env.replace("\\n", "\n").replace("\\t", "\t").strip()
            if cleaned.startswith("IyBOZXRzY2FwZQ") or (len(cleaned) > 100 and " " not in cleaned and "\n" not in cleaned):
                try:
                    cleaned = base64.b64decode(cleaned).decode("utf-8")
                except Exception:
                    pass
            COOKIES_FILE = BASE_DIR / "cookies.txt"
            COOKIES_FILE.write_text(cleaned, encoding="utf-8")
        except Exception:
            pass


def get_cookies_info() -> dict:
    """Возвращает информацию о текущем состоянии cookies для логирования."""
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 50:
        try:
            content = COOKIES_FILE.read_text(encoding="utf-8", errors="ignore")
            lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
            return {
                "active": True,
                "path": str(COOKIES_FILE),
                "size": COOKIES_FILE.stat().st_size,
                "cookie_count": len(lines)
            }
        except Exception:
            pass
    return {
        "active": False,
        "path": str(COOKIES_FILE),
        "size": 0,
        "cookie_count": 0
    }


