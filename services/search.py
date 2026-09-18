import asyncio
import concurrent.futures
import html
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import yt_dlp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from yt_dlp.extractor.common import SearchInfoExtractor
from yt_dlp.extractor.youtube import YoutubeTabBaseInfoExtractor
from yt_dlp.globals import extractors as _extractors_context

from config import get_cookies_info
from services.downloader import (
    _clean_audio_branding,
    get_current_youtube_proxy,
    get_sanitized_proxy_info,
    is_generic_artist_name,
)
from services.extractor import (
    compute_title_match_ratio,
    extract_core_title_words,
    extract_track_modifiers,
    resolve_canonical_track_info_async,
)
from services.identity import (
    clean_unicode_text,
    extract_modifiers,
    is_candidate_matching_modifiers,
    validate_artist_match,
)

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
    uploader: Optional[str] = None
    duration: int = 0
    url: str = ""
    source: str = ""
    thumbnail: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    channel: Optional[str] = None
    score: float = 0.0
    modifiers: Optional[Set[str]] = None
    clean_artist: Optional[str] = None
    clean_title: Optional[str] = None
    is_live: bool = False

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


# ============================================================================
# 1. PARSING & AUTHORITATIVE METADATA HELPERS
# ============================================================================

def extract_artist_title_from_query(query: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Извлекает (artist, title) из поискового запроса пользователя вида 'Исполнитель — Название'.
    Поддерживает варианты разделителей: ' -- ', '--', ' — ', '—', ' – ', '–', ' - '.
    """
    if not query:
        return None, None
    clean_q = clean_unicode_text(query).strip()
    for pattern in [r'\s*(?:--|—|–)\s*', r'\s+-\s+']:
        parts = re.split(pattern, clean_q, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None, None


def _clean_candidate_title(raw_title: str) -> str:
    """Очищает заголовок трека от лишних видео-приписок клипов (Official Video, Lyric Video и т.д.)."""
    if not raw_title:
        return ""
    cleaned = clean_unicode_text(raw_title).strip()
    cleaned = re.sub(
        r'\s*[\(\[](?:Official\s*(?:Music\s*)?Video|Official\s*Audio|Lyric\s*Video|Video|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
        '',
        cleaned,
        flags=re.IGNORECASE
    ).strip()
    cleaned = _clean_audio_branding(cleaned) or cleaned
    return cleaned.strip()


def resolve_canonical_candidate_metadata(
    candidate: Any,
    query_artist: Optional[str] = None,
    query_title: Optional[str] = None
) -> Tuple[str, str, Optional[str], int, Optional[str]]:
    """
    Определяет канонические метаданные трека строго по приоритету:
    1. YouTube Music metadata выбранного кандидата (candidate.artist, candidate.title, candidate.album).
    2. Обычные YouTube metadata кандидата (разделитель Artist - Title, uploader/channel без - Topic).
    3. Оригинальные метаданные аудиофайла (обрабатываются downloader-ом).
    4. Распарсенные artist/title из запроса — ТОЛЬКО как fallback, если у кандидата метаданные
       действительно отсутствуют или generic (например, 'Release').

    Возвращает: (canonical_artist, canonical_title, canonical_album, duration, thumbnail_url)
    """
    def _safe_str(val: Any) -> Optional[str]:
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    raw_title = _safe_str(getattr(candidate, "title", None) if not isinstance(candidate, dict) else candidate.get("title")) or ""
    raw_artist = _safe_str(getattr(candidate, "artist", None) if not isinstance(candidate, dict) else candidate.get("artist"))
    raw_uploader = _safe_str(getattr(candidate, "uploader", None) if not isinstance(candidate, dict) else candidate.get("uploader"))
    raw_channel = _safe_str(getattr(candidate, "channel", None) if not isinstance(candidate, dict) else candidate.get("channel"))
    album = _safe_str(getattr(candidate, "album", None) if not isinstance(candidate, dict) else candidate.get("album"))

    dur_raw = getattr(candidate, "duration", None) if not isinstance(candidate, dict) else candidate.get("duration")
    try:
        duration = int(dur_raw or 0)
    except (TypeError, ValueError):
        duration = 0

    thumb = _safe_str(getattr(candidate, "thumbnail", None) if not isinstance(candidate, dict) else candidate.get("thumbnail"))

    artist = None
    title = None

    # ПРИОРИТЕТ 1: YouTube Music metadata (кандидат имеет структурированного исполнителя)
    if raw_artist and not is_generic_artist_name(raw_artist):
        cleaned_art = _clean_audio_branding(raw_artist)
        if cleaned_art and not is_generic_artist_name(cleaned_art):
            artist = cleaned_art.strip()
            if raw_title:
                cleaned_title = _clean_candidate_title(raw_title)
                if cleaned_title:
                    title = cleaned_title

    # ПРИОРИТЕТ 2: Обычные YouTube metadata кандидата
    if not artist or is_generic_artist_name(artist):
        cleaned_raw_title = _clean_candidate_title(raw_title)
        # 2a. Разделитель в названии "Исполнитель - Название"
        for sep in [" - ", " — ", " – ", " -- "]:
            if sep in cleaned_raw_title:
                parts = cleaned_raw_title.split(sep, 1)
                cand_art = _clean_audio_branding(parts[0].strip()) or parts[0].strip()
                cand_tit = _clean_audio_branding(parts[1].strip()) or parts[1].strip()
                if cand_art and not is_generic_artist_name(cand_art):
                    artist = cand_art
                    title = cand_tit
                    break

        # 2b. uploader / channel без "- Topic"
        if not artist or is_generic_artist_name(artist):
            raw_up = (raw_uploader or raw_channel or "").replace(" - Topic", "").replace("- Topic", "").replace(" – Topic", "").strip()
            cleaned_up = _clean_audio_branding(raw_up) if raw_up else ""
            if cleaned_up and not is_generic_artist_name(cleaned_up):
                artist = cleaned_up

    # Если title еще не определен, но есть raw_title
    if not title and raw_title:
        cand_tit_clean = _clean_candidate_title(raw_title)
        if cand_tit_clean:
            title = cand_tit_clean

    # ПРИОРИТЕТ 4: Fallback на распарсенный запрос (ТОЛЬКО если у кандидата нет валидных метаданных)
    if not artist or is_generic_artist_name(artist):
        if query_artist and not is_generic_artist_name(query_artist):
            artist = _clean_audio_branding(query_artist) or query_artist.strip()
        else:
            artist = "Unknown Artist"

    if not title:
        if query_title and query_title.strip():
            title = _clean_audio_branding(query_title) or query_title.strip()
        else:
            title = "Unknown Track"

    # Имя альбома никогда не берется из поискового запроса
    # Длительность никогда не берется из поискового запроса

    return artist, title, album, duration, thumb


def _parse_candidate_title_artist(
    raw_title: str,
    uploader: Optional[str],
    query_artist: Optional[str] = None,
    query_title: Optional[str] = None,
    candidate_artist: Optional[str] = None
) -> Tuple[str, str]:
    """
    Разделяет строку на исполнителя и название трека с удалением брендинга.
    Сохраняет обратную совместимость для существующих тестов,
    делегируя определение канонических метаданных функции resolve_canonical_candidate_metadata.
    """
    dummy_item = SearchItem(
        index=0,
        title=raw_title,
        uploader=uploader,
        artist=candidate_artist
    )
    art, tit, _, _, _ = resolve_canonical_candidate_metadata(
        dummy_item,
        query_artist=query_artist,
        query_title=query_title
    )
    return art, tit


# ============================================================================
# 2. YOUTUBE MUSIC SEARCH EXTRACTOR (Custom ytmsearch implementation)
# ============================================================================

class YoutubeMusicSearchIE(YoutubeTabBaseInfoExtractor, SearchInfoExtractor):
    """
    Кастомный экстрактор для нативного поиска по YouTube Music (песни / studio releases).
    Использует клиент web_music и секцию #songs.
    Парсит внутренние JSON-структуры Innertube (musicResponsiveListItemRenderer).
    """
    IE_DESC = 'YouTube Music search (custom extractor)'
    IE_NAME = 'youtube:music:search'
    _SEARCH_KEY = 'ytmsearch'
    _SEARCH_PARAMS = 'EgWKAQIIAWoKEAoQAxAEEAkQBQ=='  # Songs section

    def _search_results(self, query):
        return super()._search_results(query, self._SEARCH_PARAMS, default_client='web_music')

    def _music_reponsive_list_entry(self, renderer):
        vid = renderer.get('playlistItemData', {}).get('videoId')
        cols = renderer.get('flexColumns', [])
        title = None
        artist = None
        duration = None
        album = None
        if len(cols) > 0:
            runs0 = cols[0].get('musicResponsiveListItemFlexColumnRenderer', {}).get('text', {}).get('runs', [])
            if runs0:
                title = runs0[0].get('text')
        if len(cols) > 1:
            runs1 = cols[1].get('musicResponsiveListItemFlexColumnRenderer', {}).get('text', {}).get('runs', [])
            texts = [r.get('text') for r in runs1 if r.get('text') and r.get('text').strip() != '•']
            if texts:
                artist = texts[0]
                for t in texts[1:]:
                    clean_t = t.strip()
                    if re.match(r'^\d+:\d+$', clean_t):
                        pts = clean_t.split(':')
                        try:
                            duration = int(pts[0]) * 60 + int(pts[1])
                        except Exception:
                            pass
                    elif not album and clean_t not in ('Song', 'Песня', 'Track', 'Single', 'Сингл', 'EP'):
                        album = clean_t
        thumbs = renderer.get('thumbnail', {}).get('musicThumbnailRenderer', {}).get('thumbnail', {}).get('thumbnails', [])
        thumb_url = thumbs[-1].get('url') if thumbs else None
        if vid:
            return {
                '_type': 'url',
                'url': f'https://www.youtube.com/watch?v={vid}',
                'id': vid,
                'title': title,
                'artist': artist,
                'uploader': None,   # Семантически на YT Music нет uploader-канала, есть исполнитель (artist)
                'channel': artist,
                'duration': duration,
                'thumbnail': thumb_url,
                'album': album,
                '_source': 'ytmusic'
            }
        return super()._music_reponsive_list_entry(renderer)


def register_ytmsearch_extractor():
    """Гарантирует регистрацию кастомного экстрактора ytmsearch в yt-dlp."""
    try:
        import yt_dlp.extractor
        yt_dlp.extractor.import_extractors()
        if 'YoutubeMusicSearchIE' not in _extractors_context.value:
            _extractors_context.value = {'YoutubeMusicSearchIE': YoutubeMusicSearchIE, **_extractors_context.value}
    except Exception as e:
        logger.debug("ytmsearch extractor registration notice: %s", e)


register_ytmsearch_extractor()


# ============================================================================
# 3. LIVE / CONCERT / PERFORMANCE DETECTION & SCORING
# ============================================================================

LIVE_MARKERS = {
    "live", "лайв", "концерт", "performance", "concert", "festival",
    "выступление", "live version", "concert version", "live performance",
    "en vivo", "ao vivo", "dal vivo", "live at", "live in", "live from",
    "live @", "tiny desk", "colors show", "live lounge"
}

UNWANTED_DEFAULT_MODIFIERS = {
    "cover", "кавер", "karaoke", "караоке", "tribute", "parody", "пародия"
}

VENUE_EVENT_PATTERNS = [
    re.compile(r'[\(\[\{][^\)\]\}]*(?:sentrum|stadium|festival|venue|arena|crocus|glavclub|стерео\s*плаза|stereo\s*plaza|фестиваль|стадион|выступление|концерт|олимпийский)[^\)\]\}]*[\)\]\}]', re.IGNORECASE),
    re.compile(r'[\(\[\{][^\)\]\}]*(?:kiev|kyiv|moscow|london|paris|msk|spb|мск|спб|питер|минск)[^\)\]\}]*[\)\]\}]', re.IGNORECASE),
    re.compile(r'@\s*(?:\d{2}\.\d{2}\.\d{2}|\d{4}|msk|мск|spb|спб|лес)', re.IGNORECASE),
    re.compile(r'\b(?:live\s+at|live\s+in|concert\s+at|concert\s+in|выступление\s+в)\s+[\w\s]+', re.IGNORECASE),
    re.compile(r'\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b'),
    re.compile(r'\b(?:клуб|club)\s+[\w\s]+', re.IGNORECASE)
]


def detect_is_live(text: str) -> bool:
    """
    Определяет, является ли кандидат записью живого выступления, концерта или фестиваля.
    Не штрафует обычные треки, где город является частью названия.
    """
    cleaned = text.lower()
    for m in LIVE_MARKERS:
        if re.search(r'(?<!\w)' + re.escape(m) + r'(?!\w)', cleaned):
            return True
    for pat in VENUE_EVENT_PATTERNS:
        if pat.search(cleaned):
            return True
    return False


def _find_studio_reference_duration(
    candidates: List[SearchItem],
    q_artist: Optional[str],
    q_title: Optional[str]
) -> Optional[int]:
    """
    Определяет эталонный хронометраж студийного релиза.
    Сначала ищет среди официальных треков YouTube Music, затем среди YouTube Topic-релизов.
    """
    # 1. Приоритет: официальный трек YouTube Music с совпадением названия
    for c in candidates:
        if c.source == "ytmusic" and not c.is_live and c.duration and 45 <= c.duration <= 900:
            if q_title:
                core_words = extract_core_title_words(q_title, artist=q_artist)
                if core_words and compute_title_match_ratio(c.title, core_words) >= 0.8:
                    cand_art = c.artist or c.uploader or c.channel
                    if not q_artist or not cand_art or validate_artist_match(q_artist, cand_art):
                        return c.duration
            else:
                return c.duration

    # 2. Поиск среди YouTube Topic-каналов (- Topic)
    for c in candidates:
        up = (c.uploader or "").lower()
        ch = (c.channel or "").lower()
        if ("- topic" in up or "- topic" in ch) and not c.is_live and c.duration and 45 <= c.duration <= 900:
            if q_title:
                core_words = extract_core_title_words(q_title, artist=q_artist)
                if core_words and compute_title_match_ratio(c.title, core_words) >= 0.8:
                    cand_art = c.artist or c.uploader or c.channel
                    if not q_artist or not cand_art or validate_artist_match(q_artist, cand_art):
                        return c.duration

    return None


def _score_inline_candidate(
    item: SearchItem,
    query_artist: Optional[str],
    query_title: Optional[str],
    requested_modifiers: Set[str],
    canonical_duration: Optional[int] = None,
) -> float:
    """
    Вычисляет оценку кандидата для Inline Mode:
    1. Предпочитает: studio, official audio, official music source, Topic, YT Music.
    2. Понижает/отбрасывает: live, concert, performance, festival, cover, karaoke, если не запрошены.
    3. Если пользователь явно запросил live/concert/acoustic, поощряет соответствующие версии.
    4. Строго защищает от подозрительной длительности при известном эталонном хронометраже.
    """
    score = 1000.0
    title_lower = (item.title or "").lower()
    uploader_lower = (item.uploader or item.channel or "").lower()
    artist_lower = (item.artist or "").lower()
    full_text = f"{title_lower} {uploader_lower} {artist_lower}".strip()

    live_keywords = {"live", "лайв", "концерт", "performance", "concert", "festival", "выступление"}
    is_live_requested = bool(requested_modifiers & live_keywords)

    # 1. Проверка живого выступления (Live / Concert / Performance)
    is_live_cand = detect_is_live(full_text)
    item.is_live = is_live_cand

    if is_live_cand and not is_live_requested:
        # Стандартный запрос: live-версия проигрывает студийному оригиналу
        score -= 5000.0
    elif is_live_cand and is_live_requested:
        # Пользователь явно запросил live: даем бонус
        score += 600.0
    elif not is_live_cand and is_live_requested:
        # Пользователь запросил live, а найден студийный трек: штрафуем
        score -= 3000.0

    # 2. Нежелательные модификаторы (cover, karaoke, parody)
    for unw in UNWANTED_DEFAULT_MODIFIERS:
        if unw in full_text and unw not in requested_modifiers:
            score -= 4000.0

    # Acoustic проверка:
    is_acoustic_cand = any(a in full_text for a in ("acoustic", "акустика", "unplugged"))
    is_acoustic_req = any(a in requested_modifiers for a in ("acoustic", "акустика", "unplugged"))
    if is_acoustic_cand and not is_acoustic_req:
        score -= 3500.0
    elif is_acoustic_cand and is_acoustic_req:
        score += 600.0

    # 3. Общие модификаторы (remix, slowed, sped up и др.)
    cand_mods = extract_modifiers(full_text)
    item.modifiers = cand_mods
    if requested_modifiers:
        if is_candidate_matching_modifiers(requested_modifiers, cand_mods):
            score += 800.0
        else:
            score -= 5000.0
    else:
        # Если пользователь искал оригинал, а кандидат имеет чужие модификаторы:
        other_mods = cand_mods - {"live", "performance"}
        if other_mods:
            score -= 3500.0

    # 4. Приоритет официальных музыкальных источников
    if item.source == "ytmusic":
        score += 400.0
    if " - topic" in uploader_lower or uploader_lower.endswith("topic"):
        score += 300.0
    if any(k in title_lower for k in ["official audio", "official release", "original audio", "official music"]):
        score += 250.0
    if any(k in uploader_lower for k in ["vevo", "official"]):
        score += 150.0

    # 5. Семантическое соответствие названию
    if query_title:
        core_words = extract_core_title_words(query_title, artist=query_artist)
        if core_words:
            ratio = compute_title_match_ratio(item.title, core_words)
            if ratio >= 0.8:
                score += 300.0
            elif ratio >= 0.5:
                score += 100.0
            else:
                score -= 4000.0

    # 6. Валидация исполнителя
    if query_artist and not is_generic_artist_name(query_artist):
        effective_art = item.artist or full_text
        if validate_artist_match(query_artist, effective_art):
            score += 150.0
        else:
            score -= 3000.0

    # 7. Хронометраж и многоуровневая защита от подозрительной длительности
    if canonical_duration and canonical_duration > 35 and item.duration:
        diff = abs(item.duration - canonical_duration)
        is_tempo_req = bool(requested_modifiers & {
            "sped up", "spedup", "speed up", "speedup", "fast version",
            "slowed", "slow", "super slowed", "super slow", "ultra slowed", "nightcore"
        })

        if is_tempo_req:
            # При запросе изменения темпа длительность может отличаться
            score += 0.0
        elif is_live_requested:
            # При запросе живого выступления допускается умеренно более широкий хронометраж
            if diff <= 15:
                score += 100.0
            elif diff <= 45:
                score -= diff * 5.0
            else:
                score -= 300.0 + (diff * 10.0)
        else:
            # Обычный студийный поиск: многоуровневая градуированная защита
            if diff <= 2:
                # Точное совпадение хронометража (208, 209 -> PASS)
                score += 200.0
            elif diff <= 4:
                # Небольшая допустимая разница (212 -> PASS / допустимо)
                score += 100.0
            elif diff <= 8:
                # Заметный штраф (215 (diff=7) -> заметный penalty)
                score -= 350.0 + (diff - 4) * 40.0
            elif diff <= 15:
                # Сильный штраф (diff 9..15s)
                score -= 1000.0 + (diff - 8) * 50.0
            elif diff <= 30:
                # Очень сильный штраф (180 (diff=28) -> сильный penalty)
                score -= 2500.0 + (diff - 15) * 60.0
            else:
                # Полный отсев подозрительных записей (125 (diff=83) -> REJECT)
                score -= 5000.0 + (diff - 30) * 80.0
    else:
        if 90 <= item.duration <= 360:
            score += 50.0
        elif 0 < item.duration < 45:
            score -= 2500.0  # Слишком короткое превью
        elif item.duration > 700:
            score -= 2000.0  # Слишком длинный микс/подкаст

    item.score = score
    return score


# ============================================================================
# 4. EXTRACTION & SEARCH ENGINE
# ============================================================================

def _extract_source_items(src: str, query: str, limit: int) -> List[SearchItem]:
    """Синхронно получает метаданные кандидатов из указанного источника (ytm, yt, sc)."""
    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 6,
    }
    cookies_info = get_cookies_info()
    if cookies_info.get("active") and src in ("yt", "ytm"):
        opts["cookiefile"] = cookies_info["path"]
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "mweb", "ios"]}}

    if src in ("yt", "ytm"):
        yt_proxy = get_current_youtube_proxy()
        if yt_proxy:
            opts["proxy"] = yt_proxy

    if src == "ytm":
        register_ytmsearch_extractor()

    items: List[SearchItem] = []
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
                if not u.startswith("http") and src in ("yt", "ytm"):
                    u = f"https://www.youtube.com/watch?v={u}"

                raw_title = e.get("title") or ""
                title = unicodedata.normalize("NFC", raw_title).strip()
                title = title.replace("’", "'").replace("‘", "'").replace("`", "'")

                dur = int(e.get("duration") or 0)
                q_lower = query.lower()
                if dur > 900 and not any(k in q_lower for k in ["mix", "микс", "album", "альбом", "1 hour", "час"]):
                    continue

                source_name = "ytmusic" if src == "ytm" else ("youtube" if src == "yt" else "soundcloud")
                artist_val = e.get("artist")
                items.append(SearchItem(
                    index=0,
                    title=title,
                    artist=artist_val,
                    uploader=e.get("uploader"),
                    channel=e.get("channel"),
                    duration=dur,
                    url=u,
                    source=source_name,
                    thumbnail=e.get("thumbnail"),
                    album=e.get("album")
                ))
    except Exception as ex:
        logger.warning("Ошибка поиска по %s для '%s': %s", src, query, ex)

    return items


def search_inline_tracks_sync(query: str, limit: int = 5) -> Tuple[List[SearchItem], Optional[str]]:
    """
    Специализированный поисковый пайплайн для Inline Mode:
    1. Основной поиск: YouTube Music (ytmsearch) — возвращает официальные студийные треки.
    2. Вычисление эталонного студийного хронометража.
    3. Скоринг кандидатов: предпочтение студии, защита от подозрительного хронометража.
    4. Fallback поиск: обычный YouTube (ytsearch) и SoundCloud (scsearch) вызывается,
       если YT Music вернул 0 результатов или после скоринга не набралось достаточно подходящих кандидатов.
    5. Защита Authoritative Metadata: uploader Release никогда не становится исполнителем.
    """
    q_norm = clean_unicode_text(query).strip()
    if not q_norm:
        return [], None

    # 1. Извлечение артиста, названия и модификаторов из запроса
    q_artist, q_title = extract_artist_title_from_query(q_norm)
    clean_q, req_mods_list = extract_track_modifiers(q_norm)
    requested_modifiers = set(req_mods_list) if req_mods_list else set()

    canonical_thumb = None

    search_term = f"{q_artist} - {q_title}" if (q_artist and q_title) else clean_q
    ytm_limit = max(10, limit * 2)

    # 2. ПЕРВИЧНЫЙ ПОИСК: YouTube Music (ytmsearch)
    ytm_candidates: List[SearchItem] = []
    has_sub_query = bool(q_title and q_title.lower() != search_term.lower() and len(q_title) >= 3)

    if has_sub_query:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f_main = executor.submit(_extract_source_items, "ytm", search_term, ytm_limit)
            f_sub = executor.submit(_extract_source_items, "ytm", q_title, 8)
            done, _ = concurrent.futures.wait([f_main, f_sub], timeout=7.0)
            res_main = f_main.result() if f_main in done else []
            res_sub = f_sub.result() if f_sub in done else []
            ytm_candidates = res_main + res_sub
    else:
        ytm_candidates = _extract_source_items("ytm", search_term, ytm_limit)

    # 3. Вычисление эталонного студийного хронометража
    canonical_duration = _find_studio_reference_duration(ytm_candidates, q_artist, q_title)

    for it in ytm_candidates:
        _score_inline_candidate(it, q_artist, q_title, requested_modifiers, canonical_duration)

    # Отбираем кандидатов с высоким положительным баллом (показательно подходящие)
    suitable_ytm = [c for c in ytm_candidates if c.score >= 500.0]

    # 4. FALLBACK ПОИСК: Обычный YouTube (ytsearch) + SoundCloud (scsearch)
    # Вызывается, если YTM вернул 0 результатов ИЛИ после скоринга не набралось достаточно подходящих кандидатов
    combined_candidates: List[SearchItem] = list(suitable_ytm)
    if len(suitable_ytm) < min(limit, 2):
        yt_fb_limit = max(8, limit * 2)
        sc_fb_limit = 5
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f_yt = executor.submit(_extract_source_items, "yt", search_term, yt_fb_limit)
            f_sc = executor.submit(_extract_source_items, "sc", search_term, sc_fb_limit)
            done, _ = concurrent.futures.wait([f_yt, f_sc], timeout=7.0)
            fb_yt = f_yt.result() if f_yt in done else []
            fb_sc = f_sc.result() if f_sc in done else []

        fallback_items = fb_yt + fb_sc
        if canonical_duration is None:
            canonical_duration = _find_studio_reference_duration(fallback_items, q_artist, q_title)

        for it in fallback_items:
            _score_inline_candidate(it, q_artist, q_title, requested_modifiers, canonical_duration)
            # Принимаем только кандидатов без критических штрафов (исключая подозрительный хронометраж и лайвы)
            if it.score >= 500.0:
                combined_candidates.append(it)

    # Если совсем ничего не нашлось, пробуем взять кандидатов с приемлемым скором
    if not combined_candidates:
        combined_candidates = [c for c in ytm_candidates if c.score > -2000.0]

    # 5. Дедупликация и сортировка кандидатов
    seen_sigs = set()
    ranked: List[SearchItem] = []

    def _sig(title: str, dur: int, url: str) -> str:
        clean = "".join(c for c in title.lower() if c.isalnum())
        return f"{clean}_{dur // 4}_{url}"

    # Сортируем: наибольший скор на 1 месте!
    sorted_items = sorted(combined_candidates, key=lambda c: c.score, reverse=True)

    for it in sorted_items:
        sig = _sig(it.title, it.duration, it.url)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        # 6. Защита Authoritative Metadata: определение канонических метаданных
        art, tit, alb, dur, th = resolve_canonical_candidate_metadata(
            it,
            query_artist=q_artist,
            query_title=q_title
        )

        it.clean_artist = art
        it.clean_title = tit
        if alb and not it.album:
            it.album = alb
        if canonical_thumb and (not it.thumbnail or "ytimg.com" in it.thumbnail):
            it.thumbnail = canonical_thumb

        ranked.append(it)

    for i, it in enumerate(ranked):
        it.index = i + 1

    return ranked[:limit], None


def search_tracks_sync(query: str, limit: int = 30) -> Tuple[List[SearchItem], Optional[str]]:
    """Синхронная точка входа для поиска треков."""
    return search_inline_tracks_sync(query, limit)


async def search_tracks_async(query: str, limit: int = 30) -> Tuple[List[SearchItem], Optional[str]]:
    """Асинхронная обертка над поиском треков (используется handlers/inline.py)."""
    return await asyncio.to_thread(search_inline_tracks_sync, query, limit)


def render_search_page(session_id: str, query: str, items: List[SearchItem], page: int = 0, page_size: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    """Формирует текст сообщения со списком треков и инлайн-клавиатуру."""
    total_items = len(items)
    total_pages = (total_items + page_size - 1) // page_size if total_items > 0 else 1
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total_items)
    page_items = items[start_idx:end_idx]

    lines = [f"<b>{html.escape(query)}</b>\n"]
    for i, item in enumerate(page_items, start=start_idx + 1):
        dur_str = f" <b>{item.formatted_duration}</b>" if item.formatted_duration else ""
        escaped_title = html.escape(item.title)
        lines.append(f"<b>{i}.</b> <i>{escaped_title}</i>{dur_str}")

    text = "\n".join(lines)

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

    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"mspg:{session_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"mspg:{session_id}:{page + 1}"))

    if nav_row:
        keyboard_rows.append(nav_row)

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
