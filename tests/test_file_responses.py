import base64
import json
import re
import socket
import tempfile
import threading
import unittest
from email.parser import BytesParser
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlencode

import server


class IframeSources(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.sources = []
        self.feed(body.decode("utf-8"))

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            self.sources.append(dict(attrs)["src"])


class FileResponseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fileweb-responses-")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.globals = patch.multiple(
            server,
            ROOT=base / "data",
            PROFILE_DIR=base / "profile",
            SHARE_FILE=base / "shares.json",
            SESSION_FILE=base / "sessions.json",
            PASSWORD_FILE=base / "login-password",
            USER="test-user",
            PASSWORD="test-password",
            SESSIONS={},
            SHARES={},
        )
        self.globals.start()
        self.addCleanup(self.globals.stop)
        server.ensure_root()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        credentials = base64.b64encode(b"test-user:test-password").decode("ascii")
        self.auth = {"Authorization": f"Basic {credentials}"}
        self.relative = (
            "OS版本发布构建方案调研与改造/Koji与OBS_OS构建发布流程.html"
        )
        self.content = (
            '<!doctype html><meta charset="utf-8"><title>构建发布流程</title>'
            '<h1>Koji 与 OBS</h1><script>document.body.dataset.ready="yes"</script>'
        ).encode("utf-8")
        path = server.ROOT / self.relative
        path.parent.mkdir(parents=True)
        path.write_bytes(self.content)

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, method="GET", headers=None, payload=None):
        # Capture the wire response: permissive HTTP clients can misread a
        # second status line as a header after a partially buffered 200.
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        request_headers = {
            "Host": f"127.0.0.1:{self.httpd.server_port}",
            "Connection": "close",
            **(headers or {}),
        }
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        lines = [f"{method} {path} HTTP/1.1"]
        lines.extend(f"{name}: {value}" for name, value in request_headers.items())
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        with socket.create_connection(self.httpd.server_address, timeout=3) as connection:
            connection.sendall(request)
            chunks = []
            while chunk := connection.recv(65536):
                chunks.append(chunk)
        wire = b"".join(chunks)
        self.assertIn(b"\r\n\r\n", wire, "HTTP headers were not completed")
        head, response_body = wire.split(b"\r\n\r\n", 1)
        status_lines = re.findall(rb"(?m)^HTTP/1\.[01] [0-9]{3}[^\r\n]*", head)
        self.assertEqual(len(status_lines), 1, status_lines)
        status_line, header_lines = head.split(b"\r\n", 1)
        status = int(status_line.split()[1])
        response_headers = BytesParser().parsebytes(header_lines + b"\r\n\r\n")
        self.assertFalse(response_headers.defects, response_headers.defects)
        self.assertEqual(int(response_headers["Content-Length"]), len(response_body))
        return status, response_headers, response_body

    def assert_file_response(self, path, *, name, disposition, headers=None, controls=False):
        status, response_headers, body = self.request(path, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.content)
        values = response_headers.get_all("Content-Disposition", [])
        self.assertEqual(len(values), 1)
        value = values[0]
        self.assertTrue(value.isascii(), value)
        self.assertTrue(all(32 <= ord(character) <= 126 for character in value), value)
        self.assertTrue(value.startswith(disposition + ";"), value)
        self.assertIsNone(response_headers.get("X-Injected"))
        self.assertRegex(value, r'filename="(?:[^"\\]|\\["\\])*"(?:;|$)')
        encoded = re.search(r"(?:^|;)\s*filename\*=UTF-8''([^;]+)", value, re.I)
        self.assertIsNotNone(encoded, value)
        if not controls:
            self.assertEqual(unquote(encoded.group(1), encoding="utf-8", errors="strict"), name)

    def test_unicode_preview_iframe_loads_default_and_explicit_modes(self):
        for mode in (None, "static", "interactive"):
            with self.subTest(mode=mode):
                query = {"path": self.relative}
                if mode is not None:
                    query["mode"] = mode
                status, _, body = self.request("/preview?" + urlencode(query), headers=self.auth)
                self.assertEqual(status, 200)
                sources = IframeSources(body).sources
                self.assertEqual(len(sources), 1)
                self.assert_file_response(
                    sources[0], name=Path(self.relative).name,
                    disposition="inline", headers=self.auth,
                )

    def test_unicode_download_preserves_file_bytes_and_filename(self):
        self.assert_file_response(
            "/api/download?" + urlencode({"path": self.relative}),
            name=Path(self.relative).name, disposition="attachment", headers=self.auth,
        )

    def test_anonymous_unicode_share_loads_default_and_explicit_modes(self):
        mutation_headers = {
            **self.auth,
            "Cookie": f"fileweb_csrf={server.BASIC_CSRF_TOKEN}",
            "X-CSRF-Token": server.BASIC_CSRF_TOKEN,
        }
        status, _, body = self.request(
            "/api/share", method="POST", headers=mutation_headers,
            payload={"path": self.relative},
        )
        self.assertEqual(status, 201)
        share = "/share/" + json.loads(body)["token"]
        for mode in (None, "static", "interactive"):
            with self.subTest(mode=mode):
                query = "" if mode is None else "?" + urlencode({"mode": mode})
                status, _, body = self.request(share + query)
                self.assertEqual(status, 200)
                sources = IframeSources(body).sources
                self.assertEqual(len(sources), 1)
                self.assert_file_response(
                    sources[0], name=Path(self.relative).name, disposition="inline",
                )

    def test_ascii_and_quoted_names_cannot_inject_response_headers(self):
        for name in (
            "report.html",
            'report "quoted"; 100% done.html',
            'report"\r\nX-Injected: yes\t\x7f.html',
        ):
            with self.subTest(name=name):
                (server.ROOT / name).write_bytes(self.content)
                self.assert_file_response(
                    "/api/download?" + urlencode({"path": name}),
                    name=name, disposition="attachment", headers=self.auth,
                    controls=any(ord(character) < 32 or ord(character) == 127 for character in name),
                )


if __name__ == "__main__":
    unittest.main()
