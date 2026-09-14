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
    """
    global _SHARED_SESSION
    if _SHARED_SESSION is None or _SHARED_SESSION.closed:
        connector = aiohttp.TCPConnector(
            limit=50,
            limit_per_host=10,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
            ttl_dns_cache=300
        )
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        _SHARED_SESSION = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout
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
