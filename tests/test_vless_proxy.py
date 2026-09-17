"""
Unit tests for services/vless_proxy.py:
Tests parsing of VLESS links (Reality, TLS, WS, gRPC), subscription decoding,
sing-box config generation, and proxy lifecycle management.
"""
import base64
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from services.vless_proxy import (
    parse_vless_url,
    resolve_subscription_if_needed,
    build_singbox_config,
    start_vless_proxy,
    stop_vless_proxy,
    ensure_singbox_binary
)
import config


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


def test_resolve_subscription_base64():
    mock_subscription_raw = (
        "vmess://dummy1\n"
        "vless://12345678-1234-1234-1234-1234567890ab@server1.com:443?security=reality&sni=test.com#Node1\n"
        "vless://another@server2.com:443#Node2\n"
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
        resolved = resolve_subscription_if_needed("https://vpn-provider.com/sub/token123")
        assert resolved.startswith("vless://12345678-1234-1234-1234-1234567890ab@server1.com:443")


def test_resolve_subscription_direct_vless():
    direct = "vless://uuid@host:443?security=reality#Test"
    assert resolve_subscription_if_needed(direct) == direct


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
         patch.dict("os.environ", {}, clear=False):
        proc = start_vless_proxy()
        assert proc is None


def test_start_vless_proxy_lifecycle(tmp_path):
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    vless_link = "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443?security=reality&sni=yahoo.com&pbk=key123#Vpn"

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        proc = start_vless_proxy(vless_input=vless_link, socks_host="127.0.0.1", socks_port=10808)
        assert proc is mock_proc
        assert config.YOUTUBE_PROXY == "socks5://127.0.0.1:10808"

        stop_vless_proxy(proc)
        mock_proc.terminate.assert_called_once()


def test_subscription_skips_malformed_node_and_picks_valid():
    """If the first node in subscription is malformed, parser skips it and picks next valid node."""
    mock_sub = (
        "vless://broken-url-without-host\n"
        "vless://valid-uuid-1234@good-server.com:443?security=reality&pbk=pk1#ValidNode\n"
    )
    b64_content = base64.b64encode(mock_sub.encode("utf-8")).decode("utf-8")

    class FakeResponse:
        def read(self):
            return b64_content.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        resolved = resolve_subscription_if_needed("https://vpn.com/sub")
        assert "good-server.com" in resolved
        outbound = parse_vless_url(resolved)
        assert outbound["server"] == "good-server.com"


def test_subscription_empty_or_broken_raises():
    """Empty or non-vless subscription raises ValueError safely."""
    b64_empty = base64.b64encode(b"ss://some-shadowsocks-node\n").decode("utf-8")

    class FakeResponse:
        def read(self):
            return b64_empty.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(ValueError, match="Не удалось найти валидную vless://"):
            resolve_subscription_if_needed("https://vpn.com/empty-sub")


def test_start_vless_proxy_fails_gracefully_when_binary_missing(capsys):
    """When sing-box binary cannot be found/installed, returns None and does not hang."""
    with patch("services.vless_proxy.ensure_singbox_binary", return_value=None):
        proc = start_vless_proxy(vless_input="vless://u@h:443#t")
        assert proc is None
        captured = capsys.readouterr()
        assert "Бинарник sing-box не найден" in captured.out


def test_start_vless_proxy_fails_gracefully_when_singbox_crashes(tmp_path, capsys):
    """When sing-box process exits immediately with error, returns None and does not hang."""
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
        assert "sing-box завершился преждевременно" in captured.out


def test_start_vless_proxy_fails_gracefully_when_port_times_out(tmp_path, capsys):
    """When sing-box is running but port never accepts connections, terminates cleanly and returns None."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    vless_link = "vless://11111111-2222-3333-4444-555555555555@vpn.example.com:443?security=reality#Vpn"

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=False), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        proc = start_vless_proxy(vless_input=vless_link, timeout_secs=0.3)
        assert proc is None
        mock_proc.terminate.assert_called_once()
        captured = capsys.readouterr()
        assert "sing-box не ответил" in captured.out


def test_sanitized_logs_never_leak_uuid_or_keys(tmp_path, capsys):
    """Console output must never leak UUID, public key, or private parameters."""
    secret_uuid = "99999999-8888-7777-6666-555555555555"
    secret_key = "TopSecretPublicKey12345"
    secret_sid = "SecretShortId99"
    vless_link = f"vless://{secret_uuid}@secret-server.vpn:443?security=reality&pbk={secret_key}&sid={secret_sid}#SecretVpn"

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("services.vless_proxy.ensure_singbox_binary", return_value=Path("/usr/local/bin/sing-box")), \
         patch("services.vless_proxy.is_port_open", return_value=True), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch("services.vless_proxy.BASE_DIR", tmp_path):
        
        start_vless_proxy(vless_input=vless_link)
        captured = capsys.readouterr()
        # Assert secrets are NOT in stdout
        assert secret_uuid not in captured.out
        assert secret_key not in captured.out
        assert secret_sid not in captured.out
        # Assert sanitized server is present
        assert "secret-server.vpn:443" in captured.out
