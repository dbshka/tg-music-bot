import asyncio
import concurrent.futures
import html
import logging
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import yt_dlp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_cookies_info

logger = logging.getLogger(__name__)


@dataclass
class SearchItem:
    index: int
    title: str
    uploader: Optional[str]
    duration: int
    url: str
    source: str
    thumbnail: Optional[str] = None

    @property
    def formatted_duration(self) -> str:
        if not self.duration or self.duration <= 0:
            return ""
        m, s = divmod(self.duration, 60)
        return f"{m}:{s:02d}"


@dataclass
class SearchSession:
    query: str
    items: List[SearchItem]
    created_at: float


class SearchCache:
    """Кэш результатов поиска в оперативной памяти с автоочисткой по TTL."""
    def __init__(self, ttl_seconds: int = 3600):
        self._cache: Dict[str, SearchSession] = {}
        self._ttl = ttl_seconds

    def save(self, query: str, items: List[SearchItem]) -> str:
        self._cleanup()
        session_id = uuid.uuid4().hex[:10]
        self._cache[session_id] = SearchSession(query=query, items=items, created_at=time.time())
        return session_id

    def get(self, session_id: str) -> Optional[SearchSession]:
        self._cleanup()
        return self._cache.get(session_id)

    def _cleanup(self):
        now = time.time()
        expired = [sid for sid, sess in self._cache.items() if now - sess.created_at > self._ttl]
        for sid in expired:
            self._cache.pop(sid, None)


search_cache = SearchCache()


def _extract_source_items(src: str, query: str, limit: int) -> List[dict]:
    """Синхронно получает легковесные метаданные кандидатов поиска."""
    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
    }
    cookies_info = get_cookies_info()
    if src == "yt" and cookies_info.get("active"):
        opts["cookiefile"] = cookies_info["path"]
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "mweb", "ios"]}}

    raw_items = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            prefix = f"{src}search{limit}:"
            info = ydl.extract_info(f"{prefix}{query}", download=False)
            for e in (info.get("entries") or []):
                if not e:
                    continue
                u = e.get("webpage_url") or e.get("url") or e.get("id")
                if not u:
                    continue
                if not u.startswith("http") and src == "yt":
                    u = f"https://www.youtube.com/watch?v={u}"

                raw_title = e.get("title") or ""
                title = unicodedata.normalize("NFC", raw_title).strip()
                # Унифицируем фигурные кавычки и апострофы
                title = title.replace("’", "'").replace("‘", "'").replace("`", "'")

                dur = int(e.get("duration") or 0)
                # Фильтруем длинные видео (> 15 мин), если пользователь не искал альбом/микс
                q_lower = query.lower()
                if dur > 900 and not any(k in q_lower for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                    continue

                raw_items.append({
                    "title": title,
                    "uploader": e.get("uploader"),
                    "duration": dur,
                    "url": u,
                    "source": "youtube" if src == "yt" else "soundcloud",
                    "thumbnail": e.get("thumbnail")
                })
    except Exception as ex:
        logger.warning("Ошибка поиска по %s для '%s': %s", src, query, ex)

    return raw_items


def search_tracks_sync(query: str, limit: int = 30) -> List[SearchItem]:
    """Параллельный опрос YouTube и SoundCloud с дедупликацией."""
    q_norm = unicodedata.normalize("NFC", query).strip()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_yt = executor.submit(_extract_source_items, "yt", q_norm, 20)
        f_sc = executor.submit(_extract_source_items, "sc", q_norm, 15)
        yt_raw = f_yt.result()
        sc_raw = f_sc.result()

    seen_signatures = set()
    combined: List[SearchItem] = []

    def _sig(title: str, dur: int) -> str:
        # Упрощенная сигнатура для исключения явных дубликатов
        clean = "".join(c for c in title.lower() if c.isalnum())
        return f"{clean}_{dur // 5}"

    # Приоритет 1: результаты YouTube
    for r in yt_raw:
        sig = _sig(r["title"], r["duration"])
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            idx = len(combined) + 1
            combined.append(SearchItem(
                index=idx,
                title=r["title"],
                uploader=r["uploader"],
                duration=r["duration"],
                url=r["url"],
                source=r["source"],
                thumbnail=r["thumbnail"]
            ))

    # Приоритет 2: результаты SoundCloud
    for r in sc_raw:
        sig = _sig(r["title"], r["duration"])
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            idx = len(combined) + 1
            combined.append(SearchItem(
                index=idx,
                title=r["title"],
                uploader=r["uploader"],
                duration=r["duration"],
                url=r["url"],
                source=r["source"],
                thumbnail=r["thumbnail"]
            ))

    return combined[:limit]


async def search_tracks_async(query: str, limit: int = 30) -> List[SearchItem]:
    """Асинхронная обертка над поиском треков."""
    return await asyncio.to_thread(search_tracks_sync, query, limit)


def render_search_page(session_id: str, query: str, items: List[SearchItem], page: int = 0, page_size: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Формирует текст сообщения со списком треков и инлайн-клавиатуру
    в точности как в дизайне скриншота пользователя.
    """
    total_items = len(items)
    total_pages = (total_items + page_size - 1) // page_size if total_items > 0 else 1
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total_items)
    page_items = items[start_idx:end_idx]

    # 1. Текст сообщения
    lines = [f"<b>{html.escape(query)}</b>\n"]
    for i, item in enumerate(page_items, start=start_idx + 1):
        dur_str = f" <b>{item.formatted_duration}</b>" if item.formatted_duration else ""
        escaped_title = html.escape(item.title)
        lines.append(f"<b>{i}.</b> <i>{escaped_title}</i>{dur_str}")

    text = "\n".join(lines)

    # 2. Кнопки номеров треков (по 5 в ряд)
    keyboard_rows: List[List[InlineKeyboardButton]] = []
    current_row: List[InlineKeyboardButton] = []

    for i in range(start_idx, end_idx):
        btn_num = str(i + 1)
        btn = InlineKeyboardButton(
            text=btn_num,
            callback_data=f"ms:{session_id}:{i}"
        )
        current_row.append(btn)
        if len(current_row) == 5:
            keyboard_rows.append(current_row)
            current_row = []

    if current_row:
        keyboard_rows.append(current_row)

    # 3. Кнопки пагинации
    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"mspg:{session_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"mspg:{session_id}:{page + 1}"))

    if nav_row:
        keyboard_rows.append(nav_row)

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
