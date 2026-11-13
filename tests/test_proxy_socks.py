"""SOCKS5 协议离线回归测试，不使用真实代理、行情或数据库。"""
import socket
import ssl
import threading
import unittest
from unittest.mock import patch

from core import proxy


class TestSocks5(unittest.TestCase):
    def test_end_to_end_remote_dns(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        self.addCleanup(listener.close)
        seen, errors = [], []

        def server():
            try:
                with listener.accept()[0] as c:
                    c.settimeout(3)
                    seen.append(proxy._recv(c, 3))
                    c.sendall(b"\x05\x00")
                    seen.append(proxy._recv(c, 4))
                    seen.append(proxy._recv(c, proxy._recv(c, 1)[0]))
                    seen.append(proxy._recv(c, 2))
                    c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += proxy._recv(c, 1)
                    seen.append(request)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=server, daemon=True)
        t.start()
        with patch.object(proxy, "conf", return_value={"protocol": "socks5"}):
            text = proxy._open_via("127.0.0.1:%d" % listener.getsockname()[1],
                                   "http://fixture.invalid/quote?a=1", {}, 3, "utf-8")
        t.join(4)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(text, "ok")
        self.assertEqual(seen[:4], [b"\x05\x01\x00", b"\x05\x01\x00\x03", b"fixture.invalid", b"\x00\x50"])
        self.assertIn(b"GET /quote?a=1 HTTP/1.1", seen[4])

    def test_authentication_and_fragmented_reply(self):
        from unittest.mock import Mock
        s = Mock()
        reply = b"\x05\x02\x01\x00\x05\x00\x00\x03\x01x\x00\x00"
        s.recv.side_effect = [bytes((x,)) for x in reply]
        with patch.object(proxy.socket, "create_connection", return_value=s):
            self.assertIs(proxy._socks_connect("u:p%40ss@127.0.0.1:1080", "example.com", 443, 2), s)
        self.assertEqual(s.sendall.call_args_list[1].args[0], b"\x01\x01u\x04p@ss")
        s.close.assert_not_called()

    def test_auth_failure_closes_socket(self):
        from unittest.mock import Mock
        s = Mock()
        s.recv.side_effect = [b"\x05\x02", b"\x01\x01"]
        with patch.object(proxy.socket, "create_connection", return_value=s):
            with self.assertRaisesRegex(OSError, "认证失败"):
                proxy._socks_connect("u:p@127.0.0.1:1080", "example.com", 443, 2)
        s.close.assert_called_once()

    def test_https_certificate_failure_not_bypassed(self):
        from unittest.mock import Mock
        s, context = Mock(), Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("fixture bad certificate")
        with patch.object(proxy, "_socks_connect", return_value=s), patch.object(
                proxy.http.client.ssl, "_create_default_https_context", return_value=context):
            with self.assertRaises(Exception) as err:
                proxy._socks_open("127.0.0.1:1080", "https://fixture.invalid/", {}, 2, "utf-8")
        self.assertIn("fixture bad certificate", str(err.exception))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "fixture.invalid")
        s.close.assert_called()

    def test_fixed_http_overrides_pool_protocol(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(proxy, "conf", return_value={"protocol": "socks5", "api_url": "http://host:8080"}), patch.object(
                proxy.urllib.request, "build_opener", return_value=opener), patch.object(proxy, "_socks_open") as socks:
            self.assertEqual(proxy._open_via("host:8080", "https://example.com/", {}, 2, "utf-8"), "ok")
        socks.assert_not_called()
