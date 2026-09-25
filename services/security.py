# services/security.py
import ipaddress
import logging
import socket
import urllib.parse
from typing import Tuple, Optional
import aiohttp

logger = logging.getLogger(__name__)

# Запрещенные IP-диапазоны (RFC 1918, Loopback, Link-Local, Cloud Metadata, Multicast, Unspecified)
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/GCP/Azure Instance Metadata 169.254.169.254
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::/128"),
]


def is_ip_blocked(ip_str: str) -> bool:
    """Проверяет, входит ли данный IP адрес в список запрещенных приватных/локальных сетей."""
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified:
            return True
        for net in BLOCKED_NETWORKS:
            if ip_obj in net:
                return True
        return False
    except ValueError:
        return True


def is_safe_url(url: str) -> Tuple[bool, str]:
    """
    Проверяет URL на допустимость и отсутствие SSRF:
    - Схема только http или https;
    - DNS resolution проверяет все полученные IP;
    - Запрещает localhost, 127.0.0.1, 169.254.169.254, RFC1918 и IPv6 аналоги.
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False, "Возникла ошибка 53. Проверьте ссылку."

    if parsed.scheme.lower() not in ("http", "https"):
        return False, "Возникла ошибка 54. Проверьте ссылку."

    hostname = parsed.hostname
    if not hostname:
        return False, "Возникла ошибка 55. Проверьте ссылку."

    # Защита от прямого указания localhost
    if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False, "Возникла ошибка 56. Проверьте ссылку."

    # Проверка, является ли hostname уже IP-адресом
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if is_ip_blocked(str(ip_obj)):
            return False, "Возникла ошибка 57. Проверьте ссылку."
        return True, ""
    except ValueError:
        pass

    # DNS разрешение имени хоста
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False, "Возникла ошибка 59. Проверьте ссылку и попробуйте ещё раз."
        for family, socktype, proto, canonname, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if is_ip_blocked(ip_str):
                return False, "Возникла ошибка 58. Проверьте ссылку."
    except socket.gaierror as e:
        return False, "Возникла ошибка 60. Проверьте ссылку и попробуйте ещё раз."
    except Exception as e:
        return False, "Возникла ошибка 61. Проверьте ссылку и попробуйте ещё раз."

    return True, ""


async def safe_unshorten_url(url: str, session: aiohttp.ClientSession, max_redirects: int = 5) -> str:
    """
    Безопасно раскрывает сокращенные ссылки (ya.cc, clck.ru, spotify.link и т.д.)
    с валидацией SSRF перед КАЖДЫМ шагом редиректа.
    """
    current_url = url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    for _ in range(max_redirects):
        is_safe, reason = is_safe_url(current_url)
        if not is_safe:
            logger.warning("SSRF заблокирован при редиректе: %s (%s)", current_url, reason)
            raise ValueError("Возникла ошибка 62. Проверьте ссылку и попробуйте ещё раз.")

        parsed = urllib.parse.urlparse(current_url)
        short_domains = ("ya.cc", "clck.ru", "vk.cc", "t.co", "goo.gl", "bit.ly", "spotify.link", "band.link", "tinyurl.com")
        if not any(sd in parsed.netloc.lower() for sd in short_domains):
            return current_url

        try:
            async with session.head(current_url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        break
                    next_url = urllib.parse.urljoin(current_url, loc)
                    current_url = next_url
                    continue
                else:
                    break
        except Exception:
            try:
                async with session.get(current_url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            break
                        next_url = urllib.parse.urljoin(current_url, loc)
                        current_url = next_url
                        continue
                    else:
                        break
            except Exception:
                break

    return current_url
