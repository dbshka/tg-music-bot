"""
Unit tests for services/vless_proxy.py:
Tests parsing of VLESS links (Reality, TLS, WS, gRPC), subscription decoding,
skipping of dummy announcement nodes (0.0.0.0/1), sing-box config generation,
outbound connectivity separation, and proxy lifecycle management.
"""
import base64
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from services.vless_proxy import (
    is_valid_remote_server,
    parse_vless_url,
    get_sanitized_outbound_summary,
    resolve_subscription_if_needed,
    build_singbox_config,
    start_vless_proxy,
    stop_vless_proxy,
    ensure_singbox_binary
)
import config


def test_is_valid_remote_server():
    assert is_valid_remote_server("0.0.0.0", 1) is False
    assert is_valid_remote_server("0.0.0.0", 443) is False
    assert is_valid_remote_server("127.0.0.1", 10808) is False
    assert is_valid_remote_server("localhost", 8080) is False
    assert is_valid_remote_server("::1", 443) is False
    assert is_valid_remote_server("vpn.example.com", 0) is False
    assert is_valid_remote_server("vpn.example.com", 1) is False
    assert is_valid_remote_server("vpn.example.com", 443) is True
    assert is_valid_remote_server("185.220.101.5", 8443) is True


def test_parse_vless_rejects_dummy_nodes():
    url_zero = "vless://info@0.0.0.0:1?security=none#InfoNode"
    with pytest.raises(ValueError, match="Недопустимый удалённый сервер VLESS"):
        parse_vless_url(url_zero)

    url_local = "vless://test@127.0.0.1:0#LocalNode"
    with pytest.raises(ValueError, match="Недопустимый удалённый сервер VLESS"):
        parse_vless_url(url_local)


def test_parse_vless_reality():
    url = (
        "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443"
        "?security=reality&sni=yahoo.com&fp=chrome&pbk=publicKey123&sid=ab12"
        "&type=tcp&flow=xtls-rprx-vision#MyHappVpn"
    )
    outbound = parse_vless_url(url)
    assert outbound["type"] == "vless"
    assert outbound["tag"] == "vless-out"
    assert outbound["server"] == "vpn.example.com"
    assert outbound["server_port"] == 443
    assert outbound["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["tls"]["enabled"] is True
    assert outbound["tls"]["server_name"] == "yahoo.com"
    assert outbound["tls"]["utls"]["fingerprint"] == "chrome"
    assert outbound["tls"]["reality"]["public_key"] == "publicKey123"
    assert outbound["tls"]["reality"]["short_id"] == "ab12"

    summary = get_sanitized_outbound_summary(outbound)
    assert summary["server"] == "vpn.example.com"
    assert summary["server_port"] == 443
    assert summary["security"] == "reality"
    assert summary["reality_enabled"] is True
    assert summary["flow"] == "xtls-rprx-vision"


def test_parse_vless_websocket_tls():
    url = (
        "vless://abcdef12-3456-7890-abcd-ef1234567890@ws.example.com:8443"
        "?security=tls&sni=cdn.example.com&type=ws&path=%2Fcustom-path&host=cdn.example.com#WSNode"
    )
    outbound = parse_vless_url(url)
    assert outbound["server"] == "ws.example.com"
    assert outbound["server_port"] == 8443
    assert outbound["transport"]["type"] == "ws"
    assert outbound["transport"]["path"] == "/custom-path"
    assert outbound["transport"]["headers"]["Host"] == "cdn.example.com"
    assert outbound["tls"]["server_name"] == "cdn.example.com"


def test_parse_vless_grpc():
    url = (
        "vless://12345678-abcd-1234-abcd-1234567890ab@grpc.example.com:443"
        "?security=reality&sni=google.com&pbk=grpcKey&type=grpc&serviceName=myGrpcService#gRPCNode"
    )
    outbound = parse_vless_url(url)
    assert outbound["transport"]["type"] == "grpc"
    assert outbound["transport"]["service_name"] == "myGrpcService"
    assert outbound["tls"]["reality"]["public_key"] == "grpcKey"


def test_resolve_subscription_skips_dummy_announcement_nodes():
    """
    Subscribers often start with dummy informational nodes (e.g. 0.0.0.0:1 with account validity remarks).
    The parser must skip these dummy nodes and pick the first valid remote server node.
    """
    mock_subscription_raw = (
        "vless://info@0.0.0.0:1?security=none#📅 Осталось дней: 30\n"
        "vless://traffic@127.0.0.1:0?security=none#📊 Трафик: 100 GB\n"
        "vless://12345678-1234-1234-1234-1234567890ab@nl1.real-vpn.net:443?security=reality&sni=test.com&pbk=pk1#NL1-Server\n"
        "vless://another@de2.real-vpn.net:443?security=tls#DE2-Server\n"
    )
    b64_content = base64.b64encode(mock_subscription_raw.encode("utf-8")).decode("utf-8")

    class FakeResponse:
        def read(self):
            return b64_content.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        link, outbound = resolve_subscription_if_needed("https://vpn-provider.com/sub/token123")
        assert outbound["server"] == "nl1.real-vpn.net"
        assert outbound["server_port"] == 443
        assert outbound["server"] != "0.0.0.0"


def test_resolve_subscription_all_dummies_raises():
    """When a subscription contains only dummy nodes (0.0.0.0:1), it raises an explicit error."""
    mock_subscription_raw = (
        "vless://info@0.0.0.0:1?security=none#Осталось дней: 0\n"
        "vless://traffic@127.0.0.1:1?security=none#Трафик исчерпан\n"
    )
    b64_content = base64.b64encode(mock_subscription_raw.encode("utf-8")).decode("utf-8")

    class FakeResponse:
        def read(self):
            return b64_content.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(ValueError, match="В подписке не найдено ни одного рабочего VLESS сервера"):
            resolve_subscription_if_needed("https://vpn-provider.com/sub/expired")


def test_resolve_subscription_direct_vless():
    direct = "vless://uuid-1234@valid-remote.com:443?security=reality&pbk=pk1#Test"
    link, outbound = resolve_subscription_if_needed(direct)
    assert link == direct
    assert outbound["server"] == "valid-remote.com"


def test_build_singbox_config():
    outbound = {
        "type": "vless",
        "tag": "vless-out",
        "server": "1.2.3.4",
        "server_port": 443,
        "uuid": "test-uuid"
    }
    cfg = build_singbox_config(outbound, socks_host="127.0.0.1", socks_port=10808)
    assert cfg["inbounds"][0]["type"] == "socks"
    assert cfg["inbounds"][0]["listen"] == "127.0.0.1"
    assert cfg["inbounds"][0]["listen_port"] == 10808
    assert cfg["outbounds"][0] == outbound


def test_start_vless_proxy_noop_when_no_env():
    with patch("services.vless_proxy.config.VLESS_URL", None), \
         patch.dict("os.environ", {"VLESS_URL": "", "YOUTUBE_VLESS_URL": ""}):
        proc = start_vless_proxy()
        assert proc is None


def test_start_vless_proxy_lifecycle(tmp_path):
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    vless_link = "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443?security=reality&sni=yahoo.com&pbk=key123#Vpn"

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("services.vless_proxy.verify_outbound_connectivity", return_value=(True, "185.220.101.5")), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        proc = start_vless_proxy(vless_input=vless_link, socks_host="127.0.0.1", socks_port=10808)
        assert proc is mock_proc
        assert config.YOUTUBE_PROXY == "socks5://127.0.0.1:10808"

        stop_vless_proxy(proc)
        mock_proc.terminate.assert_called_once()


def test_outbound_connectivity_failure_stops_proxy_and_leaves_clean(tmp_path, capsys):
    """
    If SOCKS5 listener started, but remote VLESS server fails outbound verification,
    the proxy must be stopped and NOT enabled in config.YOUTUBE_PROXY.
    """
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    vless_link = "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443?security=reality#Vpn"

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("services.vless_proxy.verify_outbound_connectivity", return_value=(False, "Connection refused")), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        config.YOUTUBE_PROXY = None
        proc = start_vless_proxy(vless_input=vless_link)
        assert proc is None
        assert config.YOUTUBE_PROXY is None
        mock_proc.terminate.assert_called_once()
        captured = capsys.readouterr()
        assert "SOCKS5 listener started" in captured.out
        assert "VLESS outbound connection NOT established" in captured.out


def test_start_vless_proxy_fails_gracefully_when_singbox_crashes(tmp_path, capsys):
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1
    mock_proc.stderr.read.return_value = "FATAL[0000] parse config error"

    vless_link = "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443?security=reality#Vpn"

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        proc = start_vless_proxy(vless_input=vless_link)
        assert proc is None
        captured = capsys.readouterr()
        assert "процесс sing-box завершился преждевременно" in captured.out


def test_sanitized_logs_never_leak_uuid_or_keys(tmp_path, capsys):
    secret_uuid = "99999999-8888-7777-6666-555555555555"
    secret_key = "TopSecretPublicKey12345"
    secret_sid = "SecretShortId99"
    vless_link = f"vless://{secret_uuid}@secret-server.vpn:443?security=reality&pbk={secret_key}&sid={secret_sid}#SecretVpn"

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("services.vless_proxy.verify_outbound_connectivity", return_value=(True, "185.220.101.5")), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        start_vless_proxy(vless_input=vless_link)
        captured = capsys.readouterr()
        assert secret_uuid not in captured.out
        assert secret_key not in captured.out
        assert secret_sid not in captured.out
        assert "secret-server.vpn:443" in captured.out


def test_downloader_dynamic_proxy_after_vless_startup(tmp_path):
    """
    REGRESSION TEST:
    1. Initially config.YOUTUBE_PROXY is None.
    2. services.downloader starts with proxy None (not cached).
    3. start_vless_proxy() starts sing-box and sets config.YOUTUBE_PROXY to socks5://127.0.0.1:10808.
    4. Downloader executes a YouTube operation and picks up the new dynamic proxy:
       cand_dl_opts["proxy"] == "socks5://127.0.0.1:10808".
    5. Initial None is proved not to be cached.
    6. stop_vless_proxy() properly resets proxy to None.
    """
    import services.downloader
    from services.downloader import _sync_download, get_current_youtube_proxy

    # 1. Начальное состояние: прокси не установлен
    config.YOUTUBE_PROXY = None
    services.downloader.YOUTUBE_PROXY = None
    assert get_current_youtube_proxy() is None

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    vless_link = "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443?security=reality&sni=yahoo.com&pbk=key123#Vpn"

    recorded_opts = []

    class CapturingFakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)
            recorded_opts.append(self.opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            if download:
                f = tmp_path / "song.m4a"
                f.write_bytes(b"\x00" * 2000)
                return {"title": "Test Dynamic Song", "duration": 180}
            return {
                "entries": [
                    {
                        "id": "cand_dyn",
                        "title": "Test Dynamic Song",
                        "uploader": "Artist - Topic",
                        "duration": 180,
                        "webpage_url": "https://www.youtube.com/watch?v=cand_dyn",
                        "_source": "youtube"
                    }
                ]
            }

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("services.vless_proxy.verify_outbound_connectivity", return_value=(True, "146.255.189.4")), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):

        # 2. Запуск VLESS прокси
        proc = start_vless_proxy(vless_input=vless_link, socks_host="127.0.0.1", socks_port=10808)
        assert proc is mock_proc
        assert config.YOUTUBE_PROXY == "socks5://127.0.0.1:10808"
        assert get_current_youtube_proxy() == "socks5://127.0.0.1:10808"

        # 3. Выполняем загрузку через downloader
        with patch("yt_dlp.YoutubeDL", side_effect=CapturingFakeYDL), \
             patch("services.downloader._apply_custom_metadata", return_value=None):
            res = _sync_download(
                query_or_url="ytsearch5:Artist Test Dynamic Song",
                output_dir=tmp_path,
                custom_title="Test Dynamic Song",
                custom_artist="Artist",
                expected_duration=180,
                is_text_input=True
            )
            assert res is not None

        # 4. Проверяем cand_dl_opts
        dl_calls = [opt for opt in recorded_opts if opt.get("extract_flat") is False]
        assert len(dl_calls) >= 1
        cand_dl_opts = dl_calls[0]
        assert cand_dl_opts.get("proxy") == "socks5://127.0.0.1:10808"

        # 5. Остановка прокси сбрасывает настройки
        stop_vless_proxy(proc)
        assert config.YOUTUBE_PROXY is None
        assert get_current_youtube_proxy() is None
