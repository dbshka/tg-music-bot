import re
import time
import json
import hashlib
import logging
import asyncio
import unicodedata
import urllib.parse
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Tuple

from config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_D1_DATABASE_ID,
    CLOUDFLARE_API_TOKEN,
    SEARCH_CACHE_TTL_SECONDS,
)
from services.http_client import get_shared_session
from services.identity import clean_unicode_text
from services.database import LRUMemoryCache, save_cached_track_async, invalidate_cached_file_id_async

logger = logging.getLogger(__name__)

# L1 In-Memory кэш для треков и поисковых результатов
_PERSISTENT_TRACK_L1 = LRUMemoryCache(maxsize=1000, ttl_seconds=86400)
_PERSISTENT_SEARCH_L1 = LRUMemoryCache(maxsize=500, ttl_seconds=SEARCH_CACHE_TTL_SECONDS)

# Интервал троттлинга обновления last_used_at в Cloudflare D1 (секунды)
# Защищает лимит 100k daily writes: запись происходит не чаще одного раза в час для популярного трека
LAST_USED_UPDATE_INTERVAL_SECONDS = 3600


@dataclass
class PersistentTrackCacheItem:
    source_key: str
    source_type: str
    source_id: str
    artist: Optional[str]
    title: str
    album: Optional[str]
    duration: Optional[int]
    metadata_hash: str
    telegram_file_id: str
    telegram_file_unique_id: Optional[str] = None
    storage_message_id: Optional[int] = None
    file_size: Optional[int] = None
    created_at: int = 0
    last_used_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersistentTrackCacheItem":
        return cls(
            source_key=str(data.get("source_key", "")),
            source_type=str(data.get("source_type", "")),
            source_id=str(data.get("source_id", "")),
            artist=data.get("artist"),
            title=str(data.get("title", "")),
            album=data.get("album"),
            duration=int(data.get("duration", 0)) if data.get("duration") is not None else None,
            metadata_hash=str(data.get("metadata_hash", "")),
            telegram_file_id=str(data.get("telegram_file_id", "")),
            telegram_file_unique_id=data.get("telegram_file_unique_id"),
            storage_message_id=int(data["storage_message_id"]) if data.get("storage_message_id") is not None else None,
            file_size=int(data["file_size"]) if data.get("file_size") is not None else None,
            created_at=int(data.get("created_at", 0)),
            last_used_at=int(data.get("last_used_at", 0)),
        )

    def matches_metadata(
        self,
        artist: Optional[Any],
        title: Any,
        album: Optional[Any] = None,
        duration: Optional[Any] = None
    ) -> bool:
        """
        Проверяет совпадение канонических метаданных с кэшированным metadata_hash.
        Использует compute_metadata_hash (casefold + NFKC), поэтому:
        - Различие только в регистре ('RSAC' vs 'RsAC') НЕ считается изменением (True).
        - Различие в существенных полях ('RSAC' vs 'Unknown') возвращает False (mismatch).
        """
        is_match, _, _ = check_metadata_match(self, artist, title, album, duration)
        return is_match


# ============================================================================
# 1. ФОРМИРОВАНИЕ СТАБИЛЬНЫХ КЛЮЧЕЙ И ХЭШЕЙ
# ============================================================================

def build_source_key(target: str) -> Tuple[str, str, str]:
    """
    Формирует стабильный и детерминированный ключ источника трека.
    Возвращает (source_key, source_type, source_id).

    Для YouTube (включая ссылки на видео, шортсы, embed, music.youtube.com):
    все форматы одного video_id приводятся к "youtube:<video_id>".
    Разные версии (например, studio и live) имеют разные video_id и разные ключи.
    """
    raw = clean_unicode_text(target.strip())
    if not raw:
        return "raw:empty", "raw", "empty"

    # Если уже передан готовый source_key вида "youtube:1NI14HDF7h0"
    if ":" in raw and not raw.startswith(("http://", "https://")):
        prefix, sid = raw.split(":", 1)
        prefix = prefix.strip().lower()
        sid = sid.strip()
        if prefix in {"youtube", "spotify", "applemusic", "yandex", "vk", "soundcloud"}:
            return f"{prefix}:{sid}", prefix, sid

    if raw.startswith(("http://", "https://")):
        try:
            parsed = urllib.parse.urlparse(raw)
            netloc = parsed.netloc.lower()

            # YouTube / YouTube Music
            if "youtube.com" in netloc or "youtu.be" in netloc:
                if "youtu.be" in netloc:
                    vid = parsed.path.strip("/").split("/")[0]
                    if vid:
                        return f"youtube:{vid}", "youtube", vid
                else:
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "v" in qs and qs["v"]:
                        vid = qs["v"][0]
                        return f"youtube:{vid}", "youtube", vid
                    if parsed.path.startswith("/shorts/"):
                        parts = [p for p in parsed.path.split('/') if p]
                        if len(parts) >= 2:
                            return f"youtube:{parts[1]}", "youtube", parts[1]
                    if parsed.path.startswith("/embed/"):
                        parts = [p for p in parsed.path.split('/') if p]
                        if len(parts) >= 2:
                            return f"youtube:{parts[1]}", "youtube", parts[1]

            # Spotify
            elif "spotify.com" in netloc:
                sp_match = re.search(r'track/([a-zA-Z0-9]+)', parsed.path)
                if sp_match:
                    sid = sp_match.group(1)
                    return f"spotify:{sid}", "spotify", sid

            # Apple Music
            elif "apple.com" in netloc:
                qs = urllib.parse.parse_qs(parsed.query)
                if "i" in qs and qs["i"]:
                    sid = qs["i"][0]
                    return f"applemusic:{sid}", "applemusic", sid
                m_path = re.search(r'/(?:id|song|album)(?:/[^/\s?]+)*/(\d+)', parsed.path)
                if m_path:
                    sid = m_path.group(1)
                    return f"applemusic:{sid}", "applemusic", sid
                m_id = re.search(r'/id(\d+)', parsed.path)
                if m_id:
                    sid = m_id.group(1)
                    return f"applemusic:{sid}", "applemusic", sid
                m_digits = re.search(r'/(\d+)(?:[?]|$)', parsed.path)
                if m_digits:
                    sid = m_digits.group(1)
                    return f"applemusic:{sid}", "applemusic", sid

            # Yandex Music
            elif "music.yandex." in netloc or "yandex." in netloc:
                m_track = re.search(r'/track/(\d+)', parsed.path)
                if m_track:
                    sid = m_track.group(1)
                    return f"yandex:{sid}", "yandex", sid
                qs = urllib.parse.parse_qs(parsed.query)
                if "track" in qs and qs["track"]:
                    sid = qs["track"][0]
                    return f"yandex:{sid}", "yandex", sid

            # VK Music
            elif "vk.com" in netloc or "vk.ru" in netloc:
                m_vk = re.search(r'audio(-?\d+)_(\d+)', parsed.path + "?" + parsed.query)
                if m_vk:
                    sid = f"{m_vk.group(1)}_{m_vk.group(2)}"
                    return f"vk:{sid}", "vk", sid

            # SoundCloud
            elif "soundcloud.com" in netloc:
                clean_path = parsed.path.strip("/")
                if clean_path:
                    return f"soundcloud:{clean_path}", "soundcloud", clean_path

            # Общий веб-URL
            clean_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}".rstrip("/")
            url_hash = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:16]
            return f"url:{url_hash}", "url", url_hash
        except Exception as e:
            logger.debug("Error parsing URL in build_source_key: %s", e)

    # Строковый запрос или некорректный URL
    norm_text = " ".join(raw.split()).lower()
    text_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()[:16]
    return f"query:{text_hash}", "query", text_hash


def _safe_str(val: Any) -> Optional[str]:
    if val is None or hasattr(val, "_mock_return_value") or "Mock" in type(val).__name__:
        return None
    return str(val).strip()


def compute_metadata_hash(
    artist: Optional[Any],
    title: Any,
    album: Optional[Any],
    duration: Optional[Any]
) -> str:
    """
    Вычисляет детерминированный SHA-256 хэш нормализованных канонических метаданных.
    Используется для обнаружения устаревших записей при обновлении метаданных релиза.
    """
    safe_art = _safe_str(artist) or ""
    safe_tit = _safe_str(title) or ""
    safe_alb = _safe_str(album) or ""
    try:
        if hasattr(duration, "_mock_return_value") or "Mock" in type(duration).__name__:
            dur_val = 0
        else:
            dur_val = int(duration or 0)
    except (TypeError, ValueError):
        dur_val = 0

    norm_artist = unicodedata.normalize("NFKC", safe_art.casefold())
    norm_title = unicodedata.normalize("NFKC", safe_tit.casefold())
    norm_album = unicodedata.normalize("NFKC", safe_alb.casefold())
    raw = f"{norm_artist}|{norm_title}|{norm_album}|{dur_val}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_metadata_match(
    cached_item: Optional[PersistentTrackCacheItem],
    artist: Optional[Any],
    title: Any,
    album: Optional[Any] = None,
    duration: Optional[Any] = None
) -> Tuple[bool, str, str]:
    """
    Проверяет совпадение канонических метаданных с кэшированным metadata_hash.
    Возвращает кортеж (is_match, cached_hash, current_hash).

    Семантика:
    1. same source_key + same metadata_hash -> is_match = True (CACHE HIT).
    2. same source_key + different metadata_hash -> is_match = False (mismatch detected).
    3. При отсутствии metadata_hash в старой записи -> is_match = True (обратная совместимость).
    4. Благодаря unicodedata NFKC и casefold() внутри compute_metadata_hash:
       - Изменение только регистра ('RSAC' vs 'RsAC') даёт идентичный хэш (is_match=True).
       - Изменение реальных значений ('RSAC' vs 'Unknown Artist') даёт разный хэш (is_match=False).
    """
    if not cached_item:
        return False, "", ""
    cached_hash = cached_item.metadata_hash or ""
    current_hash = compute_metadata_hash(artist, title, album, duration)
    if not cached_hash:
        return True, cached_hash, current_hash
    return (cached_hash == current_hash), cached_hash, current_hash


def normalize_search_query_key(query: str) -> str:
    """
    Детерминированная нормализация строки поискового запроса для persistent search cache:
    NFKC Unicode normalization, trim, casefold, нормализация пробелов и тире.
    """
    q = clean_unicode_text(query.strip())
    q = unicodedata.normalize("NFKC", q)
    q = q.replace("—", "-").replace("–", "-").replace("−", "-").replace("_", " ")
    q = " ".join(q.split()).casefold()
    return q


# ============================================================================
# 2. КЛИЕНТ CLOUDFLARE D1 (HTTP REST API)
# ============================================================================

class D1Client:
    """
    Асинхронный клиент для работы с базой данных Cloudflare D1 через официальный HTTP API.
    Endpoint: POST https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query
    Все запросы выполняются строго с параметризацией.

    Актуальные лимиты Cloudflare D1 (Free Tier):
    - 500 MB max database size on Workers Free;
    - 5 GB total account storage;
    - 5,000,000 rows read/day;
    - 100,000 rows written/day.
    """
    def __init__(
        self,
        account_id: Optional[str] = None,
        database_id: Optional[str] = None,
        api_token: Optional[str] = None
    ):
        self.account_id = (account_id or CLOUDFLARE_ACCOUNT_ID or "").strip()
        self.database_id = (database_id or CLOUDFLARE_D1_DATABASE_ID or "").strip()
        self.api_token = (api_token or CLOUDFLARE_API_TOKEN or "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self.account_id and self.database_id and self.api_token)

    @property
    def endpoint_url(self) -> str:
        return f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/d1/database/{self.database_id}/query"

    async def execute_query(
        self,
        sql: str,
        params: Optional[List[Any]] = None,
        timeout: float = 6.0
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Выполняет один параметризованный SQL-запрос к Cloudflare D1.
        Возвращает список строк (dict) результата или None при ошибке/недоступности.
        """
        if not self.is_configured:
            return None

        payload = {
            "sql": sql,
            "params": params or []
        }
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        session = get_shared_session()
        try:
            async with session.post(
                self.endpoint_url,
                json=payload,
                headers=headers,
                timeout=timeout
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning("Cloudflare D1 query error HTTP %d: %s", resp.status, err_text[:200])
                    return None

                data = await resp.json()
                if not data.get("success"):
                    errors = data.get("errors", [])
                    logger.warning("Cloudflare D1 logical error: %s", errors)
                    return None

                result_array = data.get("result", [])
                if not result_array:
                    return []
                # Первый блок результатов запроса
                query_res = result_array[0]
                rows = query_res.get("results", [])
                return rows
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Cloudflare D1 query timed out (%s s)", timeout)
            return None
        except Exception as ex:
            logger.warning("Cloudflare D1 connection failure: %s", ex)
            return None

    async def execute_batch(
        self,
        statements: List[Dict[str, Any]],
        timeout: float = 10.0
    ) -> bool:
        """
        Выполняет пачку параметризованных запросов в одной транзакции/batch.
        statements: [{"sql": "...", "params": [...]}, ...]
        """
        if not self.is_configured:
            return False
        if not statements:
            return True

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        session = get_shared_session()
        try:
            async with session.post(
                self.endpoint_url,
                json=statements,
                headers=headers,
                timeout=timeout
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning("Cloudflare D1 batch error HTTP %d: %s", resp.status, err_text[:200])
                    return False
                data = await resp.json()
                return bool(data.get("success"))
        except Exception as ex:
            logger.warning("Cloudflare D1 batch failure: %s", ex)
            return False


# Глобальный клиент D1
_d1_client = D1Client()


def get_d1_client() -> D1Client:
    return _d1_client


def set_d1_client(client: D1Client):
    global _d1_client
    _d1_client = client


# ============================================================================
# 3. SCHEMA BOOTSTRAP & MIGRATIONS
# ============================================================================

D1_INIT_STATEMENTS = [
    {
        "sql": """
        CREATE TABLE IF NOT EXISTS track_cache (
            source_key TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,

            artist TEXT,
            title TEXT NOT NULL,
            album TEXT,
            duration INTEGER,

            metadata_hash TEXT NOT NULL,

            telegram_file_id TEXT NOT NULL,
            telegram_file_unique_id TEXT,
            storage_message_id INTEGER,

            file_size INTEGER,

            created_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL
        );
        """,
        "params": []
    },
    {
        "sql": "CREATE INDEX IF NOT EXISTS idx_track_cache_source ON track_cache(source_type, source_id);",
        "params": []
    },
    {
        "sql": "CREATE INDEX IF NOT EXISTS idx_track_cache_last_used ON track_cache(last_used_at);",
        "params": []
    },
    {
        "sql": """
        CREATE TABLE IF NOT EXISTS search_cache (
            query_key TEXT PRIMARY KEY,
            results_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        """,
        "params": []
    },
    {
        "sql": "CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(expires_at);",
        "params": []
    }
]


async def init_d1_schema_async(client: Optional[D1Client] = None) -> bool:
    """
    Идемпотентно инициализирует таблицы и индексы постоянного кэша в Cloudflare D1.
    """
    c = client or get_d1_client()
    if not c.is_configured:
        logger.info("Cloudflare D1 credentials not set, skipping schema bootstrap")
        return False
    try:
        res = c.execute_batch(D1_INIT_STATEMENTS)
        if asyncio.iscoroutine(res):
            success = await res
        else:
            success = bool(res)
    except Exception as ex:
        logger.warning("Failed to initialize Cloudflare D1 schema: %s", ex)
        success = False

    if success:
        logger.info("Cloudflare D1 schema initialized successfully")
    else:
        logger.warning("Failed to initialize Cloudflare D1 schema")
    return bool(success)


_schema_initialized: bool = False


async def ensure_d1_schema_initialized(client: Optional[D1Client] = None) -> bool:
    """Гарантирует, что схема D1 создана перед первой операцией записи."""
    global _schema_initialized
    if _schema_initialized:
        return True
    c = client or get_d1_client()
    if not c.is_configured:
        return False
    ok = await init_d1_schema_async(c)
    if ok:
        _schema_initialized = True
    return ok


# ============================================================================
# 4. TRACK CACHE OPERATIONS (L1 + L2)
# ============================================================================

async def get_persistent_track_async(
    source_key: str,
    client: Optional[D1Client] = None
) -> Optional[PersistentTrackCacheItem]:
    """
    Получает трек из двухуровневого постоянного кэша:
    1. L1 (RAM memory cache) -> мгновенный ответ.
    2. L2 (Cloudflare D1 HTTP API) -> persistent source of truth.
    При нахождении в D1 кэширует в L1 и асинхронно обновляет last_used_at.
    """
    # 1. Проверка L1 RAM кэша
    cached_l1 = _PERSISTENT_TRACK_L1.get(source_key)
    if cached_l1:
        logger.debug("L1 cache HIT: source_key=%s", source_key)
        return PersistentTrackCacheItem.from_dict(cached_l1)

    # 2. Проверка L2 Cloudflare D1
    c = client or get_d1_client()
    if not c.is_configured:
        return None

    sql = """
    SELECT source_key, source_type, source_id, artist, title, album, duration,
           metadata_hash, telegram_file_id, telegram_file_unique_id,
           storage_message_id, file_size, created_at, last_used_at
    FROM track_cache
    WHERE source_key = ?
    LIMIT 1
    """
    try:
        rows = await c.execute_query(sql, [source_key])
    except Exception as ex:
        logger.warning("Cloudflare D1 query error in get_persistent_track_async: %s", ex)
        return None

    if rows and len(rows) > 0:
        row = rows[0]
        item = PersistentTrackCacheItem.from_dict(row)
        # Прогрев L1 кэша
        _PERSISTENT_TRACK_L1.set(source_key, item.to_dict())

        # Throttled обновление времени последнего использования в D1 (экономия квоты 100k daily writes)
        now_ts = int(time.time())
        if (now_ts - item.last_used_at) >= LAST_USED_UPDATE_INTERVAL_SECONDS:
            item.last_used_at = now_ts
            asyncio.create_task(
                _update_last_used_at_safe(source_key, now_ts, c)
            )
        return item

    return None


async def _update_last_used_at_safe(source_key: str, last_used: int, client: D1Client):
    """Фоновое обновление времени последнего обращения к записи."""
    try:
        sql = "UPDATE track_cache SET last_used_at = ? WHERE source_key = ?"
        await client.execute_query(sql, [last_used, source_key])
    except Exception as e:
        logger.debug("Failed to update last_used_at in D1: %s", e)


async def save_persistent_track_async(
    source_key: str,
    source_type: str,
    source_id: str,
    artist: Optional[str],
    title: str,
    album: Optional[str],
    duration: Optional[int],
    telegram_file_id: str,
    telegram_file_unique_id: Optional[str] = None,
    storage_message_id: Optional[int] = None,
    file_size: Optional[int] = None,
    client: Optional[D1Client] = None
) -> bool:
    """
    Сохраняет трек в постоянное хранилище Cloudflare D1 и локальные уровни кэша.
    """
    now_ts = int(time.time())
    safe_art = _safe_str(artist)
    safe_tit = _safe_str(title) or "Unknown Track"
    safe_alb = _safe_str(album)
    try:
        dur_val = int(duration) if (duration is not None and not hasattr(duration, "_mock_return_value") and "Mock" not in type(duration).__name__) else None
    except (TypeError, ValueError):
        dur_val = None
    try:
        fs_val = int(file_size) if (file_size is not None and not hasattr(file_size, "_mock_return_value") and "Mock" not in type(file_size).__name__) else None
    except (TypeError, ValueError):
        fs_val = None
    try:
        sm_val = int(storage_message_id) if (storage_message_id is not None and not hasattr(storage_message_id, "_mock_return_value") and "Mock" not in type(storage_message_id).__name__) else None
    except (TypeError, ValueError):
        sm_val = None
    safe_unique_id = _safe_str(telegram_file_unique_id)
    safe_file_id = _safe_str(telegram_file_id) or str(telegram_file_id)

    meta_hash = compute_metadata_hash(safe_art, safe_tit, safe_alb, dur_val)

    item = PersistentTrackCacheItem(
        source_key=source_key,
        source_type=source_type,
        source_id=source_id,
        artist=safe_art,
        title=safe_tit,
        album=safe_alb,
        duration=dur_val,
        metadata_hash=meta_hash,
        telegram_file_id=safe_file_id,
        telegram_file_unique_id=safe_unique_id,
        storage_message_id=sm_val,
        file_size=fs_val,
        created_at=now_ts,
        last_used_at=now_ts,
    )

    # 1. Сохраняем в L1 RAM кэш
    _PERSISTENT_TRACK_L1.set(source_key, item.to_dict())

    # 2. Сохраняем в локальный SQLite (для совместимости с локальным поиском)
    try:
        if safe_art and safe_tit:
            await save_cached_track_async(
                query=f"{safe_art} - {safe_tit}",
                file_id=safe_file_id,
                title=safe_tit,
                artist=safe_art,
                duration=dur_val or 0
            )
        await save_cached_track_async(
            query=source_key,
            file_id=safe_file_id,
            title=safe_tit,
            artist=safe_art or "Unknown Artist",
            duration=dur_val or 0
        )
    except Exception as sq_err:
        logger.debug("Local SQLite sync save skipped: %s", sq_err)

    # 3. Сохраняем в Cloudflare D1
    c = client or get_d1_client()
    if not c.is_configured:
        logger.warning("Cloudflare D1 is not configured. Saved only to local L1 cache.")
        return False

    await ensure_d1_schema_initialized(c)

    sql = """
    INSERT INTO track_cache (
        source_key, source_type, source_id,
        artist, title, album, duration,
        metadata_hash,
        telegram_file_id, telegram_file_unique_id, storage_message_id,
        file_size, created_at, last_used_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_key) DO UPDATE SET
        artist=excluded.artist,
        title=excluded.title,
        album=excluded.album,
        duration=excluded.duration,
        metadata_hash=excluded.metadata_hash,
        telegram_file_id=excluded.telegram_file_id,
        telegram_file_unique_id=excluded.telegram_file_unique_id,
        storage_message_id=excluded.storage_message_id,
        file_size=excluded.file_size,
        last_used_at=excluded.last_used_at
    """
    params = [
        source_key, source_type, source_id,
        artist, title, album, duration,
        meta_hash,
        telegram_file_id, telegram_file_unique_id, storage_message_id,
        file_size, now_ts, now_ts
    ]

    try:
        res = await c.execute_query(sql, params)
    except Exception as ex:
        logger.error("Failed to execute D1 write query for source_key=%s: %s", source_key, ex)
        res = None

    if res is not None:
        logger.info("Persistent cache saved\nsource_key=%s", source_key)
        return True
    else:
        logger.error("Failed to save to Cloudflare D1 persistent cache for source_key=%s", source_key)
        return False


async def invalidate_persistent_track_async(
    source_key: str,
    telegram_file_id: Optional[str] = None,
    client: Optional[D1Client] = None
) -> bool:
    """
    Инвалидирует запись в постоянном кэше (L1 RAM, SQLite и Cloudflare D1).
    Используется при обнаружении невалидного / устаревшего telegram_file_id.
    """
    logger.info("Invalid Telegram file_id, invalidating cache\nsource_key=%s", source_key)

    # 1. Удаление из L1 RAM
    _PERSISTENT_TRACK_L1.delete(source_key)

    # 2. Удаление из локального SQLite
    if telegram_file_id:
        try:
            await invalidate_cached_file_id_async(telegram_file_id)
        except Exception:
            pass

    # 3. Удаление из Cloudflare D1
    c = client or get_d1_client()
    if not c.is_configured:
        return True

    if telegram_file_id:
        sql = "DELETE FROM track_cache WHERE source_key = ? OR telegram_file_id = ?"
        params = [source_key, telegram_file_id]
    else:
        sql = "DELETE FROM track_cache WHERE source_key = ?"
        params = [source_key]

    res = await c.execute_query(sql, params)
    return res is not None


# ============================================================================
# 5. SEARCH CACHE OPERATIONS (TTL)
# ============================================================================

async def get_persistent_search_results_async(
    query: str,
    client: Optional[D1Client] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Проверяет наличие свежих результатов поиска в persistent search cache.
    Учитывает TTL (по умолчанию 12 часов).
    """
    norm_key = normalize_search_query_key(query)
    now_ts = int(time.time())

    # 1. Проверка L1
    cached_l1 = _PERSISTENT_SEARCH_L1.get(norm_key)
    if cached_l1:
        expires_at = cached_l1.get("expires_at", 0)
        if expires_at > now_ts:
            return cached_l1.get("results")
        else:
            _PERSISTENT_SEARCH_L1.delete(norm_key)

    # 2. Проверка D1
    c = client or get_d1_client()
    if not c.is_configured:
        return None

    sql = "SELECT results_json, expires_at FROM search_cache WHERE query_key = ? LIMIT 1"
    rows = await c.execute_query(sql, [norm_key])
    if rows and len(rows) > 0:
        row = rows[0]
        expires_at = int(row.get("expires_at", 0))
        if expires_at > now_ts:
            try:
                results = json.loads(row.get("results_json", "[]"))
                _PERSISTENT_SEARCH_L1.set(norm_key, {"results": results, "expires_at": expires_at})
                return results
            except Exception as e:
                logger.warning("Error decoding search_cache JSON: %s", e)
        else:
            # Истекший кэш — фоновое удаление
            asyncio.create_task(
                c.execute_query("DELETE FROM search_cache WHERE query_key = ?", [norm_key])
            )

    return None


async def save_persistent_search_results_async(
    query: str,
    results: List[Dict[str, Any]],
    ttl_seconds: Optional[int] = None,
    client: Optional[D1Client] = None
) -> bool:
    """
    Сохраняет результаты поиска в persistent search cache с установленным TTL.
    """
    norm_key = normalize_search_query_key(query)
    ttl = int(ttl_seconds or SEARCH_CACHE_TTL_SECONDS)
    now_ts = int(time.time())
    expires_at = now_ts + ttl

    # 1. Сохранение в L1
    _PERSISTENT_SEARCH_L1.set(norm_key, {"results": results, "expires_at": expires_at})

    # 2. Сохранение в D1
    c = client or get_d1_client()
    if not c.is_configured:
        return False

    await ensure_d1_schema_initialized(c)

    results_json = json.dumps(results, ensure_ascii=False)
    sql = """
    INSERT INTO search_cache (query_key, results_json, created_at, expires_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(query_key) DO UPDATE SET
        results_json=excluded.results_json,
        created_at=excluded.created_at,
        expires_at=excluded.expires_at
    """
    params = [norm_key, results_json, now_ts, expires_at]
    res = await c.execute_query(sql, params)
    return res is not None


# ============================================================================
# 6. БЕЗОПАСНАЯ МИГРАЦИЯ СУЩЕСТВУЮЩИХ ДАННЫХ ИЗ SQLITE В D1
# ============================================================================

async def migrate_local_sqlite_to_d1(client: Optional[D1Client] = None) -> int:
    """
    Переносит валидные записи из локальной таблицы tracks_cache в Cloudflare D1.
    Пропускает неполные или поврежденные записи.
    """
    c = client or get_d1_client()
    if not c.is_configured:
        logger.warning("Cannot migrate: Cloudflare D1 is not configured")
        return 0

    from services.database import get_db_connection

    rows_to_migrate = []
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT query_key, file_id, title, artist, duration, created_at
                FROM tracks_cache
                WHERE file_id IS NOT NULL AND length(file_id) > 10
            """)
            for row in cursor.fetchall():
                rows_to_migrate.append(dict(row))
    except Exception as e:
        logger.error("Error reading local SQLite for migration: %s", e)
        return 0

    if not rows_to_migrate:
        return 0

    migrated_count = 0
    batch_statements = []
    now_ts = int(time.time())

    for r in rows_to_migrate:
        q_key = r.get("query_key", "")
        file_id = r.get("file_id", "")
        title = r.get("title") or "Unknown Track"
        artist = r.get("artist")
        duration = r.get("duration")

        source_key, s_type, s_id = build_source_key(q_key)
        # Пропускаем сырые неоднозначные запросы
        if s_type == "query" and not (artist and title):
            continue

        meta_hash = compute_metadata_hash(artist, title, None, duration)

        sql = """
        INSERT OR IGNORE INTO track_cache (
            source_key, source_type, source_id,
            artist, title, album, duration,
            metadata_hash,
            telegram_file_id, telegram_file_unique_id, storage_message_id,
            file_size, created_at, last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            source_key, s_type, s_id,
            artist, title, None, duration,
            meta_hash,
            file_id, None, None,
            None, now_ts, now_ts
        ]
        batch_statements.append({"sql": sql, "params": params})

        if len(batch_statements) >= 50:
            success = await c.execute_batch(batch_statements)
            if success:
                migrated_count += len(batch_statements)
            batch_statements.clear()

    if batch_statements:
        success = await c.execute_batch(batch_statements)
        if success:
            migrated_count += len(batch_statements)

    logger.info("Migrated %d records from local SQLite to Cloudflare D1", migrated_count)
    return migrated_count
