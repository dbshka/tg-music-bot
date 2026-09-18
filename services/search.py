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
from services.extractor import extract_core_title_words, compute_title_match_ratio

logger = logging.getLogger(__name__)

# Таблицы для конвертации раскладки клавиатуры RU <-> EN
EN_LAYOUT = "`~qwertyuiop[]asdfghjkl;'zxcvbnm,./QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
RU_LAYOUT = "ёЁйцукенгшщзхъфывапролджэячсмитьбю.ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
EN_TO_RU = str.maketrans(EN_LAYOUT, RU_LAYOUT)
RU_TO_EN = str.maketrans(RU_LAYOUT, EN_LAYOUT)


def convert_keyboard_layout(text: str) -> str:
    """Конвертирует раскладку клавиатуры между RU и EN."""
    en_chars = sum(1 for c in text if 'a' <= c.lower() <= 'z')
    ru_chars = sum(1 for c in text if 'а' <= c.lower() <= 'я' or c in 'ёЁ')
    if en_chars > ru_chars:
        return text.translate(EN_TO_RU)
    elif ru_chars > 0:
        return text.translate(RU_TO_EN)
    return text


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


def _score_search_item(item: SearchItem, query: str) -> float:
    """
    Вычисляет оценку оригинальности и соответствия трека.
    Официальный студийный оригинал всегда получает наивысший балл и выходит на 1 место.
    """
    score = 0.0
    title_lower = item.title.lower()
    uploader_lower = (item.uploader or "").lower()
    q_lower = query.lower()

    # 1. Бонус официального Topic-канала (на YouTube все оригинальные студийные треки выходят на Topic)
    if " - topic" in uploader_lower or uploader_lower.endswith("topic"):
        score += 500.0

    # 2. Бонус официального аудио / Vevo
    if "official audio" in title_lower or "official release" in title_lower:
        score += 300.0
    if "vevo" in uploader_lower or "official" in uploader_lower:
        score += 150.0

    # 3. Штраф за неоригинальные модификации (если пользователь явно их не искал)
    unwanted_modifiers = [
        "remix", "rmx", "slowed", "super slowed", "sped up", "speed up",
        "nightcore", "daycore", "reverb", "lyrics", "текст", "караоке", "karaoke",
        "instrumental", "минус", "минусовка", "reaction", "реакция", "разбор",
        "cover", "кавер", "live", "лайв", "концерт", "bass boosted", "8d", "16d",
        "parody", "пародия", "family guy", "meme", "edit"
    ]
    user_requested = {m for m in unwanted_modifiers if m in q_lower}
    for mod in unwanted_modifiers:
        if mod in title_lower and mod not in user_requested:
            score -= 400.0

    # 4. Адекватный хронометраж для песни (2–5 минут)
    if item.duration:
        if 120 <= item.duration <= 320:
            score += 100.0
        elif item.duration < 60:
            score -= 300.0  # Слишком короткий отрывок/превью
        elif item.duration > 600:
            score -= 500.0  # Слишком длинное видео

    # 5. Семантическое соответствие ключевым словам названия
    core_words = extract_core_title_words(query)
    if core_words:
        ratio = compute_title_match_ratio(item.title, core_words)
        score += ratio * 350.0

    return score


def _extract_source_items(src: str, query: str, limit: int) -> List[dict]:
    """Синхронно получает легковесные метаданные кандидатов поиска."""
    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 5,
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
                title = title.replace("’", "'").replace("‘", "'").replace("`", "'")

                dur = int(e.get("duration") or 0)
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


def _search_and_rank(query: str, limit: int = 30) -> List[SearchItem]:
    """Выполняет поиск по источникам и ранжирует так, чтобы оригинал был всегда первым."""
    yt_req_limit = 8 if limit <= 5 else 20
    sc_req_limit = 5 if limit <= 5 else 15
    timeout = 6.0 if limit <= 5 else 12.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_yt = executor.submit(_extract_source_items, "yt", query, yt_req_limit)
        f_sc = executor.submit(_extract_source_items, "sc", query, sc_req_limit)
        done, _ = concurrent.futures.wait([f_yt, f_sc], timeout=timeout)
        try:
            yt_raw = f_yt.result() if f_yt in done else []
        except Exception as ex:
            logger.warning("YT extraction failed for '%s': %s", query, ex)
            yt_raw = []
        try:
            sc_raw = f_sc.result() if f_sc in done else []
        except Exception as ex:
            logger.warning("SC extraction failed for '%s': %s", query, ex)
            sc_raw = []

    seen_signatures = set()
    combined: List[SearchItem] = []

    def _sig(title: str, dur: int) -> str:
        clean = "".join(c for c in title.lower() if c.isalnum())
        return f"{clean}_{dur // 5}"

    for r in yt_raw + sc_raw:
        sig = _sig(r["title"], r["duration"])
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            combined.append(SearchItem(
                index=0,
                title=r["title"],
                uploader=r["uploader"],
                duration=r["duration"],
                url=r["url"],
                source=r["source"],
                thumbnail=r["thumbnail"]
            ))

    # Сортируем: оригинал ВСЕГДА на 1 месте!
    ranked = sorted(combined, key=lambda it: _score_search_item(it, query), reverse=True)
    for i, it in enumerate(ranked):
        it.index = i + 1
    return ranked[:limit]


def search_tracks_sync(query: str, limit: int = 30) -> Tuple[List[SearchItem], Optional[str]]:
    """
    Параллельный опрос YouTube и SoundCloud с дедупликацией.
    Всегда выводит оригинал первым.
    В САМУЮ ПОСЛЕДНЮЮ ОЧЕРЕДЬ: при 0 результатов пробует конвертацию раскладки и нечёткий поиск.
    Возвращает (items, corrected_query_or_none).
    """
    q_norm = unicodedata.normalize("NFC", query).strip()
    # 1. Основной поиск по исходному запросу
    items = _search_and_rank(q_norm, limit)
    if items:
        return items, None

    # -------------------------------------------------------------
    # ТОЛЬКО В САМУЮ ПОСЛЕДНЮЮ ОЧЕРЕДЬ: если основной поиск вернул 0 результатов
    # -------------------------------------------------------------
    # Попытка 1: инвертированная раскладка клавиатуры (RU <-> EN)
    flipped = convert_keyboard_layout(q_norm)
    if flipped.lower() != q_norm.lower():
        logger.info("Основной поиск '%s' пуст. Пробуем раскладку: '%s'", q_norm, flipped)
        items = _search_and_rank(flipped, limit)
        if items:
            return items, flipped

    # Попытка 2: нечёткий / расслабленный поиск по ключевым значимым словам
    words = q_norm.split()
    if len(words) >= 2:
        relaxed = " ".join(w for w in words if len(w) >= 3)
        if relaxed and relaxed.lower() != q_norm.lower():
            logger.info("Поиск '%s' пуст. Пробуем relaxed запрос: '%s'", q_norm, relaxed)
            items = _search_and_rank(relaxed, limit)
            if items:
                return items, relaxed

    return [], None


async def search_tracks_async(query: str, limit: int = 30) -> Tuple[List[SearchItem], Optional[str]]:
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
