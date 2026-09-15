import asyncio
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple
import aiohttp

from config import CUSTOM_API_SERVER
from services.http_client import get_shared_session

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTrack:
    """Информация о распознанном треке."""
    platform: str
    target: str  # Прямая ссылка для yt-dlp либо поисковой запрос ytsearch
    is_search: bool
    title: Optional[str] = None
    artist: Optional[str] = None
    thumbnail_url: Optional[str] = None
    duration: Optional[int] = None

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        if self.title:
            return self.title
        return self.target


def find_first_url(text: str) -> Optional[str]:
    """
    Находит и очищает первую ссылку в тексте сообщения,
    устраняя случайное дублирование и лишний мусор трекинга.
    """
    # 1. Яндекс Музыка (перехват для информативного уведомления)
    ym_match = re.search(r'https?://(?:music\.yandex\.[a-z]+|ya\.cc)[^\s]*', text)
    if ym_match:
        return ym_match.group(0)

    # 2. Spotify
    sp_match = re.search(r'https?://open\.spotify\.com/(?:intl-[a-z]+/)?track/([a-zA-Z0-9]+)', text)
    if sp_match:
        track_id = sp_match.group(1)
        return f"https://open.spotify.com/track/{track_id}"

    # 3. Apple Music
    am_match = re.search(r'(https?://music\.apple\.com/[a-z]+/[^\s?]+(?:\?[^\s#]+)?)', text)
    if am_match:
        raw_am = am_match.group(1)
        # Отсекаем, если случайно склеилась вторая ссылка
        if "https://" in raw_am[8:]:
            raw_am = raw_am[:raw_am.index("https://", 8)]
        return raw_am

    # 4. YouTube
    yt_match = re.search(
        r'(https?://(?:(?:www\.|m\.)?youtube\.com/(?:watch\?v=[a-zA-Z0-9_-]+|shorts/[a-zA-Z0-9_-]+|embed/[a-zA-Z0-9_-]+)|youtu\.be/[a-zA-Z0-9_-]+|music\.youtube\.com/watch\?v=[a-zA-Z0-9_-]+))',
        text
    )
    if yt_match:
        return yt_match.group(1)

    # Общий regex для прочих ссылок
    match = re.search(r'https?://[^\s]+', text)
    if match:
        raw_url = match.group(0)
        # Отсекаем дублирование https://
        if "https://" in raw_url[8:]:
            raw_url = raw_url[:raw_url.index("https://", 8)]
        elif "http://" in raw_url[7:]:
            raw_url = raw_url[:raw_url.index("http://", 7)]
        # Отсекаем закрывающую круглую скобку, если ссылка была в скобках
        if raw_url.endswith(")") and "(" not in raw_url:
            raw_url = raw_url[:-1]
        return raw_url

    return None


async def _unshorten_url(url: str, session: aiohttp.ClientSession) -> str:
    """
    Раскрывает редиректы для коротких ссылок (ya.cc, clck.ru, vk.cc, t.co, bit.ly, spotify.link, band.link).
    """
    short_domains = ("ya.cc", "clck.ru", "vk.cc", "t.co", "goo.gl", "bit.ly", "spotify.link", "band.link", "tinyurl.com")
    parsed = urllib.parse.urlparse(url)
    if any(sd in parsed.netloc.lower() for sd in short_domains):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        try:
            async with session.head(url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                return str(resp.url)
        except Exception:
            try:
                async with session.get(url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    return str(resp.url)
            except Exception:
                pass
    return url





TRACK_MODIFIERS = {
    "super slowed down", "super slowed", "super slow", "ultra slowed",
    "slowed down", "slowed", "slow", "reverb", "reverbed", "slowed + reverb",
    "speed up", "speedup", "sped up", "spedup", "fast version", "sped up + reverb",
    "remix", "ремикс", "rmx", "bootleg", "flip", "mashup", "vip mix",
    "cover", "кавер", "acoustic", "акустика", "piano", "пианино",
    "acapella", "a cappella", "акапелла", "live", "лайв", "концерт",
    "8d", "16d", "nightcore", "daycore", "instrumental", "инструментал", "minus", "минус",
    "edit", "fan edit", "karaoke", "караоке", "orchestral", "orchestra", "tribute"
}

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


def has_track_modifiers(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(mod in t for mod in TRACK_MODIFIERS)


TRANSLIT_TABLE = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
}


def transliterate_text(text: str) -> str:
    """Универсальная транслитерация кириллицы в латиницу с NFC-нормализацией."""
    norm = unicodedata.normalize("NFC", text or "").lower()
    return "".join(TRANSLIT_TABLE.get(c, c) for c in norm)


def extract_core_title_words(title: Optional[str], artist: Optional[str] = None) -> set[str]:
    """Извлекает ключевые слова названия трека без модификаторов и имени артиста с NFC-нормализацией."""
    if not title:
        return set()
    raw = unicodedata.normalize("NFC", title).lower()
    raw = raw.replace("’", "'").replace("‘", "'").replace("`", "'")
    # 1. Удаляем feat/ft/prod конструкции в скобках
    raw = re.sub(r'[\(\[][^\)\]]*(?:feat|ft\.|prod|prod\.)[^\)\]]*[\)\]]', ' ', raw)
    # 2. Удаляем известные модификаторы трека
    for mod in sorted(TRACK_MODIFIERS, key=len, reverse=True):
        raw = re.sub(r'\b' + re.escape(mod) + r'\b', ' ', raw)
    # 3. Удаляем слова артиста, если они присутствуют в названии
    if artist:
        artist_norm = unicodedata.normalize("NFC", artist).lower().replace("’", "'").replace("‘", "'").replace("`", "'")
        artist_words = set(re.findall(r'[\w]+', artist_norm))
        for aw in artist_words:
            if len(aw) >= 2:
                raw = re.sub(r'\b' + re.escape(aw) + r'\b', ' ', raw)
    # 4. Извлекаем слова названия
    words = set(re.findall(r'[\w]+', raw))
    return {unicodedata.normalize("NFC", w) for w in words if len(w) >= 1}


def compute_title_match_ratio(cand_title: str, core_words: set[str]) -> float:
    """Вычисляет коэффициент покрытия ключевых слов названия в заголовке кандидата с NFC-нормализацией."""
    if not core_words:
        return 1.0
    cand_norm = unicodedata.normalize("NFC", cand_title or "").lower()
    cand_lower = cand_norm.replace("’", "'").replace("‘", "'").replace("`", "'")
    cand_translit = transliterate_text(cand_lower)
    cand_tokens = set(re.findall(r'[\w]+', cand_lower))
    cand_tokens_translit = set(re.findall(r'[\w]+', cand_translit))

    core_words_norm = {unicodedata.normalize("NFC", w).lower().replace("’", "'").replace("‘", "'").replace("`", "'") for w in core_words}

    matched = 0
    for w in core_words_norm:
        w_translit = transliterate_text(w)
        # 1. Точное совпадение токена
        if w in cand_tokens or w_translit in cand_tokens_translit or w_translit in cand_tokens:
            matched += 1
            continue
        # 2. Подстрока для слов от 3 символов
        if len(w) >= 3 and (w in cand_lower or w_translit in cand_translit):
            matched += 1
            continue
        # 3. Стемминг для длинных слов (от 4 символов)
        if len(w) >= 4:
            stem = w[:-1] if len(w) > 4 else w
            stem_tr = w_translit[:-1] if len(w_translit) > 4 else w_translit
            if any(stem in tok for tok in cand_tokens if len(tok) >= 4) or \
               any(stem_tr in tok for tok in cand_tokens_translit if len(tok) >= 4):
                matched += 1
                continue

    return matched / len(core_words_norm)


async def _search_deezer(session: aiohttp.ClientSession, query: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Поиск по Deezer API с жестким таймаутом (2.5с) и валидацией совпадения исполнителя и трека."""
    try:
        encoded = urllib.parse.quote(query)
        api_url = f"https://api.deezer.com/search?q={encoded}"
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("data", [])
                clean_q = re.sub(r'[\W_]+', ' ', query.lower()).strip()
                q_words = set(clean_q.split())
                for item in results:
                    artist = item.get("artist", {}).get("name") or ""
                    title = item.get("title") or ""
                    # Если пользователь не искал модификаторы, пропускаем каверы/акустику
                    if not has_track_modifiers(query) and has_track_modifiers(title):
                        continue
                    full_str = f"{artist} {title}".lower()
                    clean_full = re.sub(r'[\W_]+', ' ', full_str).strip()
                    item_words = set(clean_full.split())
                    match = (
                        (clean_q == clean_full)
                        or (q_words and q_words.issubset(item_words))
                        or (len(q_words & item_words) / max(1, len(q_words)) >= 0.6)
                    )
                    if match:
                        cover = item.get("album", {}).get("cover_xl") or item.get("album", {}).get("cover_big")
                        duration = int(item.get("duration") or 0) or None
                        return artist, title, cover, duration
    except Exception:
        pass
    return None, None, None, None


async def _search_itunes_track(session: aiohttp.ClientSession, query: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Резервный поиск по iTunes API с валидацией совпадения трека и исполнителя (быстрый ответ < 500 мс)."""
    try:
        encoded = urllib.parse.quote(query)
        api_url = f"https://itunes.apple.com/search?term={encoded}&media=music&entity=song&limit=5"
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("results", [])
                clean_q = re.sub(r'[\W_]+', ' ', query.lower()).strip()
                q_words = set(clean_q.split())
                for item in results:
                    track_name = item.get("trackName") or ""
                    artist_name = item.get("artistName") or ""
                    if not has_track_modifiers(query) and has_track_modifiers(track_name):
                        continue
                    full_str = f"{artist_name} {track_name}".lower()
                    clean_full = re.sub(r'[\W_]+', ' ', full_str).strip()
                    item_words = set(clean_full.split())
                    match = (
                        (clean_q == clean_full)
                        or (q_words and q_words.issubset(item_words))
                        or (len(q_words & item_words) / max(1, len(q_words)) >= 0.6)
                    )
                    if match:
                        artwork = item.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
                        duration = int(item.get("trackTimeMillis", 0) / 1000) or None
                        return artist_name, track_name, artwork, duration
    except Exception:
        pass
    return None, None, None, None


async def resolve_canonical_track_info_async(query: str) -> Optional[ExtractedTrack]:
    """
    Определяет канонические метаданные студийного оригинала трека через Deezer / iTunes API:
    - Официальное имя исполнителя
    - Официальное название трека (без лишнего мусора)
    - Эталонная длительность трека в секундах (duration)
    - Официальная студийная обложка альбома в высоком разрешении
    Если в запросе содержатся модификаторы (slowed, sped up, remix и др.), студийный эталон не навязывается.
    """
    if has_track_modifiers(query):
        return None

    clean_q = re.sub(r'[\W_]+', ' ', query).strip()
    if not clean_q or len(clean_q) < 2:
        return None

    session = get_shared_session()

    # 1. Приоритетный поиск в Deezer API (< 200 мс) - чистейший студийный каталог без каверов
    try:
        d_artist, d_title, d_cover, d_dur = await _search_deezer(session, clean_q)
        if d_artist and d_title and d_dur:
            return ExtractedTrack(
                platform="Canonical/Deezer",
                target=f"ytsearch5:{d_artist} - {d_title}",
                is_search=True,
                title=d_title,
                artist=d_artist,
                thumbnail_url=d_cover,
                duration=d_dur
            )
    except Exception as ex:
        logger.debug("Ошибка канонического поиска Deezer: %s", ex)

    # 2. Резервный поиск в iTunes Search API (< 250 мс, каталог Apple Music)
    try:
        it_artist, it_title, it_cover, it_dur = await _search_itunes_track(session, clean_q)
        if it_artist and it_title and it_dur:
            return ExtractedTrack(
                platform="Canonical/iTunes",
                target=f"ytsearch5:{it_artist} - {it_title}",
                is_search=True,
                title=it_title,
                artist=it_artist,
                thumbnail_url=it_cover,
                duration=it_dur
            )
    except Exception as ex:
        logger.debug("Ошибка канонического поиска iTunes: %s", ex)

    return None


async def resolve_text_to_track_info(query: str) -> ExtractedTrack:
    """
    Превращает произвольный текстовый запрос пользователя в виртуальную ссылку / ExtractedTrack,
    как если бы пользователь отправил ссылку из Spotify:
    1. Если в запросе есть специфические модификаторы (slowed, remix, reverb и т.д.),
       канонический оригинал не навязывается, чтобы пользователь получил желаемый звук.
    2. По обычным запросам опрашивает студийные каталоги (Deezer / iTunes), получая
       чистые имя исполнителя, название, эталонный хронометраж и официальную студийную обложку.
    3. При 0 результатах в самую последнюю очередь пробует конвертацию раскладки (RU <-> EN).
    4. Если метаданных в каталогах нет, формирует безопасный поисковый ExtractedTrack.
    """
    clean_q = unicodedata.normalize("NFC", query).strip()

    # 1. Запрос с явными модификаторами (например 'radiohead creep slowed')
    if has_track_modifiers(clean_q):
        return ExtractedTrack(
            platform="TextSearch",
            target=f"ytsearch5:{clean_q}",
            is_search=True,
            title=clean_q,
            artist=None,
            thumbnail_url=None,
            duration=None
        )

    # 2. Поиск канонического студийного оригинала
    canonical = await resolve_canonical_track_info_async(clean_q)
    if canonical:
        return canonical

    # -------------------------------------------------------------
    # 3. ТОЛЬКО В САМУЮ ПОСЛЕДНЮЮ ОЧЕРЕДЬ: пробуем смену раскладки (RU <-> EN)
    # -------------------------------------------------------------
    flipped = convert_keyboard_layout(clean_q)
    if flipped.lower() != clean_q.lower():
        canonical_flipped = await resolve_canonical_track_info_async(flipped)
        if canonical_flipped:
            return canonical_flipped
        # Если в каталогах нет, пробуем искать в YouTube по исправленной раскладке
        clean_q = flipped

    # 4. Резервный поиск по тексту (редкий звук, SoundCloud и др.)
    return ExtractedTrack(
        platform="TextSearch",
        target=f"ytsearch5:{clean_q}",
        is_search=True,
        title=clean_q,
        artist=None,
        thumbnail_url=None,
        duration=None
    )


async def _extract_microlink_metadata(url: str, session: aiohttp.ClientSession) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Извлекает оригинальные метаданные (название, автор, обложка) в обход геоблоков через глобальный прокси."""
    try:
        api_url = f"https://api.microlink.io/?url={urllib.parse.quote(url)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=3.5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("status") == "success":
                    item = data.get("data", {})
                    title = item.get("title")
                    artist = item.get("author")
                    image = item.get("image", {}).get("url")

                    if not artist and item.get("description"):
                        desc = item.get("description").strip()
                        parts = re.split(r'[\u00b7\u2022]', desc)
                        if parts and "Song" not in parts[0] and "Spotify" not in parts[0]:
                            artist = parts[0].strip()

                    if title:
                        return artist, title, image
    except Exception:
        pass
    return None, None, None


async def _extract_spotify_embed_metadata(track_id: str, session: aiohttp.ClientSession) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """
    Извлекает метаданные трека напрямую через открытый Spotify Embed API:
    https://open.spotify.com/embed/track/{track_id}
    При геоблоках (HTTP 451) или отсутствии данных автоматически обращается к Microlink.
    """
    try:
        url = f"https://open.spotify.com/embed/track/{track_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3.5)) as resp:
            if resp.status == 200:
                html = await resp.text()
                import json
                m_data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
                if m_data:
                    data = json.loads(m_data.group(1))
                    entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
                    if entity:
                        title = entity.get('title') or entity.get('name')
                        artists_list = [a.get('name') for a in entity.get('artists', []) if a.get('name')]
                        artist = ", ".join(artists_list) if artists_list else None
                        dur_ms = entity.get('duration') or 0
                        duration = int(dur_ms / 1000) if dur_ms > 0 else None
                        images = entity.get('visualIdentity', {}).get('image', [])
                        cover = images[0].get('url') if images else None
                        if title and artist:
                            return artist, title, cover, duration
    except Exception as e:
        logger.debug("Ошибка извлечения Spotify embed: %s", e)

    # Fallback на Microlink при геоблокировке Embed API (HTTP 451)
    try:
        spotify_track_url = f"https://open.spotify.com/track/{track_id}"
        m_artist, m_title, m_cover = await _extract_microlink_metadata(spotify_track_url, session)
        if m_title and m_artist:
            m_duration = None
            if not has_track_modifiers(m_title):
                _, _, _, m_duration = await _search_itunes_track(session, f"{m_artist} {m_title}")
            return m_artist, m_title, m_cover, m_duration
    except Exception as ex:
        logger.debug("Ошибка Microlink fallback для Spotify: %s", ex)

    return None, None, None, None


async def extract_spotify_info(url: str, session: aiohttp.ClientSession) -> Optional[ExtractedTrack]:
    """
    Извлекает метаданные трека Spotify.
    1. Опрашивает Spotify Embed API (с fallback на Microlink).
    2. При необходимости использует oEmbed и Microlink для точного артиста и названия.
    3. Применяет канонический резолвер только для немагических/оригинальных студийных релизов.
    """
    track_id_match = re.search(r'track/([a-zA-Z0-9]+)', url)
    track_id = track_id_match.group(1) if track_id_match else None
    clean_url = f"https://open.spotify.com/track/{track_id}" if track_id else url

    title = None
    artist = None
    thumbnail_url = None
    duration = None

    # 1. Приоритетное извлечение через Embed API (с поддержкой Microlink)
    if track_id:
        artist, title, thumbnail_url, duration = await _extract_spotify_embed_metadata(track_id, session)

    # 2. Быстрый опрос Microlink напрямую, если автор или название не найдены
    if not (title and artist):
        m_artist, m_title, m_cover = await _extract_microlink_metadata(clean_url, session)
        artist = artist or m_artist
        title = title or m_title
        thumbnail_url = thumbnail_url or m_cover

    # 3. Резервный опрос oEmbed
    if not (title and artist):
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(clean_url)}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    oe_raw = data.get("title")
                    thumbnail_url = thumbnail_url or data.get("thumbnail_url")
                    if oe_raw and not title:
                        title = oe_raw
        except Exception:
            pass

    # 4. Резервный поиск канонического эталона через Deezer / iTunes
    # ВНИМАНИЕ: только если трек НЕ является модификацией (slowed, sped up, remix и т.д.)
    if title and (not artist or not duration) and not has_track_modifiers(title):
        search_q = f"{artist} {title}" if artist else title
        canonical = await resolve_canonical_track_info_async(search_q)
        if canonical:
            artist = artist or canonical.artist
            title = title or canonical.title
            thumbnail_url = thumbnail_url or canonical.thumbnail_url
            duration = duration or canonical.duration

    if title:
        search_query = f"{artist} - {title}" if (artist and artist.lower() not in title.lower()) else title
        return ExtractedTrack(
            platform="Spotify",
            target=f"ytsearch5:{search_query}",
            is_search=True,
            title=title,
            artist=artist,
            thumbnail_url=thumbnail_url,
            duration=duration
        )

    return None



def extract_apple_music_id(url: str) -> Optional[str]:
    """Извлекает цифровой ID трека или альбома из любого формата ссылок Apple Music."""
    m_i = re.search(r"[?&]i=(\d+)", url)
    if m_i:
        return m_i.group(1)
    # Ссылки вида /song/name/12345, /album/name/12345, /song/12345, /album/12345
    m_path = re.search(r'/(?:id|song|album)(?:/[^/\s?]+)*/(\d+)', url)
    if m_path:
        return m_path.group(1)
    m_id = re.search(r'/id(\d+)', url)
    if m_id:
        return m_id.group(1)
    m_digits = re.search(r'/(\d+)(?:[?]|$)', url)
    if m_digits:
        return m_digits.group(1)
    return None


def _clean_apple_music_branding(s: Optional[str]) -> Optional[str]:
    """Удаляет брендовые приписки Apple Music (on Apple Music, в Apple Music, - Single и т.д.)."""
    if not s:
        return s
    s = s.replace('\xa0', ' ')
    s = re.sub(r'\s+(?:on|в|sur|en|auf|su)\s+Apple\s*Music.*$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*Apple\s*Music.*$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*-\s*(?:Single|Album|EP)\s*$', '', s, flags=re.IGNORECASE)
    return s.strip()


async def extract_apple_music_info(url: str, session: aiohttp.ClientSession) -> Optional[ExtractedTrack]:
    """Извлекает информацию о треке Apple Music через официальный lookup API по ID трека или OpenGraph."""
    track_id = extract_apple_music_id(url)

    if track_id:
        try:
            # Извлекаем региональный код из URL (например us, ru, de)
            country_match = re.search(r"music\.apple\.com/([a-z]{2})/", url)
            country = country_match.group(1) if country_match else "us"
            lookup_url = f"https://itunes.apple.com/lookup?id={track_id}&country={country}"
            async with session.get(lookup_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    results = data.get("results", [])
                    if results:
                        item = results[0]
                        artist = _clean_apple_music_branding(item.get("artistName"))
                        title = _clean_apple_music_branding(item.get("trackName") or item.get("collectionName"))
                        artwork = item.get("artworkUrl100")
                        if artwork:
                            artwork = artwork.replace("100x100bb", "600x600bb")

                        search_query = f"{artist} - {title}" if artist and title else (title or artist)
                        duration = int(item.get("trackTimeMillis", 0) / 1000) or None
                        return ExtractedTrack(
                            platform="Apple Music",
                            target=f"ytsearch5:{search_query}",
                            is_search=True,
                            title=title,
                            artist=artist,
                            thumbnail_url=artwork,
                            duration=duration
                        )
        except Exception:
            pass

    # Fallback: OpenGraph scraping
    try:
        clean_url = url.split("?")[0]
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(clean_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                html = await resp.text()
                og_title = re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html)
                og_img = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
                if og_title:
                    raw_title = _clean_apple_music_branding(og_title.group(1))
                    artist = None
                    title = raw_title
                    if " — песня исполнителя " in raw_title:
                        title, artist = raw_title.split(" — песня исполнителя ", 1)
                    elif " by " in raw_title:
                        title, artist = raw_title.split(" by ", 1)
                    elif " — " in raw_title:
                        artist, title = raw_title.split(" — ", 1)

                    artist = _clean_apple_music_branding(artist)
                    title = _clean_apple_music_branding(title)
                    thumb = og_img.group(1) if og_img else None
                    search_query = f"{artist} - {title}" if artist else title

                    duration = None
                    canonical = await resolve_canonical_track_info_async(search_query)
                    if canonical:
                        duration = canonical.duration
                        thumb = thumb or canonical.thumbnail_url

                    return ExtractedTrack(
                        platform="Apple Music",
                        target=f"ytsearch5:{search_query}",
                        is_search=True,
                        title=title,
                        artist=artist,
                        thumbnail_url=thumb,
                        duration=duration
                    )
    except Exception:
        pass

async def extract_youtube_info(url: str, session: aiohttp.ClientSession) -> Optional[ExtractedTrack]:
    """
    Извлекает название, автора и обложку трека из YouTube через публичный oEmbed API.
    Работает со 100% надежностью без cookies и без блокировок, гарантируя метаданные для Fallback.
    """
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    video_id = qs.get("v", [""])[0]
    if not video_id and ("youtu.be" in parsed.netloc or "/shorts/" in parsed.path):
        video_id = parsed.path.strip("/").split("/")[-1]

    clean_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(clean_url)}&format=json"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw_title = data.get("title") or ""
                author = data.get("author_name") or ""
                thumb = data.get("thumbnail_url")

                # Очистка названия канала от суффиксов YouTube (- Topic / - Тема)
                author = re.sub(r'\s*-\s*(?:Topic|Тема)\b', '', author, flags=re.IGNORECASE).strip()

                title = raw_title
                artist = author
                if " - " in raw_title:
                    parts = raw_title.split(" - ", 1)
                    artist = parts[0].strip()
                    title = parts[1].strip()
                elif " — " in raw_title:
                    parts = raw_title.split(" — ", 1)
                    artist = parts[0].strip()
                    title = parts[1].strip()

                # Очищаем видео-приписки клипов, но СОХРАНЯЕМ Remix / Sped Up / Slowed
                title = re.sub(
                    r'\s*[\(\[](?:Official\s*(?:Music\s*)?Video|Official\s*Audio|Lyric\s*Video|Video|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
                    '',
                    title,
                    flags=re.IGNORECASE
                ).strip()

                duration = None
                # Сверяем канонические метаданные студийного релиза только если трек не модифицирован
                search_seed = f"{artist} - {title}" if (artist and artist != title) else (title or artist)
                if search_seed and not has_track_modifiers(title):
                    canonical = await resolve_canonical_track_info_async(search_seed)
                    if canonical:
                        duration = canonical.duration
                        thumb = thumb or canonical.thumbnail_url
                        if canonical.artist and (not artist or artist == title):
                            artist = canonical.artist
                        if canonical.title:
                            title = canonical.title

                return ExtractedTrack(
                    platform="YouTube / YouTube Music",
                    target=clean_url,
                    is_search=False,
                    title=title or raw_title,
                    artist=artist or author,
                    thumbnail_url=thumb,
                    duration=duration
                )
    except Exception:
        pass
    return None


async def extract_soundcloud_info(url: str, session: aiohttp.ClientSession) -> Optional[ExtractedTrack]:
    """
    Извлекает оригинальные метаданные трека SoundCloud через официальный oEmbed API.
    Очищает UTM-метки трекинга и парсит точное название и автора трека.
    """
    try:
        clean_url = url.split("?")[0].rstrip("/")
        oembed_url = f"https://soundcloud.com/oembed?url={urllib.parse.quote(clean_url)}&format=json"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw_title = data.get("title") or ""
                author = data.get("author_name") or ""
                thumb = data.get("thumbnail_url")

                title = raw_title
                artist = author
                if " by " in raw_title:
                    parts = raw_title.rsplit(" by ", 1)
                    title = parts[0].strip()
                    if not artist:
                        artist = parts[1].strip()

                return ExtractedTrack(
                    platform="SoundCloud",
                    target=clean_url,
                    is_search=False,
                    title=title,
                    artist=artist,
                    thumbnail_url=thumb
                )
    except Exception:
        pass
    return None


async def resolve_track_url(url: str, session: Optional[aiohttp.ClientSession] = None) -> ExtractedTrack:
    """
    Анализирует переданный URL и определяет способ загрузки:
    - Яндекс Музыка -> уведомление об отключении прямых ссылок хостинга
    - Spotify -> OpenGraph / oEmbed / iTunes -> ytsearch
    - Apple Music -> iTunes lookup -> ytsearch
    - YouTube -> oEmbed метаданные + прямая загрузка
    - SoundCloud -> oEmbed метаданные + прямая загрузка (с автоматическим Fallback)
    - VK, Bandcamp и др. -> прямая загрузка через yt-dlp
    """
    if session is None:
        session = get_shared_session()

    # 0. Автоматическое раскрытие коротких ссылок (ya.cc, clck.ru, spotify.link, band.link и др.)
    url = await _unshorten_url(url, session)

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()

    # 1. Яндекс Музыка (прямые ссылки отключены из-за геоблока хостинга)
    if "music.yandex." in domain or "ya.cc" in domain or ("yandex." in domain and ("/album" in url or "/track" in url or "/artist" in url or "/playlists" in url)):
        raise ValueError(
            "Загрузка по прямым ссылкам Яндекс Музыки отключена из-за региональных ограничений хостинга.\n\n"
            "💡 Пожалуйста, отправьте название трека или исполнителя текстом (например: Gazan — 67). "
            "Бот моментально найдёт и пришлёт MP3!"
        )

    # 2. Spotify
    if "spotify.com" in domain:
        track = await extract_spotify_info(url, session)
        if track:
            return track

    # 3. Apple Music
    if "apple.com" in domain:
        track = await extract_apple_music_info(url, session)
        if track:
            return track

    # 4. YouTube / YouTube Music (извлекаем точные метаданные для мгновенного SoundCloud Fallback)
    if "youtube.com" in domain or "youtu.be" in domain:
        yt_track = await extract_youtube_info(url, session)
        if yt_track:
            return yt_track

    # 5. SoundCloud (извлекаем точные метаданные через oEmbed и очищаем UTM-метки)
    if "soundcloud.com" in domain:
        sc_track = await extract_soundcloud_info(url, session)
        if sc_track:
            return sc_track

    # Прочие сервисы, поддерживаемые yt-dlp напрямую
    platform_name = "Музыкальный сервис"
    if "youtube.com" in domain or "youtu.be" in domain:
        platform_name = "YouTube / YouTube Music"
    elif "soundcloud.com" in domain:
        platform_name = "SoundCloud"
    elif "vk.com" in domain:
        if "/audio" in url or "/music" in url or "z=audio" in url:
            raise ValueError(
                "Загрузка по прямым ссылкам ВК Музыки не поддерживается (ВКонтакте закрыл доступ к аудио для внешних серверов без авторизации).\n\n"
                "💡 Пожалуйста, отправьте название трека или исполнителя текстом (например: MiyaGi — Captain). "
                "Бот моментально найдёт и пришлёт MP3!"
            )
        platform_name = "VK Видео"
    elif "bandcamp.com" in domain:
        platform_name = "Bandcamp"
    elif "tiktok.com" in domain:
        platform_name = "TikTok"

    return ExtractedTrack(
        platform=platform_name,
        target=url,
        is_search=False
    )

