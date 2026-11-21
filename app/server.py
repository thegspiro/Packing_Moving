#!/usr/bin/env python3
"""Packing Tracker server.

Serves the single-page web app and a small JSON API backed by SQLite.
Uses only the Python standard library.

Configuration (environment variables):
  TRACKER_PASSWORD  Required. Shared password needed to view or edit data.
  PORT              Port to listen on (default 5639).
  BIND_ADDRESS      Address to bind (default 0.0.0.0).
  DATA_DIR          Folder holding tracker.db (default /data).
  LOG_LEVEL         DEBUG, INFO, WARNING or ERROR (default INFO).
  PUBLIC_URL        Optional. Address phones use to reach the tracker, e.g.
                    http://192.168.1.50:5639. QR codes on labels point here.
                    If unset, the page uses the address it was opened from.
  PUID / PGID       User and group IDs to run as when the container starts as
                    root, e.g. from an Unraid template (default 99 / 100).
                    DATA_DIR is given to this user first. Ignored when the
                    container is already started as a non-root user.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG = logging.getLogger("packing-tracker")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# URL path -> (file in STATIC_DIR, Content-Type). Only these files are served.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/qrcode.js": ("qrcode.js", "text/javascript; charset=utf-8"),
}
MAX_PUBLIC_URL = 200

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_LOCATION = 60
MAX_ROOM = 60
MAX_ITEM = 200
MAX_ITEMS = 300
MAX_NOTES = 200
MAX_NUMBER = 999999
MAX_BULK_IDS = 5000

SESSION_COOKIE = "pt_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "packing-tracker"

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
BOX_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
BOX_PATH_RE = re.compile(r"^/api/boxes/([A-Za-z0-9-]{1,64})$")

# Each entry upgrades the schema by one version (PRAGMA user_version).
# Never edit an existing entry; append a new one for future changes.
MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS boxes (
        id          TEXT PRIMARY KEY,
        number      INTEGER NOT NULL UNIQUE,
        location    TEXT NOT NULL,
        room        TEXT NOT NULL DEFAULT '',
        contents    TEXT NOT NULL DEFAULT '[]',
        notes       TEXT NOT NULL DEFAULT '',
        fragile     INTEGER NOT NULL DEFAULT 0,
        open_first  INTEGER NOT NULL DEFAULT 0,
        rev         INTEGER NOT NULL DEFAULT 1,
        updated     TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


class ApiError(Exception):
    """An error that maps directly to an HTTP response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_public_url(value):
    """Validate PUBLIC_URL. Returns it without a trailing slash ('' if unset). Raises ValueError."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > MAX_PUBLIC_URL or CONTROL_CHARS.search(value) or " " in value:
        raise ValueError("PUBLIC_URL is too long or contains spaces or control characters.")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("PUBLIC_URL must start with http:// or https:// and include a host, "
                         "e.g. http://192.168.1.50:5639")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.params:
        raise ValueError("PUBLIC_URL must not contain a username, password, ?query or #fragment.")
    try:
        parsed.port
    except ValueError:
        raise ValueError("PUBLIC_URL has an invalid port.")
    return value.rstrip("/")


def clean_str(value, max_len):
    if not isinstance(value, str):
        return ""
    return CONTROL_CHARS.sub(" ", value).strip()[:max_len]


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_box_input(data):
    """Strictly validate a box sent by the web page. Raises ApiError(400)."""
    if not isinstance(data, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")

    number = data.get("number")
    if not is_int(number) or not 1 <= number <= MAX_NUMBER:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       "Box number must be a whole number between 1 and %d." % MAX_NUMBER)

    location = clean_str(data.get("location"), MAX_LOCATION)
    if not location:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Current location is required.")

    contents = data.get("contents", [])
    if not isinstance(contents, list) or not all(isinstance(c, str) for c in contents):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Contents must be a list of text items.")
    contents = [c for c in (clean_str(c, MAX_ITEM) for c in contents) if c]
    if len(contents) > MAX_ITEMS:
        raise ApiError(HTTPStatus.BAD_REQUEST, "A box can list at most %d items." % MAX_ITEMS)

    for flag in ("fragile", "openFirst"):
        if flag in data and not isinstance(data[flag], bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "%s must be true or false." % flag)

    for field in ("room", "notes"):
        if field in data and not isinstance(data[field], str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "%s must be text." % field)

    return {
        "number": number,
        "location": location,
        "room": clean_str(data.get("room", ""), MAX_ROOM),
        "contents": contents,
        "notes": clean_str(data.get("notes", ""), MAX_NOTES),
        "fragile": data.get("fragile", False),
        "openFirst": data.get("openFirst", False),
    }


def sanitize_import(data):
    """Leniently normalise a backup file (also accepts the original browser-only format).

    Invalid boxes and duplicate numbers are skipped, matching the web page's behaviour.
    Returns (boxes, skipped_count).
    """
    if not isinstance(data, dict) or not isinstance(data.get("boxes"), list):
        raise ApiError(HTTPStatus.BAD_REQUEST, "File does not look like a Packing Tracker backup.")

    boxes, seen_numbers, seen_ids, skipped = [], set(), set(), 0
    for raw in data["boxes"]:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        number = raw.get("number")
        if isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        if not is_int(number) or not 1 <= number <= MAX_NUMBER or number in seen_numbers:
            skipped += 1
            continue
        contents = raw.get("contents") if isinstance(raw.get("contents"), list) else []
        contents = [c for c in (clean_str(c, MAX_ITEM) for c in contents) if c][:MAX_ITEMS]
        box_id = raw.get("id") if isinstance(raw.get("id"), str) and BOX_ID_RE.match(raw["id"]) else None
        if not box_id or box_id in seen_ids:
            box_id = str(uuid.uuid4())
        seen_numbers.add(number)
        seen_ids.add(box_id)
        boxes.append({
            "id": box_id,
            "number": number,
            "location": clean_str(raw.get("location"), MAX_LOCATION) or "Old apartment",
            "room": clean_str(raw.get("room"), MAX_ROOM),
            "contents": contents,
            "notes": clean_str(raw.get("notes"), MAX_NOTES),
            "fragile": raw.get("fragile") is True,
            "openFirst": raw.get("openFirst") is True,
        })
    return boxes, skipped


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Store:
    """SQLite-backed storage. One short-lived connection per operation; writes are serialised."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._migrate()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def _migrate(self):
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise RuntimeError(
                        "Database schema version %d is newer than this app supports (%d). "
                        "Upgrade the container image." % (version, len(MIGRATIONS)))
                for target in range(version, len(MIGRATIONS)):
                    LOG.info("Applying database migration %d -> %d", target, target + 1)
                    conn.executescript("BEGIN;" + MIGRATIONS[target] +
                                       "PRAGMA user_version = %d; COMMIT;" % (target + 1))
            finally:
                conn.close()

    @staticmethod
    def _row_to_box(row):
        return {
            "id": row["id"],
            "number": row["number"],
            "location": row["location"],
            "room": row["room"],
            "contents": json.loads(row["contents"]),
            "notes": row["notes"],
            "fragile": bool(row["fragile"]),
            "openFirst": bool(row["open_first"]),
            "rev": row["rev"],
            "updated": row["updated"],
        }

    def get_or_create_secret(self):
        with self._write_lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()
                if row:
                    return row["value"]
                secret = secrets.token_hex(32)
                with conn:
                    conn.execute("INSERT INTO settings (key, value) VALUES ('session_secret', ?)", (secret,))
                return secret
            finally:
                conn.close()

    def list_boxes(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM boxes ORDER BY number").fetchall()
            return [self._row_to_box(r) for r in rows]
        finally:
            conn.close()

    def get_box(self, conn, box_id):
        row = conn.execute("SELECT * FROM boxes WHERE id = ?", (box_id,)).fetchone()
        return self._row_to_box(row) if row else None

    def _number_taken(self, conn, number, exclude_id=None):
        row = conn.execute("SELECT id FROM boxes WHERE number = ?", (number,)).fetchone()
        return row is not None and row["id"] != exclude_id

    def create_box(self, box):
        with self._write_lock:
            conn = self._connect()
            try:
                if self._number_taken(conn, box["number"]):
                    raise ApiError(HTTPStatus.CONFLICT, "Box %d already exists." % box["number"])
                box_id = str(uuid.uuid4())
                with conn:
                    conn.execute(
                        "INSERT INTO boxes (id, number, location, room, contents, notes, fragile, "
                        "open_first, rev, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        (box_id, box["number"], box["location"], box["room"],
                         json.dumps(box["contents"]), box["notes"], int(box["fragile"]),
                         int(box["openFirst"]), now_iso()))
                return self.get_box(conn, box_id)
            finally:
                conn.close()

    def update_box(self, box_id, box, expected_rev):
        with self._write_lock:
            conn = self._connect()
            try:
                current = self.get_box(conn, box_id)
                if current is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "That box no longer exists.")
                if expected_rev is not None and current["rev"] != expected_rev:
                    raise ApiError(HTTPStatus.CONFLICT,
                                   "Box %d was changed on another device. Reload and try again."
                                   % current["number"])
                if self._number_taken(conn, box["number"], exclude_id=box_id):
                    raise ApiError(HTTPStatus.CONFLICT, "Box %d already exists." % box["number"])
                with conn:
                    conn.execute(
                        "UPDATE boxes SET number = ?, location = ?, room = ?, contents = ?, notes = ?, "
                        "fragile = ?, open_first = ?, rev = rev + 1, updated = ? WHERE id = ?",
                        (box["number"], box["location"], box["room"], json.dumps(box["contents"]),
                         box["notes"], int(box["fragile"]), int(box["openFirst"]), now_iso(), box_id))
                return self.get_box(conn, box_id)
            finally:
                conn.close()

    def delete_box(self, box_id):
        with self._write_lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute("DELETE FROM boxes WHERE id = ?", (box_id,))
                if cur.rowcount == 0:
                    raise ApiError(HTTPStatus.NOT_FOUND, "That box no longer exists.")
            finally:
                conn.close()

    def move_boxes(self, box_ids, location):
        with self._write_lock:
            conn = self._connect()
            try:
                stamp = now_iso()
                moved = 0
                with conn:
                    for box_id in box_ids:
                        cur = conn.execute(
                            "UPDATE boxes SET location = ?, rev = rev + 1, updated = ? WHERE id = ?",
                            (location, stamp, box_id))
                        moved += cur.rowcount
                return moved
            finally:
                conn.close()

    def replace_all(self, boxes):
        with self._write_lock:
            conn = self._connect()
            try:
                stamp = now_iso()
                with conn:
                    conn.execute("DELETE FROM boxes")
                    conn.executemany(
                        "INSERT INTO boxes (id, number, location, room, contents, notes, fragile, "
                        "open_first, rev, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        [(b["id"], b["number"], b["location"], b["room"], json.dumps(b["contents"]),
                          b["notes"], int(b["fragile"]), int(b["openFirst"]), stamp) for b in boxes])
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class Auth:
    """Shared-password login with HMAC-signed session cookies.

    The signing key is derived from a random per-install secret and the password,
    so changing TRACKER_PASSWORD signs everyone out.
    """

    def __init__(self, password, secret):
        self._password = password.encode("utf-8")
        self._key = hmac.new(secret.encode("utf-8"), self._password, hashlib.sha256).digest()
        self._failures = {}
        self._lock = threading.Lock()

    def check_password(self, candidate):
        return hmac.compare_digest(
            hashlib.sha256(candidate.encode("utf-8")).digest(),
            hashlib.sha256(self._password).digest())

    def locked_out_for(self, client_ip):
        with self._lock:
            entry = self._failures.get(client_ip)
            if entry and entry[1] > time.time():
                return int(entry[1] - time.time()) + 1
            return 0

    def record_failure(self, client_ip):
        with self._lock:
            count, _ = self._failures.get(client_ip, (0, 0))
            count += 1
            locked_until = time.time() + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_FAILURES else 0
            self._failures[client_ip] = (0 if locked_until else count, locked_until)
            return bool(locked_until)

    def record_success(self, client_ip):
        with self._lock:
            self._failures.pop(client_ip, None)

    def _sign(self, payload):
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue_token(self):
        payload = "%d.%s" % (int(time.time()) + SESSION_TTL_SECONDS, secrets.token_hex(16))
        return payload + "." + self._sign(payload)

    def verify_token(self, token):
        if not token or token.count(".") != 2:
            return False
        expiry, nonce, signature = token.split(".")
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return hmac.compare_digest(self._sign(expiry + "." + nonce), signature)


# ---------------------------------------------------------------------------
# HTTP handling
# ---------------------------------------------------------------------------

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class TrackerHandler(BaseHTTPRequestHandler):
    server_version = "PackingTracker/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Set by make_server()
    store = None
    auth = None
    public_url = ""

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        LOG.debug("%s %s", self.client_address[0], fmt % args)

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data, headers=None):
        self._send(status, json.dumps(data).encode("utf-8"), headers=headers)

    def _error(self, status, message):
        # The request body may not have been read; close so leftover bytes aren't parsed as a new request.
        self.close_connection = True
        self._json(status, {"error": message}, headers={"Connection": "close"})

    def _read_json(self):
        if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise ApiError(HTTPStatus.FORBIDDEN, "Missing request header.")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length.")
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request is too large.")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Request body is not valid JSON.")

    def _require_csrf_header(self):
        if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise ApiError(HTTPStatus.FORBIDDEN, "Missing request header.")

    def _session_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _is_authenticated(self):
        return self.auth.verify_token(self._session_token())

    def _require_auth(self):
        if not self._is_authenticated():
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Please sign in.")

    def _cookie(self, value, max_age):
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (SESSION_COOKIE, value, max_age)

    def _dispatch(self, method):
        path = urlparse(self.path).path
        try:
            handled = self._route(method, path)
            if not handled:
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
        except ApiError as err:
            if err.status >= 500:
                LOG.error("%s %s failed: %s", method, path, err.message)
            self._error(err.status, err.message)
        except Exception:
            LOG.exception("Unhandled error on %s %s", method, path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Server error. Check the container log.")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # --- routes -----------------------------------------------------------

    def _route(self, method, path):
        client = self.client_address[0]

        if method == "GET" and path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            with open(os.path.join(STATIC_DIR, filename), "rb") as fh:
                self._send(HTTPStatus.OK, fh.read(), content_type=content_type)
            return True

        if method == "GET" and path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
            return True

        if method == "GET" and path == "/api/session":
            self._json(HTTPStatus.OK, {"authenticated": self._is_authenticated()})
            return True

        if method == "POST" and path == "/api/login":
            data = self._read_json()
            wait = self.auth.locked_out_for(client)
            if wait:
                raise ApiError(HTTPStatus.TOO_MANY_REQUESTS,
                               "Too many wrong passwords. Try again in %d seconds." % wait)
            password = data.get("password") if isinstance(data, dict) else None
            if not isinstance(password, str) or not self.auth.check_password(password):
                locked = self.auth.record_failure(client)
                LOG.warning("Failed sign-in from %s%s", client, " (locked out)" if locked else "")
                raise ApiError(HTTPStatus.UNAUTHORIZED, "Wrong password.")
            self.auth.record_success(client)
            LOG.info("Signed in from %s", client)
            self._json(HTTPStatus.OK, {"authenticated": True},
                       headers={"Set-Cookie": self._cookie(self.auth.issue_token(), SESSION_TTL_SECONDS)})
            return True

        if method == "POST" and path == "/api/logout":
            self._require_csrf_header()
            self._json(HTTPStatus.OK, {"authenticated": False},
                       headers={"Set-Cookie": self._cookie("", 0)})
            return True

        if not path.startswith("/api/"):
            return False

        # Everything below requires a signed-in session.
        self._require_auth()

        if method == "GET" and path == "/api/config":
            self._json(HTTPStatus.OK, {"publicUrl": self.public_url})
            return True

        if method == "GET" and path == "/api/boxes":
            self._json(HTTPStatus.OK, {"boxes": self.store.list_boxes()})
            return True

        if method == "POST" and path == "/api/boxes":
            box = self.store.create_box(validate_box_input(self._read_json()))
            LOG.info("Created box %d (%s) from %s", box["number"], box["id"], client)
            self._json(HTTPStatus.CREATED, {"box": box})
            return True

        match = BOX_PATH_RE.match(path)
        if match and method == "PUT":
            data = self._read_json()
            expected_rev = data.get("rev") if isinstance(data, dict) else None
            if expected_rev is not None and not is_int(expected_rev):
                raise ApiError(HTTPStatus.BAD_REQUEST, "rev must be a whole number.")
            box = self.store.update_box(match.group(1), validate_box_input(data), expected_rev)
            LOG.info("Updated box %d (%s) from %s", box["number"], box["id"], client)
            self._json(HTTPStatus.OK, {"box": box})
            return True

        if match and method == "DELETE":
            self._require_csrf_header()
            self.store.delete_box(match.group(1))
            LOG.info("Deleted box %s from %s", match.group(1), client)
            self._json(HTTPStatus.OK, {"deleted": match.group(1)})
            return True

        if method == "POST" and path == "/api/boxes/move":
            data = self._read_json()
            ids = data.get("ids") if isinstance(data, dict) else None
            if (not isinstance(ids, list) or not ids or len(ids) > MAX_BULK_IDS
                    or not all(isinstance(i, str) and BOX_ID_RE.match(i) for i in ids)):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Select at least one box to move.")
            location = clean_str(data.get("location"), MAX_LOCATION)
            if not location:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Enter the location to move the boxes to.")
            moved = self.store.move_boxes(ids, location)
            LOG.info("Moved %d box(es) to %r from %s", moved, location, client)
            self._json(HTTPStatus.OK, {"moved": moved})
            return True

        if method == "GET" and path == "/api/export":
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            body = json.dumps({"version": 2, "exported": now_iso(), "boxes": self.store.list_boxes()},
                              indent=2).encode("utf-8")
            self._send(HTTPStatus.OK, body, headers={
                "Content-Disposition": 'attachment; filename="packing-tracker-%s.json"' % stamp})
            return True

        if method == "POST" and path == "/api/import":
            boxes, skipped = sanitize_import(self._read_json())
            self.store.replace_all(boxes)
            LOG.info("Imported %d box(es) (%d skipped) from %s", len(boxes), skipped, client)
            self._json(HTTPStatus.OK, {"imported": len(boxes), "skipped": skipped})
            return True

        if path.startswith("/api/boxes"):
            raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed.")
        return False


def _id_from_env(name, default):
    raw = os.environ.get(name, "").strip() or str(default)
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError("%s must be a number between 1 and 65535 (got %r)." % (name, raw))
    return int(raw)


def drop_privileges(data_dir):
    """If running as root, give data_dir to PUID:PGID and switch to that user for good.

    Never serves as root. Does nothing when already running as a non-root user.
    Raises ValueError for bad PUID/PGID and OSError if the switch fails.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    uid = _id_from_env("PUID", 99)
    gid = _id_from_env("PGID", 100)
    os.makedirs(data_dir, exist_ok=True)
    os.lchown(data_dir, uid, gid)
    for root, dirs, files in os.walk(data_dir):
        for name in dirs + files:
            os.lchown(os.path.join(root, name), uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() == 0 or os.getegid() == 0:
        raise OSError("Could not stop running as root.")
    LOG.info("Running as user %d, group %d", uid, gid)
    return True


def make_server(host, port, data_dir, password, public_url=""):
    os.makedirs(data_dir, exist_ok=True)
    store = Store(os.path.join(data_dir, "tracker.db"))
    auth = Auth(password, store.get_or_create_secret())
    handler = type("BoundTrackerHandler", (TrackerHandler,),
                   {"store": store, "auth": auth, "public_url": parse_public_url(public_url)})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")

    password = os.environ.get("TRACKER_PASSWORD", "")
    if len(password) < 8:
        LOG.error("TRACKER_PASSWORD must be set to at least 8 characters. Refusing to start.")
        return 2

    try:
        port = int(os.environ.get("PORT", "5639"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        LOG.error("PORT must be a number between 1 and 65535.")
        return 2

    try:
        public_url = parse_public_url(os.environ.get("PUBLIC_URL", ""))
    except ValueError as err:
        LOG.error("%s Refusing to start.", err)
        return 2

    host = os.environ.get("BIND_ADDRESS", "0.0.0.0")
    data_dir = os.environ.get("DATA_DIR", "/data")

    try:
        drop_privileges(data_dir)
    except ValueError as err:
        LOG.error("%s Refusing to start.", err)
        return 2
    except OSError as err:
        LOG.error("Could not prepare %s or switch away from root: %s", data_dir, err)
        return 1

    try:
        server = make_server(host, port, data_dir, password, public_url)
    except (OSError, sqlite3.Error, RuntimeError) as err:
        LOG.error("Could not start: %s", err)
        return 1

    def stop(signum, _frame):
        LOG.info("Received signal %d, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    LOG.info("Packing Tracker listening on %s:%d (data in %s)", host, port, data_dir)
    if public_url:
        LOG.info("QR codes will point to %s", public_url)
    else:
        LOG.info("PUBLIC_URL not set; QR codes will use the address the page was opened from")
    server.serve_forever()
    server.server_close()
    LOG.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
