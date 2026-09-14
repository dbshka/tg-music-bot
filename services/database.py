import sqlite3
import datetime
import re
import time
import threading
import urllib.parse
import asyncio
from collections import OrderedDict
from typing import Optional, List, Dict, Any
from config import DB_PATH


class LRUMemoryCache:
    """
    Потокобезопасный двухуровневый L1 RAM кэш с ограничением размера и TTL.
    Обеспечивает субмиллисекундный доступ (< 0.05 мс) без обращения к диску.
    """
    def __init__(self, maxsize: int = 1000, ttl_seconds: int = 86400):
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[Dict[str, Any], float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if key not in self._cache:
                return None
            val, expire_at = self._cache[key]
            if time.time() > expire_at:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return dict(val)

    def set(self, key: str, value: Dict[str, Any]):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (dict(value), time.time() + self.ttl_seconds)
            if len(self._cache) > self.maxsize:
                self._cache.popitem(last=False)

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


# Глобальный L1 RAM кэш (потребляет < 1 МБ RAM на 1000 записей)
_L1_CACHE = LRUMemoryCache(maxsize=1000, ttl_seconds=86400)


def get_db_connection() -> sqlite3.Connection:
    """Создает соединение с SQLite, настроенное на WAL и повышенную производительность."""
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -2000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Инициализирует таблицы базы данных SQLite, индексы и прогревает L1 RAM кэш."""
    with get_db_connection() as conn:
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
        # Создаем индексы для ускорения отчетов /stats и фильтрации пользователей
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_downloads ON users(downloads_count DESC, tags_edited_count DESC)")
        conn.commit()

        # Прогрев L1 RAM кэша последними 200 записями
        try:
            cursor.execute("SELECT query_key, file_id, title, artist, duration FROM tracks_cache ORDER BY created_at DESC LIMIT 200")
            for row in cursor.fetchall():
                _L1_CACHE.set(row["query_key"], dict(row))
        except Exception:
            pass


def normalize_cache_key(query: str) -> str:
    """
    Нормализует поисковый запрос или URL для точного кэширования:
    - Извлекает уникальные идентификаторы треков (YouTube v=..., Spotify track ID, Apple Music ID, Yandex Music ID)
    - Очищает лишние GET-параметры отслеживания
    - Нормализует дефисы и пробелы в текстовых запросах
    """
    q = query.strip().lower()
    if q.startswith("http://") or q.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(q)
            netloc = parsed.netloc

            # YouTube ID
            if "youtube.com" in netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "v" in qs:
                    return f"youtube:{qs['v'][0]}"
                if parsed.path.startswith("/shorts/"):
                    parts = [p for p in parsed.path.split('/') if p]
                    if len(parts) >= 2:
                        return f"youtube:{parts[1]}"
                if parsed.path.startswith("/embed/"):
                    parts = [p for p in parsed.path.split('/') if p]
                    if len(parts) >= 2:
                        return f"youtube:{parts[1]}"
            elif "youtu.be" in netloc:
                vid = parsed.path.strip("/").split("/")[0]
                if vid:
                    return f"youtube:{vid}"

            # Spotify track ID
            elif "spotify.com" in netloc:
                sp_match = re.search(r'track/([a-zA-Z0-9]+)', parsed.path)
                if sp_match:
                    return f"spotify:{sp_match.group(1)}"

            # Apple Music track ID
            elif "apple.com" in netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "i" in qs:
                    return f"applemusic:{qs['i'][0]}"
                am_match = re.search(r'(?:/id|/song/)(\d+)', parsed.path)
                if am_match:
                    return f"applemusic:{am_match.group(1)}"

            # Яндекс Музыка track ID
            elif "music.yandex." in netloc or "yandex." in netloc:
                ym_match = re.search(r'track/(\d+)', parsed.path)
                if ym_match:
                    return f"yandexmusic:{ym_match.group(1)}"

            # Общий случай для URL: отсекаем query параметры и конечный слеш
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            return clean_url
        except Exception:
            return q.split("?")[0].rstrip("/")

    # Для текстовых запросов: нормализация символов, длинных тире и пробелов
    q = q.replace("—", "-").replace("–", "-").replace("−", "-").replace("_", " ")
    q = " ".join(q.split())
    return q


def get_cached_track(query: str) -> Optional[Dict[str, Any]]:
    """
    Проверяет наличие аудиофайла в двухуровневом кэше:
    L1 (RAM) -> мгновенный возврат (< 0.05 мс)
    L2 (SQLite WAL) -> быстрый поиск по первичному ключу.
    """
    key = normalize_cache_key(query)

    # L1: Проверка в оперативной памяти
    mem_cached = _L1_CACHE.get(key)
    if mem_cached:
        return mem_cached

    # L2: Проверка в SQLite
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_id, title, artist, duration FROM tracks_cache WHERE query_key = ?",
                (key,)
            )
            row = cursor.fetchone()
            if row:
                res = dict(row)
                _L1_CACHE.set(key, res)
                return res
    except Exception:
        pass
    return None


async def get_cached_track_async(query: str) -> Optional[Dict[str, Any]]:
    """Асинхронная версия проверки кэша: L1 в RAM (синхронно), L2 в отдельном потоке."""
    key = normalize_cache_key(query)
    mem_cached = _L1_CACHE.get(key)
    if mem_cached:
        return mem_cached
    return await asyncio.to_thread(get_cached_track, query)


def save_cached_track(query: str, file_id: str, title: str, artist: str, duration: int):
    """Сохраняет Telegram file_id скачанного трека в L1 RAM и L2 SQLite."""
    key = normalize_cache_key(query)
    now = datetime.datetime.now()
    item = {
        "file_id": file_id,
        "title": title,
        "artist": artist,
        "duration": duration
    }
    _L1_CACHE.set(key, item)

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO tracks_cache (query_key, file_id, title, artist, duration, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (key, file_id, title, artist, duration, now))
            conn.commit()
    except Exception:
        pass


async def save_cached_track_async(query: str, file_id: str, title: str, artist: str, duration: int):
    """Асинхронное сохранение трека в кэш без блокировки event loop."""
    await asyncio.to_thread(save_cached_track, query, file_id, title, artist, duration)


def log_user_activity(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    """Регистрирует нового пользователя или обновляет время последней активности."""
    now = datetime.datetime.now()
    try:
        with get_db_connection() as conn:
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
    except Exception:
        pass


async def log_user_activity_async(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None):
    """Асинхронное логирование активности пользователя."""
    await asyncio.to_thread(log_user_activity, user_id, username, full_name)


def increment_user_download(user_id: int):
    """Увеличивает счетчик скачанных треков у пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET downloads_count = downloads_count + 1 WHERE user_id = ?", (user_id,))
            conn.commit()
    except Exception:
        pass


async def increment_user_download_async(user_id: int):
    """Асинхронное инкрементирование счетчика скачиваний."""
    await asyncio.to_thread(increment_user_download, user_id)


def increment_user_tag_edit(user_id: int):
    """Увеличивает счетчик отредактированных тегов у пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET tags_edited_count = tags_edited_count + 1 WHERE user_id = ?", (user_id,))
            conn.commit()
    except Exception:
        pass


async def increment_user_tag_edit_async(user_id: int):
    """Асинхронное инкрементирование счетчика тегов."""
    await asyncio.to_thread(increment_user_tag_edit, user_id)


def get_bot_stats() -> Dict[str, Any]:
    """Возвращает сводную статистику по пользователям и активности."""
    now = datetime.datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - datetime.timedelta(days=7)

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # 1. Общее количество пользователей
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        # 2. Активные сегодня (использует индекс idx_users_last_seen)
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

        # 5. Топ 5 активных по скачиваниям (использует индекс idx_users_downloads)
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


async def get_bot_stats_async() -> Dict[str, Any]:
    """Асинхронное получение статистики."""
    return await asyncio.to_thread(get_bot_stats)


def get_all_user_ids() -> List[int]:
    """Возвращает список всех ID пользователей из базы данных для рассылки."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]


async def get_all_user_ids_async() -> List[int]:
    """Асинхронное получение ID пользователей."""
    return await asyncio.to_thread(get_all_user_ids)


