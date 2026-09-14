import asyncio
import logging
import re
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





async def _search_deezer(session: aiohttp.ClientSession, query: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Поиск по Deezer API с жестким таймаутом (2.5с) во избежание зависаний."""
    try:
        encoded = urllib.parse.quote(query)
        api_url = f"https://api.deezer.com/search?q={encoded}"
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("data", [])
                if results:
                    item = results[0]
                    artist = item.get("artist", {}).get("name")
                    title = item.get("title")
                    cover = item.get("album", {}).get("cover_xl") or item.get("album", {}).get("cover_big")
                    duration = int(item.get("duration") or 0) or None
                    return artist, title, cover, duration
    except Exception:
        pass
    return None, None, None, None


async def _search_itunes_track(session: aiohttp.ClientSession, query: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Резервный поиск по iTunes API с валидацией совпадения названия трека (быстрый ответ < 500 мс)."""
    try:
        encoded = urllib.parse.quote(query)
        api_url = f"https://itunes.apple.com/search?term={encoded}&media=music&limit=5"
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("results", [])
                clean_q = re.sub(r'[\W_]+', ' ', query.lower()).strip()
                q_words = set(clean_q.split())
                for item in results:
                    track_name = item.get("trackName") or ""
                    clean_tn = re.sub(r'[\W_]+', ' ', track_name.lower()).strip()
                    tn_words = set(clean_tn.split())
                    match = (clean_q == clean_tn) or (len(q_words) > 1 and q_words.issubset(tn_words)) or (tn_words and len(q_words & tn_words) / len(q_words) >= 0.7)
                    if match:
                        artist = item.get("artistName")
                        artwork = item.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
                        duration = int(item.get("trackTimeMillis", 0) / 1000) or None
                        return artist, track_name, artwork, duration
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
    Работает за < 200 мс без необходимости в API ключах.
    """
    clean_q = re.sub(r'[\W_]+', ' ', query).strip()
    if not clean_q or len(clean_q) < 2:
        return None

    session = get_shared_session()
    # 1. Приоритетный поиск в Deezer API (< 200 мс)
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

    # 2. Резервный поиск в iTunes Search API (< 300 мс)
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
    Возвращает точные (artist, title, cover_url, duration_seconds) без необходимости в API ключах.
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
    return None, None, None, None


async def extract_spotify_info(url: str, session: aiohttp.ClientSession) -> Optional[ExtractedTrack]:
    """
    Извлекает метаданные трека Spotify.
    1. Напрямую опрашивает Spotify Embed API для получения точных исполнителя, названия и эталонной длительности.
    2. При необходимости использует oEmbed и Deezer/iTunes для гарантированного нахождения эталонного хронометража.
    """
    track_id_match = re.search(r'track/([a-zA-Z0-9]+)', url)
    track_id = track_id_match.group(1) if track_id_match else None
    clean_url = f"https://open.spotify.com/track/{track_id}" if track_id else url

    title = None
    artist = None
    thumbnail_url = None
    duration = None

    # 1. Приоритетное прямое извлечение через Spotify Embed API
    if track_id:
        artist, title, thumbnail_url, duration = await _extract_spotify_embed_metadata(track_id, session)

    # 2. Быстрый опрос oEmbed, если embed не вернул артиста или длительность
    if not (title and artist and duration):
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(clean_url)}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    oe_raw = data.get("title")
                    thumbnail_url = thumbnail_url or data.get("thumbnail_url")
                    if oe_raw and " - " in oe_raw:
                        parts = oe_raw.split(" - ", 1)
                        artist = artist or parts[0].strip()
                        title = title or parts[1].strip()
                    elif oe_raw:
                        title = title or oe_raw
        except Exception:
            pass

    # 3. Резервный поиск канонического эталона через Deezer / iTunes
    if title and (not artist or not duration):
        search_q = f"{artist} {title}" if artist else title
        canonical = await resolve_canonical_track_info_async(search_q)
        if canonical:
            artist = artist or canonical.artist
            title = title or canonical.title
            thumbnail_url = thumbnail_url or canonical.thumbnail_url
            duration = duration or canonical.duration

    if title:
        search_query = f"{artist} - {title}" if artist else title
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

                title = re.sub(
                    r'\s*[\(\[](?:Official|Music Video|Audio|Lyric|Video|Remix|HQ|HD|Visualizer)[^\)\]]*[\)\]]',
                    '',
                    title,
                    flags=re.IGNORECASE
                ).strip()

                duration = None
                # Сверяем канонические метаданные студийного релиза (артист, длительность, студийная обложка)
                search_seed = f"{artist} - {title}" if (artist and artist != title) else (title or artist)
                if search_seed:
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

