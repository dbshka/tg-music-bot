"""
Resolvers for Yandex Music and VK.
Resolves metadata, cover art, and search queries via Russian proxy route.
The resolved track is then searched and downloaded from YouTube via Foreign proxy route.
"""
import asyncio
import logging
import os
import re
import urllib.parse
from typing import Optional, Tuple, Dict, Any, Union

import requests

import config
from services.vless_proxy import get_proxy_for_source

logger = logging.getLogger(__name__)


def _sync_http_request(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    proxy: Optional[str] = None,
    timeout: Union[float, Tuple[float, float]] = 8.0
) -> requests.Response:
    """Выполняет синхронный HTTP GET запрос с поддержкой SOCKS5 и HTTP прокси через requests."""
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        **(headers or {})
    }
    resp = requests.get(url, headers=req_headers, proxies=proxies, timeout=timeout)
    resp.raise_for_status()
    return resp


def extract_yandex_track_id(url: str) -> Optional[str]:
    """Извлекает ID трека из различных форматов ссылок Яндекс Музыки."""
    parsed = urllib.parse.urlparse(url.strip())
    # Формат 1: /album/123/track/456
    m_track = re.search(r'/track/(\d+)', parsed.path)
    if m_track:
        return m_track.group(1)
    # Формат 2: /album/123?track=456
    qs = urllib.parse.parse_qs(parsed.query)
    if "track" in qs and qs["track"]:
        return qs["track"][0]
    return None


def extract_yandex_album_id(url: str) -> Optional[str]:
    """Извлекает ID альбома из ссылок Яндекс Музыки (если присутствует)."""
    parsed = urllib.parse.urlparse(url.strip())
    m_album = re.search(r'/album/(\d+)', parsed.path)
    if m_album:
        return m_album.group(1)
    return None


def normalize_yandex_url(url: str) -> Optional[str]:
    """
    Нормализует URL трека Яндекс Музыки, удаляя query-параметры (utm_medium, ref_id и т.д.).
    Возвращает канонический URL вида https://music.yandex.ru/album/{album_id}/track/{track_id}
    или https://music.yandex.ru/track/{track_id}.
    """
    track_id = extract_yandex_track_id(url)
    if not track_id:
        return None
    album_id = extract_yandex_album_id(url)
    if album_id:
        return f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
    return f"https://music.yandex.ru/track/{track_id}"


def extract_vk_audio_id(url: str) -> Optional[Tuple[str, str]]:
    """Извлекает (owner_id, audio_id) из ссылок ВКонтакте."""
    m = re.search(r'audio(-?\d+)_(\d+)', url)
    if m:
        return m.group(1), m.group(2)
    return None


async def resolve_yandex_music_track(
    url: str,
    session: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Разрешает метаданные трека Яндекс Музыки через Russian Proxy или прямое подключение.
    Формирует точный поисковый запрос в YouTube без прямого скачивания аудиопотока из Яндекс Музыки.
    """
    parsed = urllib.parse.urlparse(url.strip())
    path = parsed.path.lower()

    # Проверка на ссылки альбомов/плейлистов
    if ("/album/" in path and "/track/" not in path) or "/playlists/" in path or "/users/" in path:
        raise ValueError("Возникла ошибка 31. Отправьте ссылку на отдельный трек.")

    track_id = extract_yandex_track_id(url)
    if not track_id:
        raise ValueError("Возникла ошибка 32. Проверьте ссылку и попробуйте ещё раз.")

    album_id = extract_yandex_album_id(url)
    proxy = get_proxy_for_source("yandex", stage="resolve")
    proxy_label = "RU" if proxy else "DIRECT"
    print(f"[PROXY] source=yandex stage=resolve proxy={proxy_label}", flush=True)

    def _do_resolve() -> Dict[str, Any]:
        headers = {
            "User-Agent": "YandexMusic/24021131 (Android 12; Pixel 6)",
            "Accept": "application/json"
        }
        token = getattr(config, "YANDEX_MUSIC_TOKEN", None) or os.getenv("YANDEX_MUSIC_TOKEN")
        if token and token.strip():
            headers["Authorization"] = f"OAuth {token.strip()}"

        meta_url = f"https://api.music.yandex.net/tracks/{track_id}"
        meta_json = None
        last_error = None

        # 1. Запрос через прокси (если настроен)
        if proxy:
            try:
                resp_meta = _sync_http_request(meta_url, headers=headers, proxy=proxy, timeout=(3.0, 4.0))
                meta_json = resp_meta.json()
            except Exception as e:
                logger.warning(
                    "Ошибка запроса метаданных Яндекс Музыки через прокси %s (track_id=%s): %s. Пробуем напрямую...",
                    proxy, track_id, e
                )
                last_error = e

        # 2. Прямой запрос без прокси (если прокси не задан или через прокси произошла ошибка)
        if not meta_json:
            try:
                resp_meta = _sync_http_request(meta_url, headers=headers, proxy=None, timeout=(3.0, 4.0))
                meta_json = resp_meta.json()
            except Exception as e:
                logger.warning("Ошибка прямого запроса метаданных Яндекс Музыки (track_id=%s): %s", track_id, e)
                last_error = e

        # 3. HTML OpenGraph fallback, если JSON API недоступен
        if not meta_json:
            logger.info("Попытка извлечения метаданных Яндекс Музыки из OpenGraph HTML (track_id=%s)...", track_id)
            html_url = normalize_yandex_url(url) or url
            html_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
            html_resp = None
            try:
                html_resp = _sync_http_request(html_url, headers=html_headers, proxy=None, timeout=(3.0, 4.0))
            except Exception:
                if proxy:
                    try:
                        html_resp = _sync_http_request(html_url, headers=html_headers, proxy=proxy, timeout=(3.0, 4.0))
                    except Exception as e2:
                        last_error = e2

            if html_resp and html_resp.status_code == 200:
                html_text = html_resp.text
                m_title = re.search(r'<meta [^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html_text)
                m_desc = re.search(r'<meta [^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', html_text)
                m_img = re.search(r'<meta [^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', html_text)

                og_title = m_title.group(1).strip() if m_title else None
                og_artist = None
                if m_desc:
                    desc_text = m_desc.group(1)
                    parts = [p.strip() for p in desc_text.split("•") if p.strip()]
                    if parts:
                        og_artist = parts[0]

                if og_title:
                    og_thumb = m_img.group(1).strip() if m_img else None
                    eff_artist = og_artist or "Unknown Artist"
                    search_query = f"{eff_artist} - {og_title}" if (eff_artist and eff_artist.lower() not in og_title.lower()) else og_title
                    return {
                        "platform": "Yandex Music",
                        "target": f"ytsearch5:{search_query}",
                        "is_search": True,
                        "title": og_title,
                        "artist": eff_artist,
                        "album": None,
                        "album_id": album_id,
                        "thumbnail_url": og_thumb,
                        "duration": None,
                        "track_id": track_id
                    }

            raise ValueError("Возникла ошибка 33. Попробуйте ещё раз позже.")

        track_list = meta_json.get("result", [])
        if not track_list:
            raise ValueError("Возникла ошибка 34. Попробуйте другой запрос.")

        track_data = track_list[0]
        title = track_data.get("title")
        artists_list = [a.get("name") for a in track_data.get("artists", []) if a.get("name")]
        artist = ", ".join(artists_list) if artists_list else None

        if not title:
            err_reason = track_data.get("error") or "метаданные недоступны"
            if "plus" in str(err_reason).lower():
                raise ValueError("Возникла ошибка 35. Попробуйте найти этот трек по названию.")
            raise ValueError("Возникла ошибка 36. Попробуйте другой источник.")

        artist = artist or "Unknown Artist"
        dur_ms = track_data.get("durationMs", 0)
        duration = int(round(dur_ms / 1000.0)) if dur_ms else None

        albums_list = track_data.get("albums", [])
        album = albums_list[0].get("title") if (albums_list and isinstance(albums_list[0], dict)) else None
        eff_album_id = album_id
        if not eff_album_id and albums_list and isinstance(albums_list[0], dict):
            raw_aid = albums_list[0].get("id")
            if raw_aid is not None:
                eff_album_id = str(raw_aid)

        cover_uri = track_data.get("ogImage") or track_data.get("coverUri")
        thumbnail_url = f"https://{cover_uri.replace('%%', '600x600')}" if cover_uri else None

        search_query = f"{artist} - {title}" if (artist and artist.lower() not in title.lower()) else title

        return {
            "platform": "Yandex Music",
            "target": f"ytsearch5:{search_query}",
            "is_search": True,
            "title": title,
            "artist": artist,
            "album": album,
            "album_id": eff_album_id,
            "thumbnail_url": thumbnail_url,
            "duration": duration,
            "track_id": track_id
        }

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do_resolve), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Превышен таймаут разрешения трека Яндекс Музыки (track_id=%s, limit=10.0s)", track_id)
        raise ValueError("Возникла ошибка 37. Попробуйте ещё раз позже.")


async def resolve_vk_music_track(
    url: str,
    session: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Разрешает метаданные трека ВКонтакте через Russian Proxy и VK API.
    Формирует точный поисковый запрос в YouTube без прямого скачивания аудиопотока из VK.
    """
    audio_ids = extract_vk_audio_id(url)
    if not audio_ids:
        raise ValueError("Возникла ошибка 38. Проверьте ссылку и попробуйте ещё раз.")

    owner_id, audio_id = audio_ids
    vk_token = getattr(config, "VK_TOKEN", None) or os.getenv("VK_TOKEN")
    if not vk_token or not vk_token.strip():
        raise ValueError("Возникла ошибка 39. Попробуйте другой способ поиска.")

    proxy = get_proxy_for_source("vk", stage="resolve")
    proxy_label = "RU" if proxy else "DIRECT"
    print(f"[PROXY] source=vk stage=resolve proxy={proxy_label}", flush=True)

    def _do_resolve() -> Dict[str, Any]:
        api_url = "https://api.vk.com/method/audio.getById"
        params = {
            "audios": f"{owner_id}_{audio_id}",
            "access_token": vk_token.strip(),
            "v": "5.131"
        }
        full_url = f"{api_url}?{urllib.parse.urlencode(params)}"
        headers = {
            "User-Agent": "KateMobileAndroid/56 lite-arm64-v8a (Android 12; SDK 31; arm64-v8a; Google Pixel 6; ru)",
            "Accept": "application/json"
        }

        try:
            resp = _sync_http_request(full_url, headers=headers, proxy=proxy, timeout=8.0)
            data = resp.json()
        except Exception as e:
            logger.warning("Ошибка обращения к VK API (audios=%s_%s): %s", owner_id, audio_id, e)
            raise ValueError("Возникла ошибка 40. Попробуйте ещё раз позже.")

        if "error" in data:
            err = data["error"]
            err_code = err.get("error_code", 0)
            err_msg = err.get("error_msg", "Неизвестная ошибка")
            if err_code == 5:
                raise ValueError("Возникла ошибка 41. Попробуйте ещё раз позже.")
            raise ValueError("Возникла ошибка 42. Попробуйте ещё раз.")

        items = data.get("response", [])
        if not items:
            raise ValueError("Возникла ошибка 43. Попробуйте другой трек.")

        item = items[0]
        title = item.get("title") or "Unknown Title"
        artists_list = [a.get("name") for a in item.get("main_artists", []) if a.get("name")] if item.get("main_artists") else []
        artist = ", ".join(artists_list) if artists_list else (item.get("artist") or "Unknown Artist")
        duration = item.get("duration") or None

        album_name = None
        thumbnail_url = None
        album_obj = item.get("album")
        if album_obj and isinstance(album_obj, dict):
            album_name = album_obj.get("title")
            thumb = album_obj.get("thumb")
            if thumb and isinstance(thumb, dict):
                thumbnail_url = (
                    thumb.get("photo_600") or
                    thumb.get("photo_300") or
                    thumb.get("photo_1200") or
                    thumb.get("photo_135")
                )

        search_query = f"{artist} - {title}" if (artist and artist.lower() not in title.lower()) else title

        return {
            "platform": "VK Music",
            "target": f"ytsearch5:{search_query}",
            "is_search": True,
            "title": title,
            "artist": artist,
            "album": album_name,
            "thumbnail_url": thumbnail_url,
            "duration": duration,
            "track_id": f"{owner_id}_{audio_id}"
        }

    return await asyncio.to_thread(_do_resolve)
