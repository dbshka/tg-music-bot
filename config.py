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
BOT_VERSION = "2.5.4"

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
import base64
import tempfile

COOKIES_FILE = Path(tempfile.gettempdir()) / "yt_cookies.txt"

def _setup_cookies() -> Path:
    """
    Ищет cookies во всех возможных источниках:
    1. Каталог Secret Files на Render (/etc/secrets)
    2. Файл cookies.txt в корне проекта
    3. Переменные окружения YOUTUBE_COOKIES_BASE64 и YOUTUBE_COOKIES
    Копирует найденные куки в гарантированно доступный для записи каталог (/tmp),
    чтобы избежать ошибок Read-only file system на Render.
    """
    dest = Path(tempfile.gettempdir()) / "yt_cookies.txt"
    
    # 1. Поиск в Secret Files Render (/etc/secrets)
    secrets_dir = Path("/etc/secrets")
    if secrets_dir.exists():
        print(f"[COOKIES] Обнаружена системная папка Secret Files: {secrets_dir}", flush=True)
        try:
            for item in secrets_dir.iterdir():
                print(f"[COOKIES] Найден секретный файл: {item.name} ({item.stat().st_size} байт)", flush=True)
                if item.is_file() and item.stat().st_size > 30:
                    text = item.read_text(encoding="utf-8", errors="ignore")
                    dest.write_text(text, encoding="utf-8")
                    print(f"[COOKIES] Успешно скопирован {item} -> {dest} ({dest.stat().st_size} байт)", flush=True)
                    return dest
        except Exception as e:
            print(f"[COOKIES] Ошибка при чтении /etc/secrets: {e}", flush=True)

    # 2. Поиск локального cookies.txt в папке проекта
    for name in ["cookies.txt", "cookies"]:
        local_candidate = BASE_DIR / name
        if local_candidate.exists() and local_candidate.stat().st_size > 30:
            try:
                text = local_candidate.read_text(encoding="utf-8", errors="ignore")
                dest.write_text(text, encoding="utf-8")
                print(f"[COOKIES] Скопирован локальный файл {local_candidate} -> {dest} ({dest.stat().st_size} байт)", flush=True)
                return dest
            except Exception as e:
                print(f"[COOKIES] Ошибка при копировании {local_candidate}: {e}", flush=True)

    # 3. Переменная YOUTUBE_COOKIES_BASE64
    raw_b64 = os.getenv("YOUTUBE_COOKIES_BASE64")
    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64.strip()).decode("utf-8")
            dest.write_text(decoded.strip(), encoding="utf-8")
            print(f"[COOKIES] Успешно загружены cookies из YOUTUBE_COOKIES_BASE64 ({dest.stat().st_size} байт)", flush=True)
            return dest
        except Exception as e:
            print(f"[COOKIES] Ошибка декодирования YOUTUBE_COOKIES_BASE64: {e}", flush=True)

    # 4. Переменная YOUTUBE_COOKIES
    raw_env = os.getenv("YOUTUBE_COOKIES")
    if raw_env:
        try:
            cleaned = raw_env.replace("\\n", "\n").replace("\\t", "\t").strip()
            if cleaned.startswith("IyBOZXRzY2FwZQ") or (len(cleaned) > 100 and " " not in cleaned and "\n" not in cleaned):
                try:
                    cleaned = base64.b64decode(cleaned).decode("utf-8")
                except Exception:
                    pass
            dest.write_text(cleaned, encoding="utf-8")
            print(f"[COOKIES] Успешно загружены cookies из YOUTUBE_COOKIES ({dest.stat().st_size} байт)", flush=True)
            return dest
        except Exception as e:
            print(f"[COOKIES] Ошибка записи YOUTUBE_COOKIES: {e}", flush=True)

    print("[COOKIES] Файл cookies не найден ни в одном из источников.", flush=True)
    return dest

COOKIES_FILE = _setup_cookies()


def get_cookies_info() -> dict:
    """Возвращает актуальную информацию о cookies."""
    target = COOKIES_FILE
    # Если в первый раз не нашлось, повторно проверим
    if not target.exists() or target.stat().st_size < 30:
        target = _setup_cookies()

    if target.exists() and target.stat().st_size > 30:
        try:
            content = target.read_text(encoding="utf-8", errors="ignore")
            lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
            has_auth = any(token in content for token in ["LOGIN_INFO", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"])
            return {
                "active": True,
                "path": str(target),
                "size": target.stat().st_size,
                "cookie_count": len(lines),
                "is_authenticated": has_auth
            }
        except Exception:
            pass
    return {
        "active": False,
        "path": str(target),
        "size": 0,
        "cookie_count": 0,
        "is_authenticated": False
    }



