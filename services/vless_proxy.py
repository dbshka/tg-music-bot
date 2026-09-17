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
from typing import Optional, Dict, Any, Tuple, List

from config import BASE_DIR, get_sanitized_proxy_info
import config

logger = logging.getLogger(__name__)

SINGBOX_VERSION = "1.10.7"
DEFAULT_SOCKS_HOST = "127.0.0.1"
DEFAULT_SOCKS_PORT = 10808


def is_valid_remote_server(server: str, port: int) -> bool:
    """
    Проверяет, что сервер не является локальной заглушкой (0.0.0.0, 127.0.0.1, localhost, ::1)
    и порт валиден (порт > 1 и <= 65535). Порты 0 и 1 часто используются для информационных
    нод-заглушек в подписках VPN-провайдеров.
    """
    if not server or not port:
        return False
    srv = server.strip().lower()
    invalid_hosts = {
        "0.0.0.0", "127.0.0.1", "localhost", "::1", "::", "127.0.0.0", "255.255.255.255"
    }
    if srv in invalid_hosts or srv.startswith("127."):
        return False
    if port in (0, 1) or not (1 <= port <= 65535):
        return False
    return True


def parse_vless_url(vless_url: str) -> Dict[str, Any]:
    """
    Парсит vless:// ссылку и преобразует её в outbound-объект sing-box.
    Поддерживает: Reality, TLS, TCP, WebSocket (ws), gRPC, xtls-rprx-vision.
    Выбрасывает ValueError, если хост/порт не является валидным удалённым сервером.
    """
    u = urllib.parse.urlparse(vless_url.strip())
    if u.scheme != "vless":
        raise ValueError(f"Ожидалась схема vless://, получено {u.scheme}://")

    if not u.username or not u.hostname:
        raise ValueError("В ссылке VLESS отсутствует UUID или хост сервера")

    port = u.port or 443
    if not is_valid_remote_server(u.hostname, port):
        raise ValueError(f"Недопустимый удалённый сервер VLESS: {u.hostname}:{port} (заглушка 0.0.0.0/127.0.0.1 или порт 1)")

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
        "server_port": port,
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


def get_sanitized_outbound_summary(outbound: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает безопасную сводку параметров outbound без приватных ключей и UUID."""
    tls_info = outbound.get("tls", {})
    reality_info = tls_info.get("reality", {})
    transport_info = outbound.get("transport", {})

    return {
        "outbound_type": outbound.get("type", "unknown"),
        "server": outbound.get("server", "unknown"),
        "server_port": outbound.get("server_port", 0),
        "security": "reality" if reality_info.get("enabled") else ("tls" if tls_info.get("enabled") else "none"),
        "transport_type": transport_info.get("type", "tcp"),
        "tls_enabled": bool(tls_info.get("enabled")),
        "reality_enabled": bool(reality_info.get("enabled")),
        "flow": outbound.get("flow") or "none"
    }


def resolve_subscription_if_needed(url_or_sub: str, timeout: float = 10.0) -> Tuple[str, Dict[str, Any]]:
    """
    Если передана HTTP/HTTPS ссылка на подписку (Happ / V2Ray / Xray),
    скачивает содержимое, декодирует Base64, отфильтровывает информационные заглушки (0.0.0.0/1)
    и возвращает лучшую валидную VLESS ссылку и её распарсенный outbound.
    Если уже передана vless:// ссылка, парсит и валидирует её напрямую.
    """
    cleaned = url_or_sub.strip()
    if not cleaned.startswith(("http://", "https://")):
        outbound = parse_vless_url(cleaned)
        summary = get_sanitized_outbound_summary(outbound)
        print(f"[VLESS] Получена прямая VLESS ссылка: host={summary['server']}:{summary['server_port']}, security={summary['security']}, transport={summary['transport_type']}", flush=True)
        return cleaned, outbound

    print("[VLESS] Загрузка подписки по URL (URL скрыт в целях безопасности)...", flush=True)
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

    valid_candidates: List[Tuple[str, Dict[str, Any]]] = []
    dummy_count = 0
    other_protocol_count = 0

    for idx, line in enumerate(lines, 1):
        if not line.startswith("vless://"):
            other_protocol_count += 1
            continue
        try:
            outbound = parse_vless_url(line)
            valid_candidates.append((line, outbound))
        except ValueError as ve:
            # Отлов отфильтрованных нод (0.0.0.0:1 и др.)
            if "0.0.0.0" in str(ve) or "127.0.0.1" in str(ve) or "порт 1" in str(ve):
                dummy_count += 1
            else:
                pass

    print(f"[VLESS] Результаты анализа подписки: всего строк={len(lines)}, рабочих VLESS={len(valid_candidates)}, инфо-заглушек={dummy_count}, других протоколов={other_protocol_count}", flush=True)

    if not valid_candidates:
        raise ValueError(
            f"В подписке не найдено ни одного рабочего VLESS сервера! "
            f"(Всего строк: {len(lines)}, отфильтровано заглушек 0.0.0.0/127.0.0.1: {dummy_count})"
        )

    # Выбор лучшей ноды: приоритет Reality > TLS > прочие
    def score_node(item: Tuple[str, Dict[str, Any]]) -> int:
        ob = item[1]
        tls_info = ob.get("tls", {})
        if tls_info.get("reality", {}).get("enabled"):
            return 100
        if tls_info.get("enabled"):
            return 50
        return 10

    valid_candidates.sort(key=score_node, reverse=True)
    chosen_link, chosen_outbound = valid_candidates[0]
    chosen_summary = get_sanitized_outbound_summary(chosen_outbound)

    print(
        f"[VLESS] Выбрана рабочая нода: host={chosen_summary['server']}:{chosen_summary['server_port']} "
        f"(security={chosen_summary['security']}, transport={chosen_summary['transport_type']}, "
        f"причина: наивысший приоритет из {len(valid_candidates)} доступных нод)",
        flush=True
    )
    return chosen_link, chosen_outbound


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
    3. Если Linux / Windows: скачивает официальный статический релиз sing-box в BASE_DIR/.bin/.
    """
    sys_path = shutil.which("sing-box")
    if sys_path:
        return Path(sys_path)

    bin_dir = BASE_DIR / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    local_bin = bin_dir / ("sing-box.exe" if sys.platform == "win32" else "sing-box")
    if local_bin.exists() and os.access(local_bin, os.X_OK):
        return local_bin

    # Автозагрузка для Linux
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

    # Автозагрузка для Windows
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
    """Проверяет доступность локального TCP-порта."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def verify_outbound_connectivity(socks_host: str, socks_port: int, timeout: float = 6.0) -> Tuple[bool, str]:
    """
    Проверяет фактическое внешнее сетевое подключение через локальный SOCKS5 прокси sing-box.
    Разделяет факт старта SOCKS5 listener и реальное соединение с интернетом через VLESS-сервер.
    """
    # 1. Проверка через curl (если доступен в системе)
    try:
        res = subprocess.run(
            [
                "curl", "-s", "-m", str(int(timeout)),
                "--socks5-hostname", f"{socks_host}:{socks_port}",
                "http://api.ipify.org?format=json"
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 1
        )
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout.strip())
                ip = data.get("ip", "unknown")
                return True, ip
            except Exception:
                return True, res.stdout.strip()[:40]
    except Exception:
        pass

    # 2. Fallback через нативный socket SOCKS5 HTTP GET к api.ipify.org
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((socks_host, socks_port))
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            s.close()
            return False, "SOCKS5 authentication failed"
        target = b"api.ipify.org"
        req = b"\x05\x01\x00\x03" + bytes([len(target)]) + target + (80).to_bytes(2, "big")
        s.sendall(req)
        reply = s.recv(10)
        if len(reply) < 4 or reply[1] != 0:
            err_code = reply[1] if len(reply) >= 2 else -1
            s.close()
            return False, f"SOCKS5 remote connection failed (code {err_code})"
        s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        resp = s.recv(1024).decode("utf-8", errors="ignore")
        s.close()
        if "HTTP/1.1 200" in resp:
            parts = resp.split("\r\n\r\n", 1)
            ext_ip = parts[1].strip() if len(parts) > 1 else "success"
            return True, ext_ip
        return False, "Non-200 HTTP response from test endpoint"
    except Exception as e:
        return False, str(e)


_active_process: Optional[subprocess.Popen] = None


def start_vless_proxy(
    vless_input: Optional[str] = None,
    socks_host: str = DEFAULT_SOCKS_HOST,
    socks_port: int = DEFAULT_SOCKS_PORT,
    timeout_secs: float = 6.0
) -> Optional[subprocess.Popen]:
    """
    Запускает sing-box SOCKS5 прокси из VLESS ссылки или подписки.
    Строго проверяет:
    1. Что сервер ноды не является заглушкой 0.0.0.0 / 127.0.0.1.
    2. Что процесс sing-box запустился и открыл порт (SOCKS5 listener started).
    3. Что через туннель проходит реальный трафик в интернет (VLESS outbound connection established).
    Обновляет config.YOUTUBE_PROXY ТОЛЬКО при успешном прохождении проверок.
    """
    global _active_process

    raw_link = vless_input or getattr(config, "VLESS_URL", None) or os.getenv("VLESS_URL") or os.getenv("YOUTUBE_VLESS_URL")
    if not raw_link:
        return None

    bin_path = ensure_singbox_binary()
    if not bin_path:
        print("[VLESS] ❌ Бинарник sing-box не найден в системе. Прокси VLESS не запущен.", flush=True)
        return None

    try:
        resolved_link, outbound = resolve_subscription_if_needed(raw_link)
        summary = get_sanitized_outbound_summary(outbound)
        server_info = f"{summary['server']}:{summary['server_port']}"

        # Детальная санитизированная сводка конфигурации перед стартом
        print(f"[VLESS] Конфигурация sing-box outbound:", flush=True)
        print(f"[VLESS]   * type: {summary['outbound_type']}", flush=True)
        print(f"[VLESS]   * server: {summary['server']}:{summary['server_port']}", flush=True)
        print(f"[VLESS]   * security: {summary['security']}", flush=True)
        print(f"[VLESS]   * transport: {summary['transport_type']}", flush=True)
        print(f"[VLESS]   * tls_enabled: {summary['tls_enabled']}", flush=True)
        print(f"[VLESS]   * reality_enabled: {summary['reality_enabled']}", flush=True)
        print(f"[VLESS]   * flow: {summary['flow']}", flush=True)

        sb_config = build_singbox_config(outbound, socks_host=socks_host, socks_port=socks_port)

        config_dir = BASE_DIR / ".bin"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "singbox.json"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(sb_config, f, indent=2)

        print(f"[VLESS] Запуск процесса sing-box на {socks_host}:{socks_port}...", flush=True)
        proc = subprocess.Popen(
            [str(bin_path), "run", "-c", str(config_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        # 1. Проверка доступности локального SOCKS5 listener
        import time
        start_t = time.time()
        listener_ready = False
        while time.time() - start_t < timeout_secs:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else "unknown error"
                print(f"[VLESS] ❌ Ошибка: процесс sing-box завершился преждевременно: {err}", flush=True)
                return None
            if is_port_open(socks_host, socks_port):
                listener_ready = True
                break
            time.sleep(0.2)

        if not listener_ready:
            print(f"[VLESS] ⚠️ sing-box не открыл порт SOCKS5 за {timeout_secs}с на {socks_host}:{socks_port}", flush=True)
            stop_vless_proxy(proc)
            return None

        print(f"[VLESS] SOCKS5 listener started on {socks_host}:{socks_port} (локальный демон готов к приёму соединений)", flush=True)

        # 2. Проверка реального outbound-соединения в интернет через VLESS
        outbound_ok, ext_info = verify_outbound_connectivity(socks_host, socks_port, timeout=5.0)
        if outbound_ok:
            proxy_url = f"socks5://{socks_host}:{socks_port}"
            config.YOUTUBE_PROXY = proxy_url
            _active_process = proc
            print(f"[VLESS] ✅ VLESS outbound connection established! Внешний выходной IP: {ext_info} (сервер: {server_info})", flush=True)
            return proc
        else:
            print(f"[VLESS] ⚠️ VLESS outbound connection NOT established ({ext_info}). Удалённый сервер {server_info} не отвечает.", flush=True)
            print("[VLESS] Отключение неработающего прокси во избежание сбоев скачивания.", flush=True)
            stop_vless_proxy(proc)
            return None

    except Exception as e:
        print(f"[VLESS] ❌ Ошибка инициализации VLESS: {e}", flush=True)
        return None


def stop_vless_proxy(proc: Optional[subprocess.Popen] = None) -> None:
    """Корректно останавливает процесс sing-box и удаляет временный конфиг."""
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
