import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import server


class PreviewMarkup(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements = []
        self.feed(content.decode("utf-8"))

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def attributes(self, tag):
        return [attrs for element, attrs in self.elements if element == tag]


class AuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fileweb-auth-")
        base = Path(self.temporary.name)
        self.originals = {
            "ROOT": server.ROOT,
            "PROFILE_DIR": server.PROFILE_DIR,
            "SHARE_FILE": server.SHARE_FILE,
            "SESSION_FILE": server.SESSION_FILE,
            "PASSWORD_FILE": server.PASSWORD_FILE,
            "USER": server.USER,
            "PASSWORD": server.PASSWORD,
        }
        server.ROOT = base / "data"
        server.PROFILE_DIR = base / "profile"
        server.SHARE_FILE = base / "shares.json"
        server.SESSION_FILE = base / "sessions.json"
        server.PASSWORD_FILE = base / "login-password"
        server.PASSWORD_FILE.write_text("test-password\n", encoding="utf-8")
        server.USER = "admin"
        server.PASSWORD = "test-password"
        with server.SESSIONS_LOCK:
            server.SESSIONS.clear()
        server.ensure_root()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        with server.SESSIONS_LOCK:
            server.SESSIONS.clear()
        for name, value in self.originals.items():
            setattr(server, name, value)
        self.temporary.cleanup()

    def request(self, method, path, *, payload=None, headers=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        result = response.status, response.getheaders(), content
        connection.close()
        return result

    @staticmethod
    def cookies(headers):
        cookies = SimpleCookie()
        for name, value in headers:
            if name.lower() == "set-cookie":
                cookies.load(value)
        return cookies

    @staticmethod
    def cookie_header(cookies):
        return "; ".join(f"{name}={morsel.value}" for name, morsel in cookies.items())

    def test_remembered_login_survives_reload_until_account_switch(self):
        status, headers, body = self.request(
            "POST",
            "/api/login",
            payload={"username": "admin", "password": "test-password", "remember": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["remembered"])

        cookies = self.cookies(headers)
        session_token = cookies["fileweb_session"].value
        self.assertEqual(cookies["fileweb_session"]["max-age"], str(server.REMEMBER_SESSION_SECONDS))
        self.assertEqual(cookies["fileweb_csrf"]["max-age"], str(server.REMEMBER_SESSION_SECONDS))

        stored = server.SESSION_FILE.read_text(encoding="utf-8")
        self.assertNotIn(session_token, stored)
        self.assertIn(server.session_id(session_token), stored)

        with server.SESSIONS_LOCK:
            server.SESSIONS.clear()
        server.load_sessions()
        cookie_header = self.cookie_header(cookies)

        status, _, _ = self.request("GET", "/api/me", headers={"Cookie": cookie_header})
        self.assertEqual(status, 200)
        status, headers, _ = self.request("GET", "/login.html", headers={"Cookie": cookie_header})
        self.assertEqual(status, 302)
        self.assertIn(("Location", "/"), headers)

        status, headers, _ = self.request(
            "POST",
            "/api/logout",
            payload={},
            headers={
                "Cookie": cookie_header,
                "X-CSRF-Token": cookies["fileweb_csrf"].value,
            },
        )
        self.assertEqual(status, 200)
        cleared = [value for name, value in headers if name.lower() == "set-cookie"]
        self.assertEqual(sum("Max-Age=0" in value for value in cleared), 2)

        with server.SESSIONS_LOCK:
            server.SESSIONS.clear()
        server.load_sessions()
        status, _, _ = self.request("GET", "/api/me", headers={"Cookie": cookie_header})
        self.assertEqual(status, 401)

    def test_short_login_is_not_persisted(self):
        status, headers, body = self.request(
            "POST",
            "/api/login",
            payload={"username": "admin", "password": "test-password", "remember": False},
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["remembered"])

        cookies = self.cookies(headers)
        self.assertEqual(cookies["fileweb_session"]["max-age"], "")
        self.assertEqual(cookies["fileweb_csrf"]["max-age"], "")

        with server.SESSIONS_LOCK:
            server.SESSIONS.clear()
        server.load_sessions()
        status, _, _ = self.request(
            "GET",
            "/api/me",
            headers={"Cookie": self.cookie_header(cookies)},
        )
        self.assertEqual(status, 401)

    def test_basic_auth_can_still_obtain_csrf_for_mutations(self):
        credentials = base64.b64encode(b"admin:test-password").decode("ascii")
        authorization = {"Authorization": f"Basic {credentials}"}
        status, headers, _ = self.request("GET", "/", headers=authorization)
        self.assertEqual(status, 200)
        cookies = self.cookies(headers)
        csrf = cookies["fileweb_csrf"].value

        status, _, _ = self.request(
            "POST",
            "/api/mkdir",
            payload={"path": "", "name": "from-basic-auth"},
            headers={
                **authorization,
                "Cookie": self.cookie_header(cookies),
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue((server.ROOT / "from-basic-auth").is_dir())

    def test_password_change_accepts_passwords_shorter_than_ten_characters(self):
        status, headers, _ = self.request(
            "POST",
            "/api/login",
            payload={"username": "admin", "password": "test-password", "remember": False},
        )
        self.assertEqual(status, 200)
        cookies = self.cookies(headers)
        authenticated_headers = {
            "Cookie": self.cookie_header(cookies),
            "X-CSRF-Token": cookies["fileweb_csrf"].value,
        }

        status, _, _ = self.request(
            "POST",
            "/api/password",
            payload={"current_password": "test-password", "new_password": ""},
            headers=authenticated_headers,
        )
        self.assertEqual(status, 400)

        status, _, _ = self.request(
            "POST",
            "/api/password",
            payload={"current_password": "test-password", "new_password": "short123"},
            headers=authenticated_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(server.PASSWORD, "short123")
        self.assertEqual(server.PASSWORD_FILE.read_text(encoding="utf-8"), "short123\n")

    def login_headers(self):
        status, headers, _ = self.request(
            "POST",
            "/api/login",
            payload={"username": "admin", "password": "test-password"},
        )
        self.assertEqual(status, 200)
        cookies = self.cookies(headers)
        return {
            "Cookie": self.cookie_header(cookies),
            "X-CSRF-Token": cookies["fileweb_csrf"].value,
        }

    def html_fixture(self):
        relative = "html/Rocky Linux/report & notes.html"
        path = server.ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            b"<!doctype html><button onclick=\"document.body.dataset.clicked='yes'\">"
            b"Run</button><script>document.body.dataset.ready='yes'</script>"
        )
        path.write_bytes(content)
        return relative, content

    def csp_directives(self, headers):
        policies = [value for name, value in headers if name.lower() == "content-security-policy"]
        self.assertEqual(len(policies), 1)
        directives = {}
        for part in policies[0].split(";"):
            words = part.split()
            if words:
                self.assertNotIn(words[0], directives)
                directives[words[0]] = set(words[1:])
        return directives

    def assert_html_policy(self, headers, *, interactive):
        policy = self.csp_directives(headers)
        self.assertIn("sandbox", policy)
        self.assertEqual(
            policy["sandbox"],
            {"allow-scripts", "allow-downloads", "allow-modals", "allow-popups"}
            if interactive else set()
        )
        self.assertEqual(policy["default-src"], {"'none'"})
        self.assertEqual(
            policy.get("script-src", policy["default-src"]),
            {"'unsafe-inline'"} if interactive else {"'none'"},
        )
        if interactive:
            for directive in (
                "connect-src", "frame-src", "child-src", "worker-src", "object-src",
                "base-uri", "form-action",
            ):
                self.assertEqual(policy[directive], {"'none'"}, directive)
            self.assertEqual(policy["img-src"], {"data:", "blob:"})
        return policy

    def mode_link(self, markup, route, mode):
        links = []
        for attributes in markup.attributes("a"):
            href = attributes.get("href", "")
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            if parsed.path == route and query.get("mode") == [mode]:
                links.append(href)
        self.assertEqual(len(links), 1, (route, mode, links))
        return links[0]

    def test_html_preview_defaults_to_interactive_and_switches_modes(self):
        relative, content = self.html_fixture()
        headers = self.login_headers()
        for mode in (None, "interactive", "static"):
            with self.subTest(mode=mode):
                query = {"path": relative}
                if mode is not None:
                    query["mode"] = mode
                preview_url = "/preview?" + urlencode(query)
                interactive = mode in (None, "interactive")
                status, response_headers, body = self.request("GET", preview_url, headers=headers)
                self.assertEqual(status, 200)
                markup = PreviewMarkup(body)
                frames = markup.attributes("iframe")
                self.assertEqual(len(frames), 1)
                self.assertIn("sandbox", frames[0])
                self.assertEqual(
                    set(frames[0]["sandbox"].split()),
                    {"allow-scripts", "allow-downloads", "allow-modals", "allow-popups"}
                    if interactive else set(),
                )
                outer_policy = self.csp_directives(response_headers)
                self.assertEqual(outer_policy["frame-src"], {"'self'"})
                raw_url = frames[0]["src"]
                self.assertEqual(urlsplit(raw_url).path, "/api/raw")
                raw_query = parse_qs(urlsplit(raw_url).query)
                self.assertEqual(raw_query["path"], [relative])
                self.assertEqual(raw_query.get("mode"),
                                 ["interactive" if interactive else "static"])

                status, raw_headers, raw_body = self.request("GET", raw_url, headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(raw_body, content)
                raw_policy = self.assert_html_policy(raw_headers, interactive=interactive)
                self.assertEqual(raw_policy["sandbox"], set(frames[0]["sandbox"].split()))

                switched_url = self.mode_link(
                    markup, "/preview", "static" if interactive else "interactive"
                )
                self.assertEqual(parse_qs(urlsplit(switched_url).query)["path"], [relative])
                status, _, switched_body = self.request("GET", switched_url, headers=headers)
                self.assertEqual(status, 200)
                switched_frame = PreviewMarkup(switched_body).attributes("iframe")[0]
                status, switched_headers, switched_content = self.request(
                    "GET", switched_frame["src"], headers=headers
                )
                self.assertEqual(status, 200)
                self.assertEqual(switched_content, content)
                switched_policy = self.assert_html_policy(
                    switched_headers, interactive=not interactive
                )
                self.assertEqual(switched_policy["sandbox"], set(switched_frame["sandbox"].split()))

    def test_direct_html_raw_responses_enforce_the_selected_sandbox(self):
        relative, content = self.html_fixture()
        headers = self.login_headers()
        for mode in (None, "static", "interactive"):
            with self.subTest(mode=mode):
                query = {"path": relative}
                if mode is not None:
                    query["mode"] = mode
                status, raw_headers, body = self.request(
                    "GET", "/api/raw?" + urlencode(query), headers=headers
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, content)
                self.assert_html_policy(raw_headers, interactive=(mode in (None, "interactive")))
                response_headers = {name.lower(): value for name, value in raw_headers}
                self.assertEqual(response_headers["x-content-type-options"], "nosniff")
                self.assertEqual(response_headers["cache-control"], "no-store")
                self.assertTrue(response_headers["content-type"].startswith("text/html"))

    def test_unknown_preview_modes_do_not_enable_scripts(self):
        relative, _ = self.html_fixture()
        headers = self.login_headers()
        for mode in (
            "", "unknown", "INTERACTIVE", "interactive allow-same-origin",
            ["interactive", "static"], ["interactive", "interactive"],
        ):
            query = urlencode({"path": relative, "mode": mode}, doseq=True)
            with self.subTest(mode=mode):
                status, _, body = self.request("GET", "/preview?" + query, headers=headers)
                self.assertEqual(status, 200)
                frames = PreviewMarkup(body).attributes("iframe")
                self.assertEqual(frames[0]["sandbox"], "")
                for raw_url in (frames[0]["src"], "/api/raw?" + query):
                    status, raw_headers, _ = self.request("GET", raw_url, headers=headers)
                    self.assertEqual(status, 200)
                    self.assert_html_policy(raw_headers, interactive=False)

    def test_html_preview_metadata_preserves_default_and_selected_modes(self):
        relative, content = self.html_fixture()
        headers = self.login_headers()
        for mode in (
            None, "interactive", "static", "", "unknown", "INTERACTIVE",
            ["interactive", "static"], ["interactive", "interactive"],
        ):
            with self.subTest(mode=mode):
                query = {"path": relative}
                if mode is not None:
                    query["mode"] = mode
                status, _, body = self.request(
                    "GET", "/api/preview?" + urlencode(query, doseq=True), headers=headers
                )
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["kind"], "html")
                self.assertEqual(payload["path"], relative)
                raw_query = parse_qs(urlsplit(payload["url"]).query)
                interactive = mode in (None, "interactive")
                self.assertEqual(raw_query["path"], [relative])
                self.assertEqual(raw_query.get("mode"),
                                 ["interactive" if interactive else "static"])
                status, raw_headers, raw_content = self.request(
                    "GET", payload["url"], headers=headers
                )
                self.assertEqual(status, 200)
                self.assertEqual(raw_content, content)
                self.assert_html_policy(raw_headers, interactive=interactive)

    def test_html_preview_modes_do_not_bypass_authentication(self):
        relative, content = self.html_fixture()
        for mode in (None, "static", "interactive", "unknown"):
            query = {"path": relative}
            if mode is not None:
                query["mode"] = mode
            for route in ("/preview", "/api/raw", "/api/preview"):
                with self.subTest(mode=mode, route=route):
                    status, _, body = self.request("GET", route + "?" + urlencode(query))
                    self.assertIn(status, (302, 401))
                    self.assertNotIn(content, body)

    def test_public_html_share_modes_are_bound_to_the_shared_file(self):
        relative, content = self.html_fixture()
        (server.ROOT / "private.html").write_text("PRIVATE FILE CONTENT", encoding="utf-8")
        status, _, body = self.request(
            "POST", "/api/share", payload={"path": relative}, headers=self.login_headers()
        )
        self.assertEqual(status, 201)
        route = "/share/" + json.loads(body)["token"]

        for mode in (
            None, "interactive", "static", "", "unknown", "INTERACTIVE",
            ["interactive", "static"], ["interactive", "interactive"],
        ):
            with self.subTest(mode=mode):
                query = {"path": "private.html"}
                if mode is not None:
                    query["mode"] = mode
                status, _, body = self.request("GET", route + "?" + urlencode(query, doseq=True))
                self.assertEqual(status, 200)
                markup = PreviewMarkup(body)
                frames = markup.attributes("iframe")
                self.assertEqual(len(frames), 1)
                interactive = mode in (None, "interactive")
                self.assertEqual(set(frames[0]["sandbox"].split()),
                                 {"allow-scripts", "allow-downloads", "allow-modals", "allow-popups"}
                                 if interactive else set())
                self.assertEqual(urlsplit(frames[0]["src"]).path, route + "/raw")
                raw_query = parse_qs(urlsplit(frames[0]["src"]).query)
                self.assertNotIn("path", raw_query)
                self.assertEqual(raw_query.get("mode"),
                                 ["interactive" if interactive else "static"])
                self.mode_link(markup, route, "static" if interactive else "interactive")

                for raw_url in (frames[0]["src"], route + "/raw?" + urlencode(query, doseq=True)):
                    status, raw_headers, raw_body = self.request("GET", raw_url)
                    self.assertEqual(status, 200)
                    self.assertEqual(raw_body, content)
                    self.assertNotIn(b"PRIVATE FILE CONTENT", raw_body)
                    self.assert_html_policy(raw_headers, interactive=interactive)

        for suffix in ("", "/raw"):
            status, _, _ = self.request("GET", "/share/missing-token" + suffix + "?mode=interactive")
            self.assertEqual(status, 404)

    def test_interactive_mode_preserves_image_and_text_previews(self):
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aBF4AAAAASUVORK5CYII="
        )
        (server.ROOT / "image.png").write_bytes(image)
        text = '<script>alert("example")</script> & plain text'
        (server.ROOT / "notes.txt").write_text(text, encoding="utf-8")
        headers = self.login_headers()

        status, _, body = self.request(
            "GET", "/preview?path=image.png&mode=interactive", headers=headers
        )
        self.assertEqual(status, 200)
        markup = PreviewMarkup(body)
        self.assertEqual(markup.attributes("iframe"), [])
        self.assertEqual(len(markup.attributes("img")), 1)
        self.assertFalse(any("mode=" in link.get("href", "") for link in markup.attributes("a")))
        status, raw_headers, body = self.request(
            "GET", "/api/raw?path=image.png&mode=interactive", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, image)
        self.assertFalse(any(name.lower() == "content-security-policy" for name, _ in raw_headers))

        status, _, body = self.request(
            "GET", "/preview?path=notes.txt&mode=interactive", headers=headers
        )
        self.assertEqual(status, 200)
        markup = PreviewMarkup(body)
        self.assertEqual(markup.attributes("iframe"), [])
        self.assertEqual(markup.attributes("script"), [])
        self.assertEqual(len(markup.attributes("pre")), 1)
        self.assertIn("&lt;script&gt;", body.decode("utf-8"))
        self.assertFalse(any("mode=" in link.get("href", "") for link in markup.attributes("a")))
        status, _, body = self.request(
            "GET", "/api/preview?path=notes.txt&mode=interactive", headers=headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["content"], text)


if __name__ == "__main__":
    unittest.main()
