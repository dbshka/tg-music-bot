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
from services.identity import clean_unicode_text


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

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._cache.pop(key, None) is not None

    def pop(self, key: str, default=None) -> Any:
        with self._lock:
            val = self._cache.pop(key, None)
            return dict(val[0]) if val else default

    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    def evict_file_id(self, file_id: str) -> int:
        count = 0
        with self._lock:
            keys_to_del = [k for k, v in self._cache.items() if v[0].get("file_id") == file_id]
            for k in keys_to_del:
                del self._cache[k]
                count += 1
        return count


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
    """Инициализирует таблицы базы данных SQLite, индексы, миграции и прогревает L1 RAM кэш."""
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
                created_at TIMESTAMP,
                variant TEXT DEFAULT 'original'
            )
        """)
        # Миграция: проверяем наличие колонки variant
        try:
            cursor.execute("PRAGMA table_info(tracks_cache)")
            columns = [col[1] for col in cursor.fetchall()]
            if "variant" not in columns:
                cursor.execute("ALTER TABLE tracks_cache ADD COLUMN variant TEXT DEFAULT 'original'")
        except Exception:
            pass

        # Создаем индексы для ускорения
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_downloads ON users(downloads_count DESC, tags_edited_count DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_file_id ON tracks_cache(file_id)")
        conn.commit()

        # Прогрев L1 RAM кэша последними 200 записями
        try:
            cursor.execute("SELECT query_key, file_id, title, artist, duration, variant FROM tracks_cache ORDER BY created_at DESC LIMIT 200")
            for row in cursor.fetchall():
                _L1_CACHE.set(row["query_key"], dict(row))
        except Exception:
            pass


def normalize_cache_key(query: str) -> str:
    """
    Нормализует поисковый запрос или URL для точного кэширования:
    - Извлекает уникальные идентификаторы треков с сохранением регистра (Base62 Spotify ID, YouTube ID)
    - Очищает лишние GET-параметры отслеживания
    - Для текстовых запросов: удаляет zero-width символы, нормализует дефисы и пробелы
    """
    q = clean_unicode_text(query.strip())
    if q.startswith("http://") or q.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(q)
            netloc = parsed.netloc.lower()

            # YouTube ID (сохраняем регистр 11-символьного ID!)
            if "youtube.com" in netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "v" in qs and qs["v"]:
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

            # Spotify track ID (регистрозависимый Base62!)
            elif "spotify.com" in netloc:
                sp_match = re.search(r'track/([a-zA-Z0-9]+)', parsed.path)
                if sp_match:
                    return f"spotify:{sp_match.group(1)}"

            # Apple Music track ID
            elif "apple.com" in netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "i" in qs and qs["i"]:
                    return f"applemusic:{qs['i'][0]}"
                m_path = re.search(r'/(?:id|song|album)(?:/[^/\s?]+)*/(\d+)', parsed.path)
                if m_path:
                    return f"applemusic:{m_path.group(1)}"
                m_id = re.search(r'/id(\d+)', parsed.path)
                if m_id:
                    return f"applemusic:{m_id.group(1)}"
                m_digits = re.search(r'/(\d+)(?:[?]|$)', parsed.path)
                if m_digits:
                    return f"applemusic:{m_digits.group(1)}"

            # Yandex Music track ID
            elif "music.yandex." in netloc or "yandex." in netloc:
                m_track = re.search(r'/track/(\d+)', parsed.path)
                if m_track:
                    return f"yandex:{m_track.group(1)}"
                qs = urllib.parse.parse_qs(parsed.query)
                if "track" in qs and qs["track"]:
                    return f"yandex:{qs['track'][0]}"

            # VK Music audio ID
            elif "vk.com" in netloc or "vk.ru" in netloc:
                m_vk = re.search(r'audio(-?\d+)_(\d+)', parsed.path + "?" + parsed.query)
                if m_vk:
                    return f"vk:{m_vk.group(1)}_{m_vk.group(2)}"

            # Общий случай для URL: scheme и host в lowercase, путь с сохранением регистра
            clean_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")
            return clean_url
        except Exception:
            return q.split("?")[0].rstrip("/")

    # Для текстовых запросов: нормализация символов, длинных тире и пробелов
    q = q.replace("—", "-").replace("–", "-").replace("−", "-").replace("_", " ")
    q = " ".join(q.split()).lower()
    return q


def build_variant_cache_key(base_key: str, variant: str = "original") -> str:
    """Формирует ключ кэша с изолированным суффиксом варианта/модификатора."""
    if "#var=" in base_key:
        return base_key
    norm_var = variant.strip().lower() if variant else "original"
    if norm_var and norm_var != "original":
        return f"{base_key}#var={norm_var}"
    return base_key


def get_cached_track(query: str, variant: str = "original") -> Optional[Dict[str, Any]]:
    """
    Проверяет наличие аудиофайла в двухуровневом кэше:
    L1 (RAM) -> мгновенный возврат (< 0.05 мс)
    L2 (SQLite WAL) -> быстрый поиск по первичному ключу.
    """
    norm = normalize_cache_key(query)
    key = build_variant_cache_key(norm, variant)

    # L1: Проверка в оперативной памяти
    mem_cached = _L1_CACHE.get(key)
    if mem_cached:
        return mem_cached

    # L2: Проверка в SQLite
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_id, title, artist, duration, variant FROM tracks_cache WHERE query_key = ?",
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


async def get_cached_track_async(query: str, variant: str = "original") -> Optional[Dict[str, Any]]:
    """Асинхронная версия проверки кэша: L1 в RAM (синхронно), L2 в отдельном потоке."""
    norm = normalize_cache_key(query)
    key = build_variant_cache_key(norm, variant)
    mem_cached = _L1_CACHE.get(key)
    if mem_cached:
        return mem_cached
    return await asyncio.to_thread(get_cached_track, query, variant)


def save_cached_track(query: str, file_id: str, title: str, artist: str, duration: int, variant: str = "original"):
    """
    Сохраняет Telegram file_id скачанного трека в L1 RAM и L2 SQLite.
    Защита от кэширования single-word non-authoritative запросов (например 'creep').
    """
    norm_key = normalize_cache_key(query)
    is_url = norm_key.startswith(("http://", "https://", "youtube:", "spotify:", "applemusic:"))

    # Запрет загрязнения кэша неоднозначными однословными запросами без артиста
    if not is_url and "-" not in norm_key and len(norm_key.split()) <= 1:
        if artist and title:
            canonical_q = f"{artist} - {title}"
            norm_key = normalize_cache_key(canonical_q)
        else:
            return

    key = build_variant_cache_key(norm_key, variant)
    now = datetime.datetime.now()
    item = {
        "file_id": file_id,
        "title": title,
        "artist": artist,
        "duration": duration,
        "variant": variant
    }
    _L1_CACHE.set(key, item)

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO tracks_cache (query_key, file_id, title, artist, duration, created_at, variant)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (key, file_id, title, artist, duration, now, variant))
            conn.commit()
    except Exception:
        pass


async def save_cached_track_async(query: str, file_id: str, title: str, artist: str, duration: int, variant: str = "original"):
    """Асинхронное сохранение трека в кэш без блокировки event loop."""
    await asyncio.to_thread(save_cached_track, query, file_id, title, artist, duration, variant)


def delete_cached_track(query: str, variant: str = "original"):
    """Удаляет трек из L1 (RAM) и L2 (SQLite) кэша при обнаружении несоответствия."""
    norm_key = normalize_cache_key(query)
    key = build_variant_cache_key(norm_key, variant)
    _L1_CACHE.pop(key, None)
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tracks_cache WHERE query_key = ?", (key,))
            conn.commit()
    except Exception:
        pass


async def delete_cached_track_async(query: str, variant: str = "original"):
    """Асинхронное удаление трека из кэша без блокировки event loop."""
    await asyncio.to_thread(delete_cached_track, query, variant)


def invalidate_cached_file_id(file_id: str) -> int:
    """Удаляет из L1 и L2 кэша все записи с данным file_id при ошибке Telegram (expired/invalid file_id)."""
    _L1_CACHE.evict_file_id(file_id)
    count = 0
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tracks_cache WHERE file_id = ?", (file_id,))
            count = cursor.rowcount
            conn.commit()
    except Exception:
        pass
    return count


async def invalidate_cached_file_id_async(file_id: str) -> int:
    """Асинхронная инвалидация file_id."""
    return await asyncio.to_thread(invalidate_cached_file_id, file_id)


def search_cached_tracks(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Поиск треков в кэше по частичному совпадению названия или исполнителя.
    Используется для быстрого ответа в Telegram Inline Mode.
    """
    clean_q = re.sub(r'[\W_]+', ' ', query.lower()).strip()
    if not clean_q:
        return []
    words = clean_q.split()
    like_patterns = [f"%{w}%" for w in words]

    # Все слова должны встречаться в title, artist или query_key
    conditions = []
    params = []
    for pat in like_patterns:
        conditions.append("(LOWER(title) LIKE ? OR LOWER(artist) LIKE ? OR LOWER(query_key) LIKE ?)")
        params.extend([pat, pat, pat])

    where_clause = " AND ".join(conditions)
    params.append(limit)

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT file_id, title, artist, duration, query_key FROM tracks_cache WHERE {where_clause} ORDER BY created_at DESC LIMIT ?",
                tuple(params)
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


async def search_cached_tracks_async(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Асинхронный поиск треков в кэше для Inline Mode."""
    return await asyncio.to_thread(search_cached_tracks, query, limit)


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
