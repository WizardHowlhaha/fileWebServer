#!/usr/bin/env python3
"""Small, dependency-free file management server with a constrained Codex runner."""

from __future__ import annotations

import base64
import cgi
import hashlib
import html
import json
import mimetypes
import os
import secrets
import selectors
import signal
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(os.environ.get("FILEWEB_ROOT", "/srv/fileweb/data")).resolve()
HOST = os.environ.get("FILEWEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("FILEWEB_PORT", "8787"))
PUBLIC_URL = os.environ.get("FILEWEB_PUBLIC_URL", "").rstrip("/")
MAX_UPLOAD = int(os.environ.get("FILEWEB_MAX_UPLOAD", str(100 * 1024 * 1024)))
MAX_PREVIEW = int(os.environ.get("FILEWEB_MAX_PREVIEW", str(4 * 1024 * 1024)))
MAX_HTML_PREVIEW = int(os.environ.get("FILEWEB_MAX_HTML_PREVIEW", str(128 * 1024 * 1024)))
MAX_AVATAR = int(os.environ.get("FILEWEB_MAX_AVATAR", str(2 * 1024 * 1024)))
AGENT_TIMEOUT = int(os.environ.get("FILEWEB_AGENT_TIMEOUT", "120"))
AGENT_MAX_OUTPUT = int(os.environ.get("FILEWEB_AGENT_MAX_OUTPUT", str(80 * 1024)))
AGENT_ALLOW_WRITE = os.environ.get("FILEWEB_AGENT_ALLOW_WRITE", "0").lower() in {"1", "true", "yes"}
CODEX_BIN = os.environ.get("FILEWEB_CODEX_BIN", "/root/.nvm/versions/node/v24.21.0/bin/codex")
AGENT_HOME = Path(os.environ.get("FILEWEB_AGENT_HOME", "/var/lib/fileweb/agent-home"))
PROFILE_DIR = Path(os.environ.get("FILEWEB_PROFILE_DIR", "/var/lib/fileweb/profile"))
SHARE_FILE = Path(os.environ.get("FILEWEB_SHARE_FILE", "/var/lib/fileweb/shares.json"))
SESSION_FILE = Path(os.environ.get("FILEWEB_SESSION_FILE", "/var/lib/fileweb/sessions.json"))
SESSION_SECONDS = 8 * 60 * 60
REMEMBER_SESSION_SECONDS = int(os.environ.get("FILEWEB_REMEMBER_SESSION_SECONDS", str(10 * 365 * 24 * 60 * 60)))
BASIC_CSRF_TOKEN = secrets.token_urlsafe(32)

USER = os.environ.get("FILEWEB_USER", "admin")
PASSWORD_FILE = Path(os.environ.get("FILEWEB_PASSWORD_FILE", "/etc/fileweb/admin-password"))
try:
    PASSWORD = PASSWORD_FILE.read_text(encoding="utf-8").strip()
except OSError:
    PASSWORD = os.environ.get("FILEWEB_PASSWORD", "admin")
if not PASSWORD:
    raise RuntimeError("FILEWEB_PASSWORD or FILEWEB_PASSWORD_FILE must not be empty")

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".log", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".conf", ".xml", ".svg", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".css", ".sh", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".vue",
}
HTML_EXTENSIONS = {".html", ".htm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# Interactive HTML previews run in a unique opaque origin created by the
# sandbox.  Scripts can drive self-contained controls, but the document cannot
# read management cookies, use the management API, or load arbitrary network
# resources.  Downloads, user-facing alerts, and source links work without
# granting same-origin access or navigation of the management window. New
# windows inherit the sandbox because allow-popups-to-escape-sandbox is absent.
HTML_PREVIEW_SANDBOX = "allow-scripts allow-downloads allow-modals allow-popups"
HTML_STATIC_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
HTML_PREVIEW_CSP = (
    f"sandbox {HTML_PREVIEW_SANDBOX}; default-src 'none'; "
    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; media-src data: blob:; font-src data: blob:; "
    "connect-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; "
    "manifest-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
AGENT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fileweb-agent")
MAX_AGENT_QUEUE = 8
MAX_DIRECTORY_OPTIONS = 200
MAX_DIRECTORY_DEPTH = 4
MAX_AGENT_HISTORY = 20
MAX_AGENT_HISTORY_CHARS = 30_000
SESSIONS: dict[str, dict] = {}
SESSIONS_LOCK = threading.Lock()
SHARES: dict[str, dict] = {}
SHARES_LOCK = threading.Lock()
PASSWORD_LOCK = threading.Lock()


class BadPath(ValueError):
    pass


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def ensure_root() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / ".agent-jobs").mkdir(mode=0o700, exist_ok=True)
    SHARE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.chmod(0o700)
    load_shares()
    load_sessions()


def load_shares() -> None:
    global SHARES
    try:
        data = json.loads(SHARE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            SHARES = {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, dict)}
    except (OSError, json.JSONDecodeError):
        SHARES = {}


def save_shares() -> None:
    temporary = SHARE_FILE.with_name(f".{SHARE_FILE.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(SHARES, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, SHARE_FILE)


def session_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_sessions() -> None:
    now = time.time()
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        records = data.get("sessions", {}) if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        records = {}
    valid = {
        key: value for key, value in records.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and value.get("persistent") is True
        and value.get("user") == USER
        and isinstance(value.get("csrf"), str)
        and isinstance(value.get("expires"), (int, float))
        and value["expires"] > now
    }
    with SESSIONS_LOCK:
        SESSIONS.clear()
        SESSIONS.update(valid)


def save_sessions_locked() -> None:
    now = time.time()
    records = {
        key: value for key, value in SESSIONS.items()
        if value.get("persistent") is True and value.get("expires", 0) > now
    }
    temporary = SESSION_FILE.with_name(f".{SESSION_FILE.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps({"version": 1, "sessions": records}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, SESSION_FILE)


def cookie_value(handler: BaseHTTPRequestHandler, name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except CookieError:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def session_token(handler: BaseHTTPRequestHandler) -> str:
    return cookie_value(handler, "fileweb_session")


def session_record(handler: BaseHTTPRequestHandler) -> dict | None:
    token = session_token(handler)
    if not token:
        return None
    key = session_id(token)
    with SESSIONS_LOCK:
        session = SESSIONS.get(key)
        if not session or session.get("expires", 0) < time.time():
            expired = SESSIONS.pop(key, None)
            if expired and expired.get("persistent") is True:
                try:
                    save_sessions_locked()
                except OSError:
                    audit("session_cleanup", result="error")
            return None
        return dict(session)


def session_user(handler: BaseHTTPRequestHandler) -> str | None:
    session = session_record(handler)
    return str(session.get("user", USER)) if session else None


def preview_limit(path: Path) -> int:
    return MAX_HTML_PREVIEW if path.suffix.lower() in HTML_EXTENSIONS else MAX_PREVIEW


def interactive_preview(query: dict[str, list[str]]) -> bool:
    # Normal document navigation should keep self-contained controls working.
    # Explicit static, unknown, empty, or repeated modes remain script-free.
    return query.get("mode", ["interactive"]) == ["interactive"]


def relative_path(value: str | None) -> str:
    """Decode one URL layer and normalize a user-facing relative path."""
    if value is None:
        return ""
    value = unquote(value)
    if "\x00" in value or "\\" in value:
        raise BadPath("invalid path")
    value = value.strip("/")
    if value in {"", "."}:
        return ""
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BadPath("invalid path")
    return "/".join(parts)


def safe_path(value: str | None, *, must_exist: bool = False) -> Path:
    rel = relative_path(value)
    candidate = ROOT.joinpath(*rel.split("/")) if rel else ROOT
    try:
        relative = candidate.relative_to(ROOT)
    except ValueError as exc:
        raise BadPath("path escapes managed root") from exc

    current = ROOT
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise BadPath("symbolic links are not allowed")
        except OSError as exc:
            raise BadPath("cannot inspect path") from exc
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def display_path(path: Path) -> str:
    return "" if path == ROOT else path.relative_to(ROOT).as_posix()


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def file_kind(path: Path) -> str:
    if path.is_dir():
        return "directory"
    return "file"


def mime_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def is_previewable(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in HTML_EXTENSIONS:
        return "html", "text/html; charset=utf-8"
    if suffix in IMAGE_EXTENSIONS:
        return "image", mime_for(path)
    if suffix in TEXT_EXTENSIONS or mime_for(path).startswith("text/") or suffix == ".pdf":
        return ("pdf" if suffix == ".pdf" else "text"), mime_for(path)
    return "binary", mime_for(path)


def basic_user(handler: BaseHTTPRequestHandler) -> bool:
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        user, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user, USER) and secrets.compare_digest(password, PASSWORD)


def csrf_ok(handler: BaseHTTPRequestHandler) -> bool:
    session = session_record(handler)
    if session:
        expected = str(session.get("csrf", ""))
    else:
        expected = BASIC_CSRF_TOKEN if basic_user(handler) else ""
    browser_token = cookie_value(handler, "fileweb_csrf")
    supplied = handler.headers.get("X-CSRF-Token", "")
    return (
        bool(expected)
        and secrets.compare_digest(browser_token, expected)
        and secrets.compare_digest(supplied, expected)
    )


def audit(action: str, path: str = "", result: str = "ok") -> None:
    # Keep audit entries metadata-only; prompts and credentials never enter logs.
    print(json.dumps({"ts": int(time.time()), "action": action, "path": path, "result": result}, ensure_ascii=False), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "FileWeb/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_session_cookies(self, token: str, csrf_token: str, remember: bool) -> None:
        lifetime = f"; Max-Age={REMEMBER_SESSION_SECONDS}" if remember else ""
        self.send_header("Set-Cookie", f"fileweb_session={token}{lifetime}; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Set-Cookie", f"fileweb_csrf={csrf_token}{lifetime}; SameSite=Strict; Path=/")

    def refresh_csrf_cookie(self) -> None:
        session = session_record(self)
        if not session and basic_user(self):
            self.send_header("Set-Cookie", f"fileweb_csrf={BASIC_CSRF_TOKEN}; SameSite=Strict; Path=/")
            return
        if not session:
            return
        remaining = max(0, int(session.get("expires", 0) - time.time()))
        lifetime = f"; Max-Age={remaining}" if session.get("persistent") is True else ""
        self.send_header("Set-Cookie", f"fileweb_csrf={session['csrf']}{lifetime}; SameSite=Strict; Path=/")

    def clear_session_cookies(self) -> None:
        self.send_header("Set-Cookie", "fileweb_session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Set-Cookie", "fileweb_csrf=; Max-Age=0; SameSite=Strict; Path=/")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def require_auth(self) -> bool:
        if session_user(self) or basic_user(self):
            return True
        if not self.path.startswith("/api/") and self.path not in {"/app.js", "/styles.css"}:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/login.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="FileWeb"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    def require_mutation(self) -> bool:
        if not csrf_ok(self):
            self.send_error_json(HTTPStatus.FORBIDDEN, "CSRF token required")
            return False
        return True

    def parse_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request is too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON object required")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json({"status": "ok"})
            return
        if parsed.path == "/login.html" and session_user(self):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path in {"/login.html", "/login.js", "/styles.css"}:
            self.serve_static(parsed.path, public=True)
            return
        if parsed.path.startswith("/share/"):
            try:
                self.serve_share(parsed.path, parse_qs(parsed.query, keep_blank_values=True))
            except (BadPath, FileNotFoundError):
                self.send_error_json(HTTPStatus.NOT_FOUND, "share not found")
            except ApiError as exc:
                self.send_error_json(exc.status, exc.message)
            return
        if parsed.path in {"/", "/index.html", "/app.js", "/styles.css"}:
            if not self.require_auth():
                return
            self.serve_static(parsed.path)
            return
        if parsed.path == "/preview":
            if not self.require_auth():
                return
            try:
                self.serve_preview(parse_qs(parsed.query, keep_blank_values=True))
            except (BadPath, ValueError):
                self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid path")
            except FileNotFoundError:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            except PermissionError:
                self.send_error_json(HTTPStatus.FORBIDDEN, "permission denied")
            except ApiError as exc:
                self.send_error_json(exc.status, exc.message)
            return
        if not self.require_auth():
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/api/list":
                self.api_list(query)
            elif parsed.path == "/api/download":
                self.api_download(query)
            elif parsed.path == "/api/raw":
                self.api_raw(query)
            elif parsed.path == "/api/preview":
                self.api_preview(query)
            elif parsed.path == "/api/me":
                self.api_me()
            elif parsed.path == "/api/avatar":
                self.api_avatar()
            elif parsed.path == "/api/share":
                self.api_share_list()
            elif parsed.path == "/api/directories":
                self.api_directories(query)
            elif parsed.path.startswith("/api/agent/"):
                agent_path = parsed.path[len("/api/agent/"):]
                if agent_path.endswith("/stream"):
                    self.api_agent_stream(agent_path[:-len("/stream")].rstrip("/"))
                else:
                    self.api_agent_status(agent_path)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except (BadPath, ValueError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid path")
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except PermissionError:
            self.send_error_json(HTTPStatus.FORBIDDEN, "permission denied")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            try:
                self.api_login()
            except ApiError as exc:
                self.send_error_json(exc.status, exc.message)
            return
        if not self.require_auth() or not self.require_mutation():
            return
        try:
            if parsed.path == "/api/logout":
                self.api_logout()
            elif parsed.path == "/api/password":
                self.api_password()
            elif parsed.path == "/api/avatar":
                self.api_avatar_upload()
            elif parsed.path == "/api/share":
                self.api_share_create()
            elif parsed.path == "/api/mkdir":
                self.api_mkdir()
            elif parsed.path == "/api/upload":
                self.api_upload(parse_qs(parsed.query, keep_blank_values=True))
            elif parsed.path == "/api/delete":
                self.api_delete()
            elif parsed.path == "/api/move":
                self.api_move()
            elif parsed.path == "/api/agent":
                self.api_agent_start()
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except (BadPath, ValueError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid path")
        except FileExistsError:
            self.send_error_json(HTTPStatus.CONFLICT, "already exists")
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except PermissionError:
            self.send_error_json(HTTPStatus.FORBIDDEN, "permission denied")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)

    def do_DELETE(self) -> None:
        if not self.require_auth() or not self.require_mutation():
            return
        try:
            if urlparse(self.path).path != "/api/delete":
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            self.api_delete()
        except (BadPath, ValueError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid path")
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except PermissionError:
            self.send_error_json(HTTPStatus.FORBIDDEN, "permission denied")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)

    def serve_static(self, path: str, public: bool = False) -> None:
        name = "index.html" if path in {"/", "/index.html"} else path[1:]
        root = Path(__file__).resolve().parent / "static"
        target = root / name
        if not target.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        body = target.read_bytes()
        content_type = "text/html; charset=utf-8" if name.endswith(".html") else mime_for(target) + "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'")
        if not public:
            self.refresh_csrf_cookie()
        self.end_headers()
        self.wfile.write(body)

    def serve_preview(self, query: dict[str, list[str]], *, share_token: str | None = None) -> None:
        path = safe_path(query.get("path", [""])[0], must_exist=True)
        if not path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a file")
        if path.stat().st_size > preview_limit(path):
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "preview exceeds size limit")
        kind, mime = is_previewable(path)
        relative = quote(display_path(path))
        safe_name = html.escape(path.name, quote=True)
        safe_mime = html.escape(mime, quote=True)
        raw_prefix = f"/share/{quote(share_token)}/raw" if share_token else "/api/raw"
        raw_url = f"{raw_prefix}?path={relative}" if not share_token else raw_prefix
        mode_link = ""
        if kind == "text":
            content = html.escape(path.read_text(encoding="utf-8", errors="replace"))
            body = f"<pre class=\"code\">{content}</pre>"
        elif kind == "html":
            interactive = interactive_preview(query)
            preview_url = f"/share/{quote(share_token)}" if share_token else f"/preview?path={relative}"
            if interactive:
                raw_url += ("?" if share_token else "&") + "mode=interactive"
                preview_url += ("?" if share_token else "&") + "mode=static"
                mode_label = "静态预览"
                mode_status = "交互预览"
            else:
                raw_url += ("?" if share_token else "&") + "mode=static"
                preview_url += ("?" if share_token else "&") + "mode=interactive"
                mode_label = "启用交互"
                mode_status = "静态预览 · 点击启用交互可使用文档内的按钮"
            mode_link = f'<span class="mode-status">{mode_status}</span><a class="mode-toggle" href="{html.escape(preview_url, quote=True)}">{mode_label}</a>'
            sandbox = HTML_PREVIEW_SANDBOX if interactive else ""
            body = f'<iframe class="frame" title="预览" sandbox="{sandbox}" referrerpolicy="no-referrer" src="{html.escape(raw_url, quote=True)}"></iframe>'
        elif kind == "image":
            body = f"<img class=\"image\" alt=\"\" src=\"{raw_url}\">"
        elif kind == "pdf":
            body = f"<iframe class=\"frame\" title=\"PDF 预览\" src=\"{raw_url}\"></iframe>"
        else:
            body = "<div class=\"unsupported\">此文件类型不支持在线预览。</div>" if share_token else f"<div class=\"unsupported\">此文件类型不支持在线预览。<a href=\"/api/download?path={relative}\">下载文件</a></div>"
        download_link = "" if share_token else f'<a class="download" href="/api/download?path={relative}">下载</a>'
        page = f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>预览 · {safe_name}</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:#f4f2eb;color:#17251f;font:14px/1.5 system-ui,sans-serif}}header{{height:68px;padding:0 28px;display:flex;align-items:center;justify-content:space-between;background:#fffefa;border-bottom:1px solid #dce3dc}}h1{{font:600 18px Georgia,serif;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}small{{color:#82908a}}header>div:first-child{{min-width:0}}.preview-actions{{display:flex;align-items:center;gap:16px;flex-shrink:0;margin-left:20px}}.mode-status{{font-size:12px;color:#64776c}}.mode-toggle{{padding:8px 12px;border:1px solid #b7cabe;border-radius:6px;text-decoration:none;white-space:nowrap;font-size:13px}}main{{height:calc(100vh - 68px);padding:24px;display:grid;place-items:center}}.code{{width:min(1100px,100%);height:100%;margin:0;padding:25px;background:#fffefa;border:1px solid #dce3dc;overflow:auto;white-space:pre-wrap;word-break:break-word;font:13px/1.7 ui-monospace,monospace}}.frame{{width:100%;height:100%;border:1px solid #dce3dc;background:#fff}}.image{{max-width:100%;max-height:100%;object-fit:contain}}.unsupported{{padding:35px;background:#fffefa;border:1px solid #dce3dc}}a{{color:#14563f;font-weight:600}}.download{{color:#14563f;text-decoration:none;font-size:12px}}@media(max-width:800px){{.mode-status{{display:none}}}}@media(max-width:600px){{main{{padding:12px}}header{{padding:0 16px}}.preview-actions{{gap:10px;margin-left:12px}}}}</style></head><body><header><div><h1>{safe_name}</h1><small>{safe_mime} · {path.stat().st_size} bytes</small></div><div class="preview-actions">{mode_link}{download_link}</div></header><main>{body}</main></body></html>"""
        encoded = page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'")
        if not share_token:
            self.refresh_csrf_cookie()
        self.end_headers()
        self.wfile.write(encoded)

    def share_entry(self, token: str) -> Path:
        if not token or len(token) > 100:
            raise ApiError(HTTPStatus.NOT_FOUND, "share not found")
        with SHARES_LOCK:
            share = SHARES.get(token)
            if not share or float(share.get("expires", 0)) < time.time():
                if share:
                    SHARES.pop(token, None)
                    save_shares()
                raise ApiError(HTTPStatus.NOT_FOUND, "share not found")
            path_value = share.get("path")
        if not isinstance(path_value, str):
            raise ApiError(HTTPStatus.NOT_FOUND, "share not found")
        return safe_path(path_value, must_exist=True)

    def serve_share(self, request_path: str, query: dict[str, list[str]]) -> None:
        parts = request_path.split("/")
        if len(parts) not in {3, 4} or (len(parts) == 4 and parts[3] != "raw"):
            raise ApiError(HTTPStatus.NOT_FOUND, "share not found")
        token = parts[2] if len(parts) > 2 else ""
        path = self.share_entry(token)
        if len(parts) > 3 and parts[3] == "raw":
            if not path.is_file() or path.stat().st_size > preview_limit(path):
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "preview exceeds size limit")
            kind, mime = is_previewable(path)
            if kind not in {"html", "image", "pdf"}:
                raise ApiError(HTTPStatus.BAD_REQUEST, "file is not previewable")
            self.send_file(path, mime=mime, disposition="inline", sandbox=(kind == "html"), interactive=interactive_preview(query))
            return
        preview_query = {"path": [display_path(path)]}
        if "mode" in query:
            preview_query["mode"] = query["mode"]
        self.serve_preview(preview_query, share_token=token)

    def api_login(self) -> None:
        payload = self.parse_json()
        username = payload.get("username")
        password = payload.get("password")
        remember = payload.get("remember", False)
        if not isinstance(username, str) or not isinstance(password, str) or not secrets.compare_digest(username, USER) or not secrets.compare_digest(password, PASSWORD):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "用户名或密码错误")
        if not isinstance(remember, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "登录状态选项无效")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires = time.time() + (REMEMBER_SESSION_SECONDS if remember else SESSION_SECONDS)
        key = session_id(token)
        with SESSIONS_LOCK:
            SESSIONS[key] = {
                "user": USER,
                "expires": expires,
                "persistent": remember,
                "csrf": csrf_token,
            }
            if remember:
                try:
                    save_sessions_locked()
                except OSError as exc:
                    SESSIONS.pop(key, None)
                    raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "无法保存登录状态") from exc
        body = json_bytes({"user": USER, "remembered": remember})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_session_cookies(token, csrf_token, remember)
        self.end_headers()
        self.wfile.write(body)

    def api_logout(self) -> None:
        token = session_token(self)
        with SESSIONS_LOCK:
            session = SESSIONS.pop(session_id(token), None) if token else None
            if session and session.get("persistent") is True:
                try:
                    save_sessions_locked()
                except OSError:
                    audit("logout", result="session_store_error")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.clear_session_cookies()
        self.end_headers()
        self.wfile.write(b"{}")

    def api_me(self) -> None:
        avatar = PROFILE_DIR / "avatar"
        self.send_json({"username": USER, "avatar": "/api/avatar" if avatar.is_file() else None})

    def api_avatar(self) -> None:
        avatar = PROFILE_DIR / "avatar"
        if not avatar.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "avatar not found")
            return
        mime = "image/jpeg"
        try:
            mime = (PROFILE_DIR / "avatar.type").read_text(encoding="ascii").strip() or mime
        except OSError:
            pass
        self.send_file(avatar, mime=mime, disposition="inline")

    def api_avatar_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        if not content_type.startswith("multipart/form-data") or length <= 0 or length > MAX_AVATAR + 64 * 1024:
            raise ApiError(HTTPStatus.BAD_REQUEST, "头像文件无效")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(length)}, keep_blank_values=False, max_num_fields=3)
        item = form["avatar"] if "avatar" in form else None
        mime = str(getattr(item, "type", "")) if item is not None else ""
        if item is None or not mime.startswith("image/") or mime == "image/svg+xml":
            raise ApiError(HTTPStatus.BAD_REQUEST, "仅支持 PNG、JPEG、WEBP 或 GIF")
        data = item.file.read(MAX_AVATAR + 1)
        if len(data) > MAX_AVATAR:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "头像不能超过 2 MiB")
        signatures = {"image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8\xff", "image/gif": b"GIF8", "image/webp": b"RIFF"}
        if not any(mime == key and data.startswith(signature) for key, signature in signatures.items()):
            raise ApiError(HTTPStatus.BAD_REQUEST, "头像格式不受支持")
        temporary = PROFILE_DIR / f".avatar-{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        os.replace(temporary, PROFILE_DIR / "avatar")
        (PROFILE_DIR / "avatar.type").write_text(mime, encoding="ascii")
        self.send_json({"avatar": "/api/avatar"})

    def api_password(self) -> None:
        global PASSWORD
        payload = self.parse_json()
        current = payload.get("current_password")
        new = payload.get("new_password")
        if not isinstance(current, str) or not isinstance(new, str) or not secrets.compare_digest(current, PASSWORD):
            raise ApiError(HTTPStatus.FORBIDDEN, "当前密码不正确")
        if not new or len(new) > 200:
            raise ApiError(HTTPStatus.BAD_REQUEST, "新密码不能为空且不能超过 200 个字符")
        with PASSWORD_LOCK:
            with PASSWORD_FILE.open("r+", encoding="utf-8") as password_stream:
                password_stream.seek(0)
                password_stream.write(new + "\n")
                password_stream.truncate()
                password_stream.flush()
                os.fsync(password_stream.fileno())
            PASSWORD_FILE.chmod(0o600)
            PASSWORD = new
        with SESSIONS_LOCK:
            for session in SESSIONS.values():
                session["expires"] = min(session.get("expires", time.time()), time.time() + SESSION_SECONDS)
            try:
                save_sessions_locked()
            except OSError:
                audit("password", result="session_store_error")
        self.send_json({"ok": True})

    def api_share_create(self) -> None:
        payload = self.parse_json()
        path_value = payload.get("path")
        if not isinstance(path_value, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid path")
        path = safe_path(path_value, must_exist=True)
        if not path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, "only files can be shared")
        days = payload.get("expires_days", 7)
        if not isinstance(days, (int, float)) or days <= 0 or days > 30:
            raise ApiError(HTTPStatus.BAD_REQUEST, "expires_days must be between 1 and 30")
        token = secrets.token_urlsafe(24)
        with SHARES_LOCK:
            SHARES[token] = {"path": display_path(path), "created": int(time.time()), "expires": time.time() + float(days) * 86400}
            save_shares()
        audit("share", display_path(path))
        base_url = PUBLIC_URL or f"http://{self.headers.get('Host', f'127.0.0.1:{PORT}') }"
        self.send_json({"token": token, "url": f"{base_url}/share/{token}", "expires": SHARES[token]["expires"]}, HTTPStatus.CREATED)

    def api_share_list(self) -> None:
        now = time.time()
        with SHARES_LOCK:
            expired = [token for token, share in SHARES.items() if float(share.get("expires", 0)) < now]
            for token in expired:
                SHARES.pop(token, None)
            if expired:
                save_shares()
            items = [{"token": token, **share, "url": f"/share/{token}"} for token, share in SHARES.items()]
        self.send_json({"shares": items})

    def api_list(self, query: dict[str, list[str]]) -> None:
        directory = safe_path(query.get("path", [""])[0], must_exist=True)
        if not directory.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a directory")
        entries = []
        for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            if entry.name in {".agent-jobs", ".uploading"} or entry.name.startswith(".upload-"):
                continue
            if entry.is_symlink():
                continue
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "path": display_path(entry),
                "kind": file_kind(entry),
                "size": stat.st_size if entry.is_file() else None,
                "mtime": int(stat.st_mtime),
                "mime": mime_for(entry) if entry.is_file() else None,
                "preview": is_previewable(entry)[0] if entry.is_file() else None,
            })
        self.send_json({"path": display_path(directory), "entries": entries})

    def api_mkdir(self) -> None:
        payload = self.parse_json()
        parent = safe_path(str(payload.get("path", "")), must_exist=True)
        if not parent.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a directory")
        name = payload.get("name")
        if not isinstance(name, str) or not name or any(ord(char) < 32 for char in name) or name in {".", "..", ".agent-jobs", ".uploading"} or name.startswith(".upload-") or "/" in name or "\\" in name or "\x00" in name:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid directory name")
        target = safe_path(f"{display_path(parent)}/{name}")
        target.mkdir(mode=0o750)
        audit("mkdir", display_path(target))
        self.send_json({"path": display_path(target)}, HTTPStatus.CREATED)

    def api_upload(self, query: dict[str, list[str]]) -> None:
        parent = safe_path(query.get("path", [""])[0], must_exist=True)
        if not parent.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a directory")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "upload exceeds size limit")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "multipart upload required")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
            "REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(length)
        }, keep_blank_values=False, max_num_fields=20)
        items = form["files"] if "files" in form else []
        if not isinstance(items, list):
            items = [items]
        overwrite = query.get("overwrite", ["0"])[0] == "1"
        prepared = []
        seen: set[Path] = set()
        conflicts = []
        for item in items:
            name = os.path.basename(str(getattr(item, "filename", "")))
            if not name or any(ord(char) < 32 for char in name) or name in {".", "..", ".agent-jobs", ".uploading"} or name.startswith(".upload-") or "\x00" in name or "/" in name or "\\" in name:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid filename")
            target = safe_path(f"{display_path(parent)}/{name}")
            if target in seen:
                raise ApiError(HTTPStatus.BAD_REQUEST, "duplicate filename in upload")
            seen.add(target)
            if target.exists() or target.is_symlink():
                if target.is_dir():
                    raise ApiError(HTTPStatus.CONFLICT, "a directory has the same name")
                conflicts.append(display_path(target))
            prepared.append((item, name, target))
        if conflicts and not overwrite:
            self.send_json({"error": "already_exists", "conflicts": conflicts}, HTTPStatus.CONFLICT)
            return
        created = []
        for item, name, target in prepared:
            temp = parent / f".upload-{uuid.uuid4().hex}.tmp"
            size = 0
            try:
                with temp.open("xb") as handle:
                    while True:
                        chunk = item.file.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "upload exceeds size limit")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if overwrite:
                    os.replace(temp, target)
                else:
                    os.link(temp, target)
                temp.unlink(missing_ok=True)
            finally:
                temp.unlink(missing_ok=True)
            created.append({"name": name, "path": display_path(target), "size": size})
            audit("upload", display_path(target))
        self.send_json({"files": created}, HTTPStatus.CREATED)

    def api_delete(self) -> None:
        payload = self.parse_json()
        requested = payload.get("path", "")
        if not isinstance(requested, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid path")
        path = safe_path(requested, must_exist=True)
        if path == ROOT or path.name in {".agent-jobs", ".uploading"} or path.name.startswith(".upload-"):
            raise ApiError(HTTPStatus.FORBIDDEN, "protected path")
        if path.is_dir():
            if not payload.get("recursive") is True:
                raise ApiError(HTTPStatus.CONFLICT, "directory deletion requires confirmation")
            for child in path.rglob("*"):
                if child.is_symlink():
                    raise ApiError(HTTPStatus.FORBIDDEN, "symbolic links are not allowed")
            shutil.rmtree(path)
        else:
            path.unlink()
        audit("delete", display_path(path))
        self.send_json({"path": display_path(path)})

    def api_move(self) -> None:
        payload = self.parse_json()
        source_value = payload.get("path")
        destination_value = payload.get("destination")
        if not isinstance(source_value, str) or not isinstance(destination_value, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "path and destination are required")
        source = safe_path(source_value, must_exist=True)
        if source == ROOT or source.name in {".agent-jobs", ".uploading"} or source.name.startswith(".upload-"):
            raise ApiError(HTTPStatus.FORBIDDEN, "protected path")
        destination = safe_path(destination_value, must_exist=True)
        if not destination.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, "destination is not a directory")
        if source == destination:
            raise ApiError(HTTPStatus.CONFLICT, "cannot move into itself")
        if source.is_dir():
            try:
                destination.relative_to(source)
            except ValueError:
                pass
            else:
                raise ApiError(HTTPStatus.CONFLICT, "cannot move a directory into itself")
        target = safe_path(f"{display_path(destination)}/{source.name}")
        if target == source or target.exists() or target.is_symlink():
            raise ApiError(HTTPStatus.CONFLICT, "destination already exists")
        os.rename(source, target)
        audit("move", f"{display_path(source)} -> {display_path(target)}")
        self.send_json({"path": display_path(target), "destination": display_path(destination)})

    def api_download(self, query: dict[str, list[str]]) -> None:
        path = safe_path(query.get("path", [""])[0], must_exist=True)
        if not path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a file")
        self.send_file(path, mime="application/octet-stream", disposition="attachment")
        audit("download", display_path(path))

    def api_raw(self, query: dict[str, list[str]]) -> None:
        path = safe_path(query.get("path", [""])[0], must_exist=True)
        if not path.is_file() or path.stat().st_size > preview_limit(path):
            raise ApiError(HTTPStatus.BAD_REQUEST, "file is not previewable")
        kind, mime = is_previewable(path)
        if kind not in {"html", "image", "pdf"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "raw preview is not available")
        self.send_file(path, mime=mime, disposition="inline", sandbox=(kind == "html"), interactive=interactive_preview(query))

    def api_preview(self, query: dict[str, list[str]]) -> None:
        path = safe_path(query.get("path", [""])[0], must_exist=True)
        if not path.is_file():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a file")
        if path.stat().st_size > preview_limit(path):
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "preview exceeds size limit")
        kind, mime = is_previewable(path)
        payload = {"path": display_path(path), "name": path.name, "kind": kind, "mime": mime, "size": path.stat().st_size}
        if kind == "text":
            payload["content"] = path.read_text(encoding="utf-8", errors="replace")
        elif kind in {"html", "image", "pdf"}:
            payload["url"] = "/api/raw?path=" + quote(display_path(path))
            if kind == "html":
                payload["url"] += "&mode=" + ("interactive" if interactive_preview(query) else "static")
        self.send_json(payload)

    def api_directories(self, query: dict[str, list[str]]) -> None:
        """Return a bounded directory tree for work-directory selectors."""
        base = safe_path(query.get("path", [""])[0], must_exist=True)
        if not base.is_dir():
            raise ApiError(HTTPStatus.BAD_REQUEST, "not a directory")
        try:
            depth = int(query.get("depth", ["2"])[0])
            limit = int(query.get("limit", [str(MAX_DIRECTORY_OPTIONS)])[0])
        except (TypeError, ValueError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid directory query") from exc
        if depth < 0 or depth > MAX_DIRECTORY_DEPTH:
            raise ApiError(HTTPStatus.BAD_REQUEST, "directory depth is out of range")
        if limit < 1 or limit > MAX_DIRECTORY_OPTIONS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "directory limit is out of range")

        root_label = "根目录" if base == ROOT else base.name
        directories = [{"name": root_label, "path": display_path(base), "depth": 0}]
        queue: list[tuple[Path, int]] = [(base, 0)]
        index = 0
        truncated = False
        while index < len(queue):
            current, current_depth = queue[index]
            index += 1
            if current_depth >= depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for child in children:
                if child.is_symlink() or not child.is_dir() or child.name in {".agent-jobs", ".uploading"} or child.name.startswith(".upload-"):
                    continue
                if len(directories) >= limit:
                    truncated = True
                    break
                child_depth = current_depth + 1
                directories.append({"name": child.name, "path": display_path(child), "depth": child_depth})
                queue.append((child, child_depth))
            if truncated:
                break
        self.send_json({"path": display_path(base), "directories": directories, "truncated": truncated})

    @staticmethod
    def normalize_agent_history(value: object) -> list[dict[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_AGENT_HISTORY:
            raise ApiError(HTTPStatus.BAD_REQUEST, "history must contain at most 20 messages")
        history: list[dict[str, str]] = []
        total = 0
        for item in value:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "history messages require user or assistant role and text content")
            content = item["content"].strip()
            if not content or len(content) > 6000:
                raise ApiError(HTTPStatus.BAD_REQUEST, "history message must be 1-6000 characters")
            total += len(content)
            if total > MAX_AGENT_HISTORY_CHARS:
                raise ApiError(HTTPStatus.BAD_REQUEST, "history is too large")
            history.append({"role": item["role"], "content": content})
        return history

    def api_agent_start(self) -> None:
        payload = self.parse_json()
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "prompt must be 1-6000 characters")
        requested_path = payload.get("path", "")
        if not isinstance(requested_path, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid path")
        selected = safe_path(requested_path, must_exist=True)
        if not selected.exists():
            raise ApiError(HTTPStatus.NOT_FOUND, "path not found")
        history = self.normalize_agent_history(payload.get("history"))
        allow_write = payload.get("allow_write") is True and AGENT_ALLOW_WRITE
        prune_jobs()
        with JOBS_LOCK:
            active_jobs = sum(1 for job in JOBS.values() if job["status"] in {"queued", "running"})
        if active_jobs >= MAX_AGENT_QUEUE:
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "too many agent tasks")
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"id": job_id, "status": "queued", "created": int(time.time()), "output": "", "path": display_path(selected), "allow_write": allow_write}
        AGENT_POOL.submit(run_agent, job_id, prompt, selected, allow_write, history)
        self.send_json({"id": job_id, "stream_url": f"/api/agent/{job_id}/stream"}, HTTPStatus.ACCEPTED)

    def api_agent_status(self, job_id: str) -> None:
        if not job_id.isalnum() or len(job_id) != 32:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid job id")
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                raise ApiError(HTTPStatus.NOT_FOUND, "job not found")
            self.send_json(dict(job))

    def api_agent_stream(self, job_id: str) -> None:
        if not job_id.isalnum() or len(job_id) != 32:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid job id")
        with JOBS_LOCK:
            if job_id not in JOBS:
                raise ApiError(HTTPStatus.NOT_FOUND, "job not found")

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        last_signature: tuple[object, ...] | None = None
        last_output = ""
        last_heartbeat = time.monotonic()
        terminal = {"succeeded", "failed", "timeout"}
        try:
            while True:
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    snapshot = dict(job) if job else None
                if snapshot is None:
                    return
                signature = (
                    snapshot.get("status"), snapshot.get("output", ""), snapshot.get("exit_code"),
                    snapshot.get("started"), snapshot.get("finished"),
                )
                if signature != last_signature:
                    current_output = str(snapshot.get("output", ""))
                    delta = current_output[len(last_output):] if current_output.startswith(last_output) else current_output
                    event_payload = {**snapshot, "delta": delta}
                    self.wfile.write(b"event: update\n")
                    self.wfile.write(f"data: {json.dumps(event_payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_signature = signature
                    last_output = current_output
                    if snapshot.get("status") in terminal:
                        self.wfile.write(b"event: done\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                if time.monotonic() - last_heartbeat >= 10:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def send_file(self, path: Path, *, mime: str, disposition: str, sandbox: bool = False, interactive: bool = False) -> None:
        size = path.stat().st_size
        # HTTP headers must stay ASCII. RFC 6266/5987 carries the original
        # Unicode filename in filename*, with a quoted fallback for old clients.
        safe_name = "".join(char for char in path.name if ord(char) >= 32 and ord(char) != 127)
        fallback_name = "".join(
            char if char.isascii() and char not in '\\"%' else "_"
            for char in safe_name
        ) or "download"
        disposition_header = (
            f'{disposition}; filename="{fallback_name}"; '
            f"filename*=UTF-8''{quote(safe_name, safe='')}"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition_header)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if sandbox:
            self.send_header("Content-Security-Policy", HTML_PREVIEW_CSP if interactive else HTML_STATIC_CSP)
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)


def update_job(job_id: str, **changes: object) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(changes)


def prune_jobs() -> None:
    cutoff = int(time.time()) - 6 * 60 * 60
    with JOBS_LOCK:
        finished = [job for job in JOBS.values() if job["status"] not in {"queued", "running"} and job.get("finished", 0) < cutoff]
        for job in finished:
            JOBS.pop(job["id"], None)
        if len(JOBS) > 100:
            old = sorted((job for job in JOBS.values() if job["status"] not in {"queued", "running"}), key=lambda item: item.get("finished", item["created"]))
            for job in old[: max(0, len(JOBS) - 100)]:
                JOBS.pop(job["id"], None)


def run_agent(job_id: str, prompt: str, selected: Path, allow_write: bool, history: list[dict[str, str]] | None = None) -> None:
    update_job(job_id, status="running", started=int(time.time()))
    mode = "workspace-write" if allow_write else "read-only"
    workdir = selected if selected.is_dir() else selected.parent
    history_text = "\n\nPrior conversation:\n" + "\n".join(
        f"{item['role'].title()}: {item['content']}" for item in (history or [])
    ) if history else ""
    context = (
        "You are the file management assistant. Work only inside the managed root and selected path below. "
        "Do not inspect or modify the operating system, credentials, network configuration, or service files. "
        "Use the available tools only for the user's file task.\n\n"
        f"Managed root: {ROOT}\nSelected path: {selected}\nWrite permission: {allow_write}{history_text}\n\nUser request:\n{prompt.strip()}"
    )
    cmd = [
        CODEX_BIN, "exec", "--json", "--ephemeral", "--ignore-rules", "--skip-git-repo-check",
        "--sandbox", mode, "-C", str(workdir), "--add-dir", str(workdir), "-",
    ]
    env = {
        "PATH": f"{Path(CODEX_BIN).parent}:/usr/local/bin:/usr/bin:/bin", "HOME": str(AGENT_HOME), "CODEX_HOME": str(AGENT_HOME),
        "TMPDIR": str(AGENT_HOME / "tmp"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
    }
    output = ""
    try:
        os.makedirs(env["TMPDIR"], mode=0o700, exist_ok=True)
        process = subprocess.Popen(
            cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        process.stdin.write(context.encode("utf-8"))
        process.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + AGENT_TIMEOUT
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                raise subprocess.TimeoutExpired(cmd, AGENT_TIMEOUT)
            events = selector.select(min(0.25, remaining))
            if events:
                chunk = os.read(process.stdout.fileno(), 8192)
                if chunk:
                    output = (output + chunk.decode("utf-8", errors="replace"))[-AGENT_MAX_OUTPUT:]
                    update_job(job_id, output=output)
                else:
                    selector.unregister(process.stdout)
                    break
            if process.poll() is not None and not events:
                break
        process.wait(timeout=5)
        selector.close()
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        update_job(job_id, status="timeout", output=output, finished=int(time.time()))
        audit("agent", display_path(selected), "timeout")
        return
    except Exception as exc:
        update_job(job_id, status="failed", output=f"agent failed: {type(exc).__name__}", finished=int(time.time()))
        audit("agent", display_path(selected), "error")
        return
    status = "succeeded" if process.returncode == 0 else "failed"
    update_job(job_id, status=status, output=output, exit_code=process.returncode, finished=int(time.time()))
    audit("agent", display_path(selected), status)


def main() -> None:
    ensure_root()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"FileWeb listening on {HOST}:{PORT}, root={ROOT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
