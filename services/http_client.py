import aiohttp
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SHARED_SESSION: Optional[aiohttp.ClientSession] = None


def get_shared_session() -> aiohttp.ClientSession:
    """
    Возвращает синглтон aiohttp.ClientSession с connection pooling,
    DNS кэшированием и TCP keep-alive.
    Автоматически пересоздает сессию, если текущий event loop изменился или был закрыт.
    """
    global _SHARED_SESSION
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    needs_new = False
    if _SHARED_SESSION is None or _SHARED_SESSION.closed:
        needs_new = True
    elif current_loop and getattr(_SHARED_SESSION, "_loop", None) is not current_loop:
        needs_new = True
    elif getattr(_SHARED_SESSION, "_loop", None) and _SHARED_SESSION._loop.is_closed():
        needs_new = True

    if needs_new:
        connector = aiohttp.TCPConnector(
            limit=50,
            limit_per_host=10,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
            ttl_dns_cache=300
        )
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"
        }
        _SHARED_SESSION = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers
        )
    return _SHARED_SESSION


async def close_shared_session():
    """Корректно закрывает соединение при остановке бота."""
    global _SHARED_SESSION
    if _SHARED_SESSION and not _SHARED_SESSION.closed:
        try:
            await _SHARED_SESSION.close()
            # Даем 250 мс на закрытие открытых SSL сокетов в asyncio
            await asyncio.sleep(0.25)
        except Exception as e:
            logger.debug("Ошибка закрытия shared aiohttp session: %s", e)
        finally:
            _SHARED_SESSION = None
