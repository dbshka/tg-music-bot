import sqlite3
import datetime
import re
import urllib.parse
from typing import Optional, List, Dict, Any
from config import DB_PATH


def init_db():
    """Инициализирует таблицы базы данных SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                downloads_count INTEGER DEFAULT 0,
                tags_edited_count INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracks_cache (
                query_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                duration INTEGER,
                created_at TIMESTAMP
            )
        """)
        conn.commit()


def normalize_cache_key(query: str) -> str:
    """
    Нормализует поисковый запрос или URL для точного кэширования:
    - Извлекает уникальные идентификаторы треков (YouTube v=..., Spotify track ID, Apple Music ID)
    - Очищает лишние GET-параметры отслеживания
    - Нормализует дефисы и пробелы в текстовых запросах
    """
    q = query.strip().lower()
    if q.startswith("http://") or q.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(q)
            # YouTube ID
            if "youtube.com" in parsed.netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "v" in qs:
                    return f"youtube:{qs['v'][0]}"
                if parsed.path.startswith("/shorts/"):
                    parts = [p for p in parsed.path.split('/') if p]
                    if len(parts) >= 2:
                        return f"youtube:{parts[1]}"
            elif "youtu.be" in parsed.netloc:
                vid = parsed.path.strip("/")
                if vid:
                    return f"youtube:{vid}"
            # Spotify track ID
            elif "spotify.com" in parsed.netloc:
                sp_match = re.search(r'track/([a-zA-Z0-9]+)', parsed.path)
                if sp_match:
                    return f"spotify:{sp_match.group(1)}"
            # Apple Music track ID
            elif "apple.com" in parsed.netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "i" in qs:
                    return f"applemusic:{qs['i'][0]}"
                am_match = re.search(r'(?:/id|/song/)(\d+)', parsed.path)
                if am_match:
                    return f"applemusic:{am_match.group(1)}"

            # Общий случай для URL: отсекаем query параметры и конечный слеш
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            return clean_url
        except Exception:
            return q.split("?")[0].rstrip("/")

    # Для текстовых запросов: заменяем длинные тире на дефис и схлопываем пробелы
    q = q.replace("—", "-").replace("–", "-").replace("−", "-").replace("_", " ")
    q = " ".join(q.split())
    return q


def get_cached_track(query: str) -> Optional[Dict[str, Any]]:
    """Проверяет наличие уже загруженного аудиофайла в Telegram-кэше."""
    key = normalize_cache_key(query)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_id, title, artist, duration FROM tracks_cache WHERE query_key = ?",
                (key,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return None


def save_cached_track(query: str, file_id: str, title: str, artist: str, duration: int):
    """Сохраняет Telegram file_id скачанного трека для мгновенной отдачи при повторных запросах."""
    key = normalize_cache_key(query)
    now = datetime.datetime.now()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO tracks_cache (query_key, file_id, title, artist, duration, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (key, file_id, title, artist, duration, now))
            conn.commit()
    except Exception:
        pass


def log_user_activity(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    """Регистрирует нового пользователя или обновляет время последней активности."""
    now = datetime.datetime.now()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("""
                UPDATE users 
                SET last_seen = ?, username = COALESCE(?, username), full_name = COALESCE(?, full_name)
                WHERE user_id = ?
            """, (now, username, full_name, user_id))
        else:
            cursor.execute("""
                INSERT INTO users (user_id, username, full_name, first_seen, last_seen, downloads_count, tags_edited_count)
                VALUES (?, ?, ?, ?, ?, 0, 0)
            """, (user_id, username, full_name, now, now))
        conn.commit()


def increment_user_download(user_id: int):
    """Увеличивает счетчик скачанных треков у пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET downloads_count = downloads_count + 1 WHERE user_id = ?", (user_id,))
        conn.commit()


def increment_user_tag_edit(user_id: int):
    """Увеличивает счетчик отредактированных тегов у пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET tags_edited_count = tags_edited_count + 1 WHERE user_id = ?", (user_id,))
        conn.commit()


def get_bot_stats() -> Dict[str, Any]:
    """Возвращает сводную статистику по пользователям и активности."""
    now = datetime.datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - datetime.timedelta(days=7)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Общее количество пользователей
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        # 2. Активные сегодня
        cursor.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (today_start,))
        active_today = cursor.fetchone()[0]

        # 3. Активные за 7 дней
        cursor.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (week_start,))
        active_week = cursor.fetchone()[0]

        # 4. Всего скачано треков
        cursor.execute("SELECT SUM(downloads_count), SUM(tags_edited_count) FROM users")
        row = cursor.fetchone()
        total_downloads = row[0] or 0
        total_tags_edited = row[1] or 0

        # 5. Топ 5 активных по скачиваниям
        cursor.execute("""
            SELECT user_id, username, full_name, downloads_count, tags_edited_count, last_seen
            FROM users
            ORDER BY downloads_count DESC, tags_edited_count DESC
            LIMIT 5
        """)
        top_users = [dict(r) for r in cursor.fetchall()]

        # 6. Последние 5 зарегистрированных
        cursor.execute("""
            SELECT user_id, username, full_name, first_seen, downloads_count
            FROM users
            ORDER BY first_seen DESC
            LIMIT 5
        """)
        recent_users = [dict(r) for r in cursor.fetchall()]

        return {
            "total_users": total_users,
            "active_today": active_today,
            "active_week": active_week,
            "total_downloads": total_downloads,
            "total_tags_edited": total_tags_edited,
            "top_users": top_users,
            "recent_users": recent_users
        }


def get_all_user_ids() -> List[int]:
    """Возвращает список всех ID пользователей из базы данных для рассылки."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]

