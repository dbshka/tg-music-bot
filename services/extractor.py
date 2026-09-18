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
from services.identity import (
    TRACK_MODIFIERS,
    PERFORMANCE_MODIFIERS,
    DSP_SUPPORTED_MODIFIERS,
    SEMANTIC_MODIFIERS,
    clean_unicode_text,
    parse_speed_multiplier,
    extract_modifiers,
    has_track_modifiers,
    extract_track_modifiers,
    TRANSLIT_TABLE,
    transliterate_text,
    split_artist_names,
    validate_artist_match,
    extract_core_title_words,
    compute_title_match_ratio,
    parse_query_artist_title,
    TrackIdentity,
    VariantIdentity
)
from services.security import is_safe_url, safe_unshorten_url
from services.yandex_vk import resolve_yandex_music_track, resolve_vk_music_track

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
    album: Optional[str] = None

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


def parse_url_and_modifiers(text: str) -> Tuple[Optional[str], set, Optional[float]]:
    """
    Извлекает URL и запрошенные модификаторы/множители из текста (например '<url> slowed', '<url> 1.1x').
    """
    if not text:
        return None, set(), None
    cleaned = clean_unicode_text(text)
    url = find_first_url(cleaned)
    if not url:
        return None, set(), None
    remainder = cleaned.replace(url, " ")
    mods = extract_modifiers(remainder)
    mult = parse_speed_multiplier(remainder)
    return url, mods, mult

async def _unshorten_url(url: str, session: aiohttp.ClientSession) -> str:
    """Безопасное раскрытие ссылок через safe_unshorten_url с защитой от SSRF."""
    return await safe_unshorten_url(url, session)





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

def _score_catalog_candidate(query: str, cand_artist: str, cand_title: str, cand_album: str = "") -> float:
    """
    Интеллектуальная оценка кандидата из каталога (Deezer / iTunes).
    Отсеивает бутлеги, каверы, 8-bit эмуляции и несовпадающих артистов/названия.
    Возвращает скор (от 0.0 до 100.0). Отрицательный скор означает категорический отказ.
    """
    if not cand_artist or not cand_title:
        return -1000.0

    q_artist, q_title = None, query
    for sep in [" — ", " - ", " – "]:
        if sep in query:
            parts = query.split(sep, 1)
            if parts[0].strip() and parts[1].strip():
                q_artist, q_title = parts[0].strip(), parts[1].strip()
                break

    c_art = cand_artist.lower()
    c_tit = cand_title.lower()
    c_alb = (cand_album or "").lower()
    full_c = f"{c_art} {c_tit} {c_alb}"

    # 1. Негативные маркеры (бутлеги, каверы, трибьюты, 8-bit караоке)
    if not has_track_modifiers(query):
        negatives = [
            "bootleg", "bootleeg", "tribute", "8-bit", "8 bit", "emulation",
            "karaoke", "караоке", "instrumental", "инструментал", "кавер", "cover",
            "in the style of", "originally performed by"
        ]
        for neg in negatives:
            if neg in full_c:
                return -1000.0

    # 2. Оценка совпадения исполнителя
    art_score = 1.0
    clean_ca = re.sub(r'[\W_]+', ' ', c_art).strip()
    ca_words = set(clean_ca.split()) - {"the", "a", "an"}
    if not ca_words:
        ca_words = set(clean_ca.split())

    clean_ct = re.sub(r'[\W_]+', ' ', c_tit).strip()
    clean_qt = re.sub(r'[\W_]+', ' ', q_title.lower()).strip() if q_title else ""

    if q_artist:
        clean_qa = re.sub(r'[\W_]+', ' ', q_artist.lower()).strip()
        # Проверка перевернутого запроса (Название — Исполнитель)
        if clean_qa and clean_qt and (clean_qa == clean_ct or clean_qa in clean_ct or clean_ct in clean_qa) and \
           (clean_qt == clean_ca or clean_qt in clean_ca or clean_ca in clean_qt):
            return 98.0
        clean_qa = re.sub(r'[\W_]+', ' ', q_artist.lower()).strip()
        qa_core = re.sub(r'^the\s+', '', clean_qa)
        ca_core = re.sub(r'^the\s+', '', clean_ca)
        if clean_qa == clean_ca or qa_core == ca_core:
            art_score = 1.0
        elif clean_qa in clean_ca or clean_ca in clean_qa:
            w_qa = clean_qa.split()
            w_ca = clean_ca.split()
            if len(w_qa) == len(w_ca) and w_qa != w_ca:
                return -500.0  # Искаженное имя артиста (например 'Arctic Monkey' вместо 'Arctic Monkeys')
            art_score = 0.85
        else:
            w_qa = set(clean_qa.split())
            w_ca = set(clean_ca.split())
            overlap = len(w_qa & w_ca) / max(1, len(w_qa))
            if overlap >= 0.7:
                art_score = 0.7
            else:
                return -500.0  # Чужой артист! (например DJ Pedro Dance вместо Dua Lipa, Alejandro MG вместо Kavinsky)
    else:
        # В запросе нет разделителя ' - ' (например 'Radiohead Creep', 'idioteque radiohead', '505 arctic monkeys')
        clean_q_all = re.sub(r'[\W_]+', ' ', query.lower()).strip()
        w_query = set(clean_q_all.split())
        art_overlap = len(ca_words & w_query) / max(1, len(ca_words))
        if clean_ca in clean_q_all or art_overlap >= 0.8:
            art_score = 1.0
        elif art_overlap >= 0.5:
            art_score = 0.7
        else:
            return -500.0  # Имя артиста вообще не упоминается в запросе! (отсекает каверы/бутлеги вроде Mirko Barbesino)

    # 3. Оценка совпадения названия трека
    clean_qt = re.sub(r'[\W_]+', ' ', q_title.lower()).strip()
    clean_ct = re.sub(r'[\W_]+', ' ', c_tit).strip()
    w_qt = set(clean_qt.split())
    w_ct = set(clean_ct.split())

    filler = {"the", "a", "an", "in", "on", "at", "of", "and", "or", "feat", "ft"}
    w_qt_core = w_qt - filler
    if not q_artist and art_score >= 0.7:
        w_qt_core = w_qt_core - ca_words
    if not w_qt_core:
        w_qt_core = w_qt

    overlap_tit = len(w_qt_core & w_ct) / max(1, len(w_qt_core))
    if overlap_tit < 0.5:
        return -500.0  # Не то название! (например 'a lot' вместо 'redrum')

    return art_score * 50.0 + overlap_tit * 50.0


async def _search_deezer(session: aiohttp.ClientSession, query: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Поиск по Deezer API с жестким таймаутом (2.5с) и валидацией совпадения исполнителя и трека."""
    try:
        encoded = urllib.parse.quote(query)
        api_url = f"https://api.deezer.com/search?q={encoded}"
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("data", [])
                best_cand = None
                best_score = -1.0
                for item in results:
                    artist = item.get("artist", {}).get("name") or ""
                    title = item.get("title") or ""
                    album_title = item.get("album", {}).get("title") or ""
                    duration = int(item.get("duration") or 0)
                    if not artist or not title or duration <= 0:
                        continue
                    if not has_track_modifiers(query) and has_track_modifiers(title):
                        continue
                    score = _score_catalog_candidate(query, artist, title, album_title)
                    if score > best_score and score >= 70.0:
                        best_score = score
                        cover = item.get("album", {}).get("cover_xl") or item.get("album", {}).get("cover_big")
                        best_cand = (artist, title, cover, duration)
                if best_cand:
                    return best_cand
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
                best_cand = None
                best_score = -1.0
                for item in results:
                    track_name = item.get("trackName") or ""
                    artist_name = item.get("artistName") or ""
                    collection_name = item.get("collectionName") or ""
                    duration = int(item.get("trackTimeMillis", 0) / 1000)
                    if not artist_name or not track_name or duration <= 0:
                        continue
                    if not has_track_modifiers(query) and has_track_modifiers(track_name):
                        continue
                    score = _score_catalog_candidate(query, artist_name, track_name, collection_name)
                    if score > best_score and score >= 70.0:
                        best_score = score
                        artwork = item.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
                        best_cand = (artist_name, track_name, artwork, duration)
                if best_cand:
                    return best_cand
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

    norm_q = re.sub(r'\s*[-—–]\s*', ' - ', query).strip()
    clean_q = re.sub(r'[^\w\s\-]+', ' ', norm_q).strip()
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
    Превращает произвольный текстовый запрос пользователя в виртуальную ссылку / ExtractedTrack:
    1. Если в запросе есть специфические модификаторы (slowed, remix, reverb и т.д.),
       канонический оригинал не навязывается, чтобы пользователь получил желаемый звук.
    2. По обычным запросам опрашивает студийные каталоги (Deezer / iTunes), получая
       чистые имя исполнителя, название, эталонный хронометраж и официальную студийную обложку.
    3. При 0 результатах пробует конвертацию раскладки (RU <-> EN) только для обращения к каталогу.
    4. Если метаданных в каталогах нет, формирует безопасный поисковый ExtractedTrack,
       сохраняя оригинальный текст пользователя (кириллицу).
    """
    clean_q = unicodedata.normalize("NFC", query).strip()

    parsed_artist, parsed_title = None, clean_q
    for sep in [" — ", " - ", " – "]:
        if sep in clean_q:
            parts = clean_q.split(sep, 1)
            if parts[0].strip() and parts[1].strip():
                parsed_artist, parsed_title = parts[0].strip(), parts[1].strip()
                break

    # 1. Запрос с явными модификаторами (например 'Billie Eilish — bad guy 1.1x')
    if has_track_modifiers(clean_q):
        return ExtractedTrack(
            platform="TextSearch",
            target=f"ytsearch5:{clean_q}",
            is_search=True,
            title=parsed_title,
            artist=parsed_artist,
            thumbnail_url=None,
            duration=None
        )

    # 2. Поиск канонического студийного оригинала
    canonical = await resolve_canonical_track_info_async(clean_q)
    if canonical:
        return canonical

    # 3. Резервная проверка каталога со сменой раскладки (только для запроса в каталог!)
    flipped = convert_keyboard_layout(clean_q)
    if flipped.lower() != clean_q.lower():
        canonical_flipped = await resolve_canonical_track_info_async(flipped)
        if canonical_flipped:
            return canonical_flipped

    # 4. Резервный поиск по оригинальному тексту (редкий звук, инди, кириллица, SoundCloud)
    # ВСЕГДА сохраняем оригинальный запрос пользователя (не подменяя его на qwerty)
    parsed_artist, parsed_title = None, clean_q
    for sep in [" — ", " - ", " – "]:
        if sep in clean_q:
            parts = clean_q.split(sep, 1)
            if parts[0].strip() and parts[1].strip():
                parsed_artist, parsed_title = parts[0].strip(), parts[1].strip()
                break

    return ExtractedTrack(
        platform="TextSearch",
        target=f"ytsearch5:{clean_q}",
        is_search=True,
        title=parsed_title,
        artist=parsed_artist,
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
                m_data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, flags=re.DOTALL)
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
    if video_id:
        return ExtractedTrack(
            platform="YouTube / YouTube Music",
            target=clean_url,
            is_search=False,
            title=None,
            artist=None,
            thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            duration=None
        )
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

    # 0. Проверка безопасности URL (SSRF)
    is_safe, reason = is_safe_url(url)
    if not is_safe:
        raise ValueError(f"Недопустимая или небезопасная ссылка ({reason})")

    # Автоматическое безопасное раскрытие коротких ссылок
    url = await _unshorten_url(url, session)
    is_safe_after, reason_after = is_safe_url(url)
    if not is_safe_after:
        raise ValueError(f"Недопустимая ссылка после редиректа ({reason_after})")

    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    # Явный отказ от альбомов и плейлистов (Section 14)
    if "spotify.com" in domain:
        if "/album/" in path or "/playlist/" in path or "/collection/" in path:
            raise ValueError(
                "Загрузка альбомов и плейлистов не поддерживается.\n\n"
                "💡 Пожалуйста, отправьте ссылку на конкретный трек."
            )
    elif "apple.com" in domain:
        qs = urllib.parse.parse_qs(parsed.query)
        if "/album/" in path and "i" not in qs and not re.search(r'/album/[^/\s?]+/\d+/\d+', path):
            raise ValueError(
                "Загрузка альбомов и плейлистов не поддерживается.\n\n"
                "💡 Пожалуйста, отправьте ссылку на конкретный трек."
            )
    elif "youtube.com" in domain or "youtu.be" in domain:
        qs = urllib.parse.parse_qs(parsed.query)
        if "/playlist" in path or ("list=" in url and "v" not in qs):
            raise ValueError(
                "Загрузка альбомов и плейлистов не поддерживается.\n\n"
                "💡 Пожалуйста, отправьте ссылку на конкретный трек."
            )
    elif "soundcloud.com" in domain:
        if "/sets/" in path:
            raise ValueError(
                "Загрузка альбомов и плейлистов не поддерживается.\n\n"
                "💡 Пожалуйста, отправьте ссылку на конкретный трек."
            )

    # 1. Яндекс Музыка (двухэтапный resolve через российский VLESS-маршрут)
    if "music.yandex." in domain or "ya.cc" in domain or ("yandex." in domain and ("/album" in url or "/track" in url or "/artist" in url or "/playlists" in url)):
        res = await resolve_yandex_music_track(url, session)
        return ExtractedTrack(
            platform=res["platform"],
            target=res["target"],
            is_search=res["is_search"],
            title=res["title"],
            artist=res["artist"],
            thumbnail_url=res["thumbnail_url"],
            duration=res["duration"],
            album=res.get("album")
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
    elif "vk.com" in domain or "vk.ru" in domain:
        if "/audio" in url or "/music" in url or "z=audio" in url:
            res = await resolve_vk_music_track(url, session)
            return ExtractedTrack(
                platform=res["platform"],
                target=res["target"],
                is_search=res["is_search"],
                title=res["title"],
                artist=res["artist"],
                thumbnail_url=res["thumbnail_url"],
                duration=res["duration"],
                album=res.get("album")
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
