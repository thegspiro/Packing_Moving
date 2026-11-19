"""Integration tests for the Packing Tracker server.

Run from the project root:  python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import server  # noqa: E402

PASSWORD = "correct-horse-battery"
CSRF = {"X-Requested-With": "packing-tracker"}


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name, PASSWORD)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, raw_body=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(CSRF)
        payload = raw_body
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        parsed = None
        if res.getheader("Content-Type", "").startswith("application/json") and data:
            parsed = json.loads(data)
        return res, parsed

    def login(self, password=PASSWORD):
        res, data = self.request("POST", "/api/login", {"password": password})
        if res.status == 200:
            self.cookie = res.getheader("Set-Cookie").split(";")[0]
        return res, data

    def add_box(self, number, **extra):
        body = {"number": number, "location": "Old apartment", "contents": ["Plates"]}
        body.update(extra)
        return self.request("POST", "/api/boxes", body)


class AuthTests(ServerTestCase):
    def test_page_and_health_need_no_login(self):
        res, _ = self.request("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.getheader("Content-Type"))
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        res, data = self.request("GET", "/api/health")
        self.assertEqual((res.status, data), (200, {"ok": True}))

    def test_api_requires_login(self):
        res, _ = self.request("GET", "/api/boxes")
        self.assertEqual(res.status, 401)
        res, _ = self.request("GET", "/api/export")
        self.assertEqual(res.status, 401)

    def test_wrong_password_rejected_and_locked_out(self):
        for _ in range(server.LOGIN_MAX_FAILURES):
            res, _ = self.login("wrong-password")
            self.assertEqual(res.status, 401)
        res, data = self.login(PASSWORD)
        self.assertEqual(res.status, 429)
        self.assertIn("Try again", data["error"])

    def test_login_sets_httponly_strict_cookie(self):
        res, data = self.login()
        self.assertEqual(res.status, 200)
        self.assertTrue(data["authenticated"])
        cookie = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        res, data = self.request("GET", "/api/session")
        self.assertTrue(data["authenticated"])

    def test_tampered_cookie_rejected(self):
        self.login()
        name, value = self.cookie.split("=", 1)
        self.cookie = name + "=" + value[:-1] + ("0" if value[-1] != "0" else "1")
        res, _ = self.request("GET", "/api/boxes")
        self.assertEqual(res.status, 401)

    def test_password_change_invalidates_sessions(self):
        self.login()
        secret = self.httpd.RequestHandlerClass.store.get_or_create_secret()
        other = server.Auth("a-different-password", secret)
        token = self.cookie.split("=", 1)[1]
        self.assertFalse(other.verify_token(token))
        self.assertTrue(self.httpd.RequestHandlerClass.auth.verify_token(token))

    def test_writes_require_csrf_header(self):
        self.login()
        res, _ = self.request("POST", "/api/boxes", {"number": 1, "location": "X"},
                              headers={"X-Requested-With": ""})
        self.assertEqual(res.status, 403)
        res, _ = self.request("POST", "/api/boxes", raw_body=b'{"number":1,"location":"X"}',
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(res.status, 415)

    def test_logout_clears_cookie(self):
        self.login()
        res, _ = self.request("POST", "/api/logout")
        self.assertEqual(res.status, 200)
        self.assertIn("Max-Age=0", res.getheader("Set-Cookie"))


class BoxTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_create_and_list_with_new_fields(self):
        res, data = self.add_box(1, room="Kitchen", fragile=True, openFirst=True, notes="Heavy")
        self.assertEqual(res.status, 201)
        box = data["box"]
        self.assertEqual(box["room"], "Kitchen")
        self.assertTrue(box["fragile"])
        self.assertTrue(box["openFirst"])
        self.assertEqual(box["rev"], 1)
        res, data = self.request("GET", "/api/boxes")
        self.assertEqual([b["number"] for b in data["boxes"]], [1])

    def test_room_is_optional(self):
        res, data = self.add_box(1)
        self.assertEqual(res.status, 201)
        self.assertEqual(data["box"]["room"], "")
        self.assertFalse(data["box"]["openFirst"])

    def test_validation(self):
        bad = [
            {"number": 0, "location": "X"},
            {"number": True, "location": "X"},
            {"number": "5", "location": "X"},
            {"number": 1, "location": "   "},
            {"number": 1, "location": "X", "contents": "Plates"},
            {"number": 1, "location": "X", "contents": [1, 2]},
            {"number": 1, "location": "X", "fragile": "yes"},
            {"number": 1, "location": "X", "room": 5},
            {"number": 1, "location": "X", "contents": ["a"] * (server.MAX_ITEMS + 1)},
        ]
        for body in bad:
            res, _ = self.request("POST", "/api/boxes", body)
            self.assertEqual(res.status, 400, body)

    def test_strings_are_cleaned_and_truncated(self):
        res, data = self.add_box(1, location="Old\x00apt\n", room="R" * 500,
                                 contents=["  a  ", "", "b\tc"])
        box = data["box"]
        self.assertEqual(box["location"], "Old apt")
        self.assertEqual(len(box["room"]), server.MAX_ROOM)
        self.assertEqual(box["contents"], ["a", "b c"])

    def test_duplicate_number_rejected(self):
        self.add_box(1)
        res, data = self.add_box(1)
        self.assertEqual(res.status, 409)
        self.assertIn("already exists", data["error"])

    def test_update_with_revision_check(self):
        _, data = self.add_box(1)
        box = data["box"]
        path = "/api/boxes/" + box["id"]
        res, data = self.request("PUT", path, {"number": 1, "location": "Truck", "room": "Office", "rev": 1})
        self.assertEqual(res.status, 200)
        self.assertEqual(data["box"]["rev"], 2)
        self.assertEqual(data["box"]["room"], "Office")
        # A second device still holding rev 1 must not overwrite.
        res, data = self.request("PUT", path, {"number": 1, "location": "Garage", "rev": 1})
        self.assertEqual(res.status, 409)
        self.assertIn("another device", data["error"])

    def test_update_cannot_steal_number(self):
        self.add_box(1)
        _, data = self.add_box(2)
        res, _ = self.request("PUT", "/api/boxes/" + data["box"]["id"], {"number": 1, "location": "X"})
        self.assertEqual(res.status, 409)

    def test_update_and_delete_missing_box(self):
        res, _ = self.request("PUT", "/api/boxes/nope", {"number": 1, "location": "X"})
        self.assertEqual(res.status, 404)
        res, _ = self.request("DELETE", "/api/boxes/nope")
        self.assertEqual(res.status, 404)

    def test_delete(self):
        _, data = self.add_box(1)
        res, _ = self.request("DELETE", "/api/boxes/" + data["box"]["id"])
        self.assertEqual(res.status, 200)
        _, data = self.request("GET", "/api/boxes")
        self.assertEqual(data["boxes"], [])

    def test_bulk_move(self):
        ids = [self.add_box(n)[1]["box"]["id"] for n in (1, 2, 3)]
        res, data = self.request("POST", "/api/boxes/move", {"ids": ids[:2], "location": "Truck"})
        self.assertEqual((res.status, data["moved"]), (200, 2))
        _, data = self.request("GET", "/api/boxes")
        self.assertEqual([b["location"] for b in data["boxes"]], ["Truck", "Truck", "Old apartment"])
        res, _ = self.request("POST", "/api/boxes/move", {"ids": [], "location": "Truck"})
        self.assertEqual(res.status, 400)
        res, _ = self.request("POST", "/api/boxes/move", {"ids": ids, "location": ""})
        self.assertEqual(res.status, 400)

    def test_import_old_browser_format_and_export(self):
        self.add_box(99)
        legacy = {"version": 1, "boxes": [
            {"id": "b1", "number": 1, "location": "Old apartment", "contents": ["Plates"],
             "notes": "", "fragile": True, "updated": "2026-11-11T00:00:00Z"},
            {"id": "b2", "number": 1, "location": "Duplicate number"},
            {"id": "bad id!", "number": 2, "location": "Truck", "contents": ["Lamp", 5]},
            {"number": -3, "location": "Invalid"},
            "not an object",
        ]}
        res, data = self.request("POST", "/api/import", legacy)
        self.assertEqual(res.status, 200)
        self.assertEqual((data["imported"], data["skipped"]), (2, 3))
        _, data = self.request("GET", "/api/boxes")
        numbers = [b["number"] for b in data["boxes"]]
        self.assertEqual(numbers, [1, 2])  # replace, not merge: box 99 is gone
        self.assertEqual(data["boxes"][0]["room"], "")
        self.assertFalse(data["boxes"][0]["openFirst"])
        self.assertEqual(data["boxes"][1]["contents"], ["Lamp"])
        self.assertNotEqual(data["boxes"][1]["id"], "bad id!")

        res, data = self.request("GET", "/api/export")
        self.assertEqual(res.status, 200)
        self.assertIn("attachment", res.getheader("Content-Disposition"))
        res2, _ = self.request("POST", "/api/import", data)
        self.assertEqual(res2.status, 200)

    def test_import_rejects_garbage(self):
        res, _ = self.request("POST", "/api/import", {"nope": True})
        self.assertEqual(res.status, 400)
        res, _ = self.request("POST", "/api/import", raw_body=b"{not json",
                              headers={"Content-Type": "application/json"})
        self.assertEqual(res.status, 400)

    def test_oversized_body_rejected(self):
        res, _ = self.request("POST", "/api/import", raw_body=b"x",
                              headers={"Content-Type": "application/json",
                                       "Content-Length": str(server.MAX_BODY_BYTES + 1)})
        self.assertEqual(res.status, 413)

    def test_concurrent_creates_never_duplicate(self):
        results = []

        def worker():
            res, _ = self.add_box(7)
            results.append(res.status)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201] + [409] * 7)

    def test_sql_injection_is_inert(self):
        res, data = self.add_box(1, location="x'); DROP TABLE boxes;--")
        self.assertEqual(res.status, 201)
        res, data = self.request("GET", "/api/boxes")
        self.assertEqual(data["boxes"][0]["location"], "x'); DROP TABLE boxes;--")


class PersistenceTests(unittest.TestCase):
    def test_data_and_sessions_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "tracker.db")
            store = server.Store(db)
            secret = store.get_or_create_secret()
            store.create_box({"number": 5, "location": "Truck", "room": "Kitchen", "contents": ["Pan"],
                              "notes": "", "fragile": False, "openFirst": True})
            token = server.Auth(PASSWORD, secret).issue_token()

            reopened = server.Store(db)
            self.assertEqual(reopened.get_or_create_secret(), secret)
            self.assertEqual(reopened.list_boxes()[0]["room"], "Kitchen")
            self.assertTrue(server.Auth(PASSWORD, secret).verify_token(token))

            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(server.MIGRATIONS))
            conn.close()

    def test_refuses_newer_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "tracker.db")
            server.Store(db)
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version = 99")
            conn.close()
            with self.assertRaises(RuntimeError):
                server.Store(db)


class ConfigTests(unittest.TestCase):
    def test_refuses_to_start_without_password(self):
        old = os.environ.pop("TRACKER_PASSWORD", None)
        try:
            os.environ["TRACKER_PASSWORD"] = "short"
            self.assertEqual(server.main(), 2)
            del os.environ["TRACKER_PASSWORD"]
            self.assertEqual(server.main(), 2)
        finally:
            if old is not None:
                os.environ["TRACKER_PASSWORD"] = old


if __name__ == "__main__":
    unittest.main()
