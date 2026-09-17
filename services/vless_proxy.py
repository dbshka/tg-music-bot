"""
VLESS / Happ VPN proxy manager.
Преобразует ссылку VLESS (vless://...) или ссылку на подписку в локальный SOCKS5-прокси через sing-box.
"""
import os
import sys
import json
import atexit
import base64
import shutil
import socket
import logging
import platform
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any

from config import BASE_DIR, get_sanitized_proxy_info
import config

logger = logging.getLogger(__name__)

SINGBOX_VERSION = "1.10.7"
DEFAULT_SOCKS_HOST = "127.0.0.1"
DEFAULT_SOCKS_PORT = 10808


def resolve_subscription_if_needed(url_or_sub: str, timeout: float = 10.0) -> str:
    """
    Если передана HTTP/HTTPS ссылка на подписку (Happ / V2Ray / Xray),
    скачивает содержимое, декодирует Base64 и возвращает первую vless:// ссылку.
    Если уже передана vless:// ссылка, возвращает её без изменений.
    """
    cleaned = url_or_sub.strip()
    if not cleaned.startswith(("http://", "https://")):
        return cleaned

    req = urllib.request.Request(
        cleaned,
        headers={"User-Agent": "Happ/1.0 v2rayN/6.0 sing-box/1.10"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read().decode("utf-8", errors="ignore").strip()

    lines = []
    if not content.startswith("vless://"):
        try:
            # Нормализация паддинга Base64
            padded = content + "=" * (-len(content) % 4)
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
            lines = [ln.strip() for ln in decoded.splitlines() if ln.strip()]
        except Exception:
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    else:
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]

    for line in lines:
        if line.startswith("vless://"):
            try:
                # Проверка структуры ноды перед возвратом
                parse_vless_url(line)
                return line
            except Exception:
                continue

    raise ValueError(f"Не удалось найти валидную vless:// ссылку в подписке {cleaned}")


def parse_vless_url(vless_url: str) -> Dict[str, Any]:
    """
    Парсит vless:// ссылку и преобразует её в outbound-объект sing-box.
    Поддерживает: Reality, TLS, TCP, WebSocket (ws), gRPC, xtls-rprx-vision.
    """
    u = urllib.parse.urlparse(vless_url.strip())
    if u.scheme != "vless":
        raise ValueError(f"Ожидалась схема vless://, получено {u.scheme}://")

    if not u.username or not u.hostname:
        raise ValueError("В ссылке VLESS отсутствует UUID или хост сервера")

    qs_raw = urllib.parse.parse_qs(u.query)
    qs = {k: v[0] for k, v in qs_raw.items()}

    security = qs.get("security", "none").lower()
    transport_type = qs.get("type", "tcp").lower()
    flow = qs.get("flow", "")
    sni = qs.get("sni") or u.hostname
    fp = qs.get("fp", "chrome")

    outbound: Dict[str, Any] = {
        "type": "vless",
        "tag": "vless-out",
        "server": u.hostname,
        "server_port": u.port or 443,
        "uuid": u.username,
    }

    if flow:
        outbound["flow"] = flow

    # Транспорт
    if transport_type == "ws":
        ws_headers = {}
        host_hdr = qs.get("host") or sni
        if host_hdr:
            ws_headers["Host"] = host_hdr
        outbound["transport"] = {
            "type": "ws",
            "path": urllib.parse.unquote(qs.get("path", "/")),
            "headers": ws_headers
        }
    elif transport_type == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": qs.get("serviceName", "")
        }

    # Безопасность / Шифрование
    if security == "reality":
        reality_conf: Dict[str, Any] = {
            "enabled": True,
            "public_key": qs.get("pbk", ""),
        }
        sid = qs.get("sid", "")
        if sid:
            reality_conf["short_id"] = sid
        spx = qs.get("spx", "")
        if spx:
            reality_conf["spider_x"] = urllib.parse.unquote(spx)

        outbound["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {
                "enabled": True,
                "fingerprint": fp
            },
            "reality": reality_conf
        }
    elif security == "tls":
        tls_conf: Dict[str, Any] = {
            "enabled": True,
            "server_name": sni
        }
        if fp:
            tls_conf["utls"] = {"enabled": True, "fingerprint": fp}
        if qs.get("alpn"):
            tls_conf["alpn"] = [a.strip() for a in qs["alpn"].split(",")]
        outbound["tls"] = tls_conf

    return outbound


def build_singbox_config(
    outbound: Dict[str, Any],
    socks_host: str = DEFAULT_SOCKS_HOST,
    socks_port: int = DEFAULT_SOCKS_PORT
) -> Dict[str, Any]:
    """Формирует полную конфигурацию sing-box с локальным SOCKS5-инбаундом."""
    return {
        "log": {
            "level": "warn",
            "timestamp": True
        },
        "inbounds": [
            {
                "type": "socks",
                "tag": "socks-in",
                "listen": socks_host,
                "listen_port": socks_port
            }
        ],
        "outbounds": [
            outbound
        ]
    }


def ensure_singbox_binary() -> Optional[Path]:
    """
    Проверяет наличие бинарника sing-box:
    1. Системный в PATH (например, установленный через Dockerfile).
    2. Локальный в BASE_DIR/.bin/.
    3. Если Linux: скачивает официальный статический релиз sing-box в BASE_DIR/.bin/.
    """
    # 1. Системный путь
    sys_path = shutil.which("sing-box")
    if sys_path:
        return Path(sys_path)

    bin_dir = BASE_DIR / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    local_bin = bin_dir / ("sing-box.exe" if sys.platform == "win32" else "sing-box")
    if local_bin.exists() and os.access(local_bin, os.X_OK):
        return local_bin

    # 2. Автозагрузка для Linux
    if sys.platform.startswith("linux"):
        machine = platform.machine().lower()
        if machine in ("x86_64", "amd64"):
            arch = "amd64"
        elif machine in ("aarch64", "arm64"):
            arch = "arm64"
        else:
            arch = "amd64"

        tar_url = f"https://github.com/SagerNet/sing-box/releases/download/v{SINGBOX_VERSION}/sing-box-{SINGBOX_VERSION}-linux-{arch}.tar.gz"
        print(f"[VLESS] Загрузка sing-box v{SINGBOX_VERSION} ({arch}) для Linux...", flush=True)
        try:
            import tarfile
            tar_path = bin_dir / "sing-box.tar.gz"
            urllib.request.urlretrieve(tar_url, tar_path)
            with tarfile.open(tar_path, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("/sing-box") or member.name == "sing-box":
                        f = tar.extractfile(member)
                        if f:
                            with open(local_bin, "wb") as out_f:
                                out_f.write(f.read())
                            break
            tar_path.unlink(missing_ok=True)
            local_bin.chmod(0o755)
            print(f"[VLESS] sing-box успешно установлен: {local_bin}", flush=True)
            return local_bin
        except Exception as e:
            print(f"[VLESS] Не удалось автоматически загрузить sing-box: {e}", flush=True)
            return None

    # 3. Автозагрузка для Windows
    if sys.platform == "win32":
        zip_url = f"https://github.com/SagerNet/sing-box/releases/download/v{SINGBOX_VERSION}/sing-box-{SINGBOX_VERSION}-windows-amd64.zip"
        try:
            import zipfile
            zip_path = bin_dir / "sing-box.zip"
            urllib.request.urlretrieve(zip_url, zip_path)
            with zipfile.ZipFile(zip_path) as z:
                for name in z.namelist():
                    if name.endswith("sing-box.exe"):
                        with open(local_bin, "wb") as out_f:
                            out_f.write(z.read(name))
                        break
            zip_path.unlink(missing_ok=True)
            return local_bin
        except Exception as e:
            print(f"[VLESS] Не удалось автоматически загрузить sing-box для Windows: {e}", flush=True)
            return None

    return None


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Проверяет доступность TCP-порта."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_active_process: Optional[subprocess.Popen] = None


def start_vless_proxy(
    vless_input: Optional[str] = None,
    socks_host: str = DEFAULT_SOCKS_HOST,
    socks_port: int = DEFAULT_SOCKS_PORT,
    timeout_secs: float = 6.0
) -> Optional[subprocess.Popen]:
    """
    Запускает sing-box SOCKS5 прокси из VLESS ссылки или подписки.
    Обновляет config.YOUTUBE_PROXY при успешном запуске.
    """
    global _active_process

    raw_link = vless_input or getattr(config, "VLESS_URL", None) or os.getenv("VLESS_URL") or os.getenv("YOUTUBE_VLESS_URL")
    if not raw_link:
        return None

    bin_path = ensure_singbox_binary()
    if not bin_path:
        print("[VLESS] Бинарник sing-box не найден в системе. Прокси VLESS не запущен.", flush=True)
        return None

    try:
        resolved_link = resolve_subscription_if_needed(raw_link)
        outbound = parse_vless_url(resolved_link)
        server_info = f"{outbound.get('server')}:{outbound.get('server_port')}"
        sb_config = build_singbox_config(outbound, socks_host=socks_host, socks_port=socks_port)

        config_dir = BASE_DIR / ".bin"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "singbox.json"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(sb_config, f, indent=2)

        print(f"[VLESS] Запуск sing-box туннеля к {server_info} на {socks_host}:{socks_port}...", flush=True)
        proc = subprocess.Popen(
            [str(bin_path), "run", "-c", str(config_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        # Ожидание готовности порта
        import time
        start_t = time.time()
        ready = False
        while time.time() - start_t < timeout_secs:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else "unknown error"
                print(f"[VLESS] Ошибка: sing-box завершился преждевременно: {err}", flush=True)
                return None
            if is_port_open(socks_host, socks_port):
                ready = True
                break
            time.sleep(0.2)

        if ready:
            proxy_url = f"socks5://{socks_host}:{socks_port}"
            config.YOUTUBE_PROXY = proxy_url
            _active_process = proc
            print(f"[VLESS] ✅ sing-box успешно запущен на {proxy_url} (сервер: {server_info})", flush=True)
            return proc
        else:
            print(f"[VLESS] ⚠️ sing-box не ответил за {timeout_secs}с на {socks_host}:{socks_port}", flush=True)
            stop_vless_proxy(proc)
            return None

    except Exception as e:
        print(f"[VLESS] Ошибка инициализации VLESS: {e}", flush=True)
        return None


def stop_vless_proxy(proc: Optional[subprocess.Popen] = None) -> None:
    """Корректно останавливает процесс sing-box."""
    global _active_process
    target = proc or _active_process
    if target and target.poll() is None:
        print("[VLESS] Остановка sing-box процесса...", flush=True)
        try:
            target.terminate()
            target.wait(timeout=3)
        except Exception:
            try:
                target.kill()
            except Exception:
                pass
    if target == _active_process:
        _active_process = None

    # Очистка временного файла конфигурации sing-box
    try:
        cfg_file = BASE_DIR / ".bin" / "singbox.json"
        cfg_file.unlink(missing_ok=True)
    except Exception:
        pass


# Автоматическая очистка процесса при завершении работы интерпретатора Python
atexit.register(stop_vless_proxy)
