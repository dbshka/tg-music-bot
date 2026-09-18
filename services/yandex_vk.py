"""
Resolvers for Yandex Music and VK.
Stage 1: Resolve metadata, cover art, and signed media URLs via Russian proxy route.
Stage 2: Hand off signed media URL to downloader via Foreign proxy route.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional, Tuple, Dict, Any, List

import requests

import config
from services.security import is_ip_blocked
from services.vless_proxy import get_proxy_for_source

logger = logging.getLogger(__name__)

YANDEX_ALLOWED_SUFFIXES = (
    "strm.yandex.net",
    "yandex.net",
    "storage.yandex.net",
    "yandex.ru",
    "music.yandex.net",
    "music.yandex.ru",
)

VK_ALLOWED_SUFFIXES = (
    "vkuser.net",
    "vk.com",
    "vk.ru",
    "userapi.com",
    "vkuseraudio.net",
)


import ipaddress

def is_safe_cdn_domain(host: str, allowed_suffixes: Tuple[str, ...]) -> bool:
    """
    Проверяет безопасность хоста CDN:
    1. Запрещает localhost, 127.0.0.1 и сырые IP-адреса.
    2. Домен обязан оканчиваться на один из разрешенных суффиксов.
    3. Защищает от DNS-спуфинга и SSRF.
    """
    if not host:
        return False
    h = host.strip().lower()
    if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False

    # CDN серверы обязаны использовать доменные имена, а не сырые IP-адреса
    try:
        ipaddress.ip_address(h)
        return False
    except ValueError:
        pass

    matches_suffix = any(h == s or h.endswith("." + s) for s in allowed_suffixes)
    return matches_suffix


def _sync_http_request(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    proxy: Optional[str] = None,
    timeout: float = 8.0
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
    Разрешает метаданные и подписанную прямую ссылку на трек Яндекс Музыки через Russian Proxy.
    Строго отслеживает и отклоняет превью-треки (preview: True) для сохранения аутентичности.
    """
    parsed = urllib.parse.urlparse(url.strip())
    path = parsed.path.lower()

    # Проверка на ссылки альбомов/плейлистов
    if ("/album/" in path and "/track/" not in path) or "/playlists/" in path or "/users/" in path:
        raise ValueError(
            "Загрузка альбомов и плейлистов не поддерживается.\n\n"
            "💡 Пожалуйста, отправьте ссылку на конкретный трек."
        )

    track_id = extract_yandex_track_id(url)
    if not track_id:
        raise ValueError(
            "Не удалось определить ID трека в ссылке Яндекс Музыки.\n\n"
            "💡 Пожалуйста, отправьте прямую ссылку на трек (например: https://music.yandex.ru/track/60292250)."
        )

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

        # 1. Получение метаданных трека
        meta_url = f"https://api.music.yandex.net/tracks/{track_id}"
        try:
            resp_meta = _sync_http_request(meta_url, headers=headers, proxy=proxy, timeout=8.0)
            meta_json = resp_meta.json()
        except Exception as e:
            logger.warning("Ошибка запроса метаданных Яндекс Музыки (track_id=%s): %s", track_id, e)
            raise ValueError(
                f"Не удалось связаться с сервером Яндекс Музыки ({e}). "
                f"Убедитесь, что настроен рабочий российский прокси (VLESS_RU_URL)."
            )

        track_list = meta_json.get("result", [])
        if not track_list:
            raise ValueError("Трек не найден в каталоге Яндекс Музыки.")

        track_data = track_list[0]
        if track_data.get("available") is False or track_data.get("error"):
            err_reason = track_data.get("error") or "недоступен"
            raise ValueError(f"Трек недоступен в каталоге Яндекс Музыки ({err_reason}).")

        title = track_data.get("title") or "Unknown Title"
        artists_list = [a.get("name") for a in track_data.get("artists", []) if a.get("name")]
        artist = ", ".join(artists_list) if artists_list else "Unknown Artist"
        dur_ms = track_data.get("durationMs", 0)
        duration = int(round(dur_ms / 1000.0)) if dur_ms else None

        cover_uri = track_data.get("ogImage") or track_data.get("coverUri")
        thumbnail_url = f"https://{cover_uri.replace('%%', '600x600')}" if cover_uri else None

        # 2. Получение download-info (адрес расположения медиапотока)
        d_url = f"https://api.music.yandex.net/tracks/{track_id}/download-info"
        try:
            resp_d = _sync_http_request(d_url, headers=headers, proxy=proxy, timeout=8.0)
            d_json = resp_d.json()
        except Exception as e:
            logger.warning("Ошибка получения download-info Яндекс Музыки (track_id=%s): %s", track_id, e)
            raise ValueError(f"Не удалось получить ссылку на аудиопоток Яндекс Музыки: {e}")

        d_items = d_json.get("result", [])
        if not d_items:
            raise ValueError("Яндекс Музыка не предоставила вариантов загрузки для этого трека.")

        # Выбираем MP3 с наивысшим битрейтом
        mp3_items = [item for item in d_items if item.get("codec") == "mp3"]
        mp3_items.sort(key=lambda x: x.get("bitrateInKbps", 0), reverse=True)
        chosen_info = mp3_items[0] if mp3_items else d_items[0]

        # 3. ПРОВЕРКА ПРЕВЬЮ: Строгий запрет на подмену полного трека превью
        is_preview = bool(chosen_info.get("preview") or track_data.get("preview"))
        if is_preview:
            raise ValueError(
                "⚠️ Этот трек доступен только по подписке Яндекс Плюс (сервис отдаёт лишь 30-секундный фрагмент во избежание подмены).\n\n"
                "💡 Укажите YANDEX_MUSIC_TOKEN с активной подпиской или отправьте название трека текстом для поиска альтернативного источника."
            )

        download_info_url = chosen_info.get("downloadInfoUrl")
        if not download_info_url:
            raise ValueError("В ответе Яндекс Музыки отсутствует URL расположения файла.")

        # SSRF-проверка downloadInfoUrl
        d_parsed = urllib.parse.urlparse(download_info_url)
        if not is_safe_cdn_domain(d_parsed.hostname or "", YANDEX_ALLOWED_SUFFIXES):
            raise ValueError(f"Недопустимый сервер хранилища Яндекс Музыки: {d_parsed.hostname}")

        # 4. Запрос XML данных хранилища
        try:
            resp_xml = _sync_http_request(download_info_url, headers=headers, proxy=proxy, timeout=8.0)
            xml_text = resp_xml.text
        except Exception as e:
            raise ValueError(f"Ошибка запроса адреса хранилища Яндекс Музыки: {e}")

        root = ET.fromstring(xml_text)
        fields = {child.tag: (child.text or "").strip() for child in root}
        host = fields.get("host")
        fpath = fields.get("path")
        ts = fields.get("ts")
        s = fields.get("s")

        if not host or not fpath or not ts or not s:
            raise ValueError("Некорректная структура XML ответа хранилища Яндекс Музыки.")

        # SSRF-проверка хоста CDN стриминга
        if not is_safe_cdn_domain(host, YANDEX_ALLOWED_SUFFIXES):
            raise ValueError(f"Недопустимый хост CDN Яндекс Музыки: {host}")

        # 5. Вычисление секретного MD5-ключа и формирование подписанного медиа-URL
        secret_salt = "XGRlBW9FXlekgbPrRHuSiA"
        clean_path = fpath[1:] if fpath.startswith("/") else fpath
        key_input = f"{secret_salt}{clean_path}{s}"
        sign_key = hashlib.md5(key_input.encode("utf-8")).hexdigest()

        signed_media_url = f"https://{host}/get-mp3/{sign_key}/{ts}/{clean_path}?track-id={track_id}"

        return {
            "platform": "Yandex Music",
            "target": signed_media_url,
            "is_search": False,
            "title": title,
            "artist": artist,
            "thumbnail_url": thumbnail_url,
            "duration": duration,
            "track_id": track_id
        }

    return await asyncio.to_thread(_do_resolve)


async def resolve_vk_music_track(
    url: str,
    session: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Разрешает метаданные и прямую ссылку на трек ВКонтакте через Russian Proxy и VK API.
    Требует наличия VK_TOKEN (аккаунт пользователя или токен приложения Kate Mobile / Android).
    """
    audio_ids = extract_vk_audio_id(url)
    if not audio_ids:
        raise ValueError(
            "Не удалось определить ID аудиозаписи ВКонтакте.\n\n"
            "💡 Пример правильной ссылки: https://vk.com/audio-2001429780_128429780"
        )

    owner_id, audio_id = audio_ids
    vk_token = getattr(config, "VK_TOKEN", None) or os.getenv("VK_TOKEN")
    if not vk_token or not vk_token.strip():
        raise ValueError(
            "⚠️ Загрузка по прямым ссылкам ВК Музыки требует указания VK_TOKEN в настройках бота "
            "(ВКонтакте закрыл публичный доступ к аудиозаписям без авторизации).\n\n"
            "💡 Пожалуйста, отправьте название трека или исполнителя текстом (например: MiyaGi — Captain), "
            "и бот моментально найдёт и пришлёт MP3!"
        )

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
            raise ValueError(f"Не удалось связаться с серверами ВКонтакте: {e}")

        if "error" in data:
            err = data["error"]
            err_code = err.get("error_code", 0)
            err_msg = err.get("error_msg", "Неизвестная ошибка")
            if err_code == 5:
                raise ValueError("Токен авторизации ВКонтакте (VK_TOKEN) недействителен или истёк.")
            raise ValueError(f"Ошибка API ВКонтакте: {err_msg} (код {err_code})")

        items = data.get("response", [])
        if not items:
            raise ValueError("Аудиозапись не найдена или была удалена из ВКонтакте.")

        item = items[0]
        title = item.get("title") or "Unknown Title"
        artist = item.get("artist") or "Unknown Artist"
        duration = item.get("duration") or None
        media_url = item.get("url")

        if not media_url:
            raise ValueError(
                "ВКонтакте не предоставил прямую ссылку на аудиофайл "
                "(возможно, трек заблокирован правообладателем или недоступен для данного аккаунта)."
            )

        # SSRF-проверка ссылки на аудиофайл ВКонтакте
        u_parsed = urllib.parse.urlparse(media_url)
        if not is_safe_cdn_domain(u_parsed.hostname or "", VK_ALLOWED_SUFFIXES):
            raise ValueError(f"Недопустимый сервер аудиопотока ВКонтакте: {u_parsed.hostname}")

        # Попытка извлечь обложку альбома
        thumbnail_url = None
        album = item.get("album")
        if album and isinstance(album, dict):
            thumb = album.get("thumb")
            if thumb and isinstance(thumb, dict):
                thumbnail_url = (
                    thumb.get("photo_600") or
                    thumb.get("photo_300") or
                    thumb.get("photo_1200") or
                    thumb.get("photo_135")
                )

        return {
            "platform": "VK Music",
            "target": media_url,
            "is_search": False,
            "title": title,
            "artist": artist,
            "thumbnail_url": thumbnail_url,
            "duration": duration,
            "track_id": f"{owner_id}_{audio_id}"
        }

    return await asyncio.to_thread(_do_resolve)
