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


class PublicUrlTests(unittest.TestCase):
    def test_valid_urls_are_normalised(self):
        self.assertEqual(server.parse_public_url(""), "")
        self.assertEqual(server.parse_public_url("  "), "")
        self.assertEqual(server.parse_public_url("http://192.168.1.50:5639/"), "http://192.168.1.50:5639")
        self.assertEqual(server.parse_public_url("https://tower.local"), "https://tower.local")
        self.assertEqual(server.parse_public_url("http://tower:5639/tracker/"), "http://tower:5639/tracker")

    def test_invalid_urls_rejected(self):
        for bad in ["192.168.1.50:5639", "ftp://host", "javascript:alert(1)", "http://",
                    "http://user:pw@host", "http://host/?x=1", "http://host/#box", "http://host:99999",
                    "http://ho st", "http://host/\x00", "http://" + "a" * 300]:
            with self.assertRaises(ValueError, msg=bad):
                server.parse_public_url(bad)


class QrSupportTests(ServerTestCase):
    def test_qr_library_served_as_javascript(self):
        res, _ = self.request("GET", "/qrcode.js")
        self.assertEqual(res.status, 200)
        self.assertIn("text/javascript", res.getheader("Content-Type"))

    def test_only_listed_static_files_served(self):
        for path in ["/THIRD_PARTY_LICENSES.md", "/server.py", "/../server.py", "/static/qrcode.js"]:
            res, _ = self.request("GET", path)
            self.assertEqual(res.status, 404, path)

    def test_config_requires_login_and_defaults_to_empty(self):
        res, _ = self.request("GET", "/api/config")
        self.assertEqual(res.status, 401)
        self.login()
        res, data = self.request("GET", "/api/config")
        self.assertEqual((res.status, data), (200, {"publicUrl": ""}))


class QrConfiguredTests(ServerTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = server.make_server("127.0.0.1", 0, self.tmp.name, PASSWORD,
                                        public_url="http://192.168.1.50:5639/")
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.cookie = None

    def test_config_returns_normalised_public_url(self):
        self.login()
        _, data = self.request("GET", "/api/config")
        self.assertEqual(data, {"publicUrl": "http://192.168.1.50:5639"})


APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
DROP_SCRIPT = """
import os, sys, json
sys.path.insert(0, sys.argv[1])
import server
changed = server.drop_privileges(sys.argv[2])
open(os.path.join(sys.argv[2], "written-after-drop"), "w").close()
print(json.dumps({"changed": changed, "uid": os.getuid(), "gid": os.getgid(),
                  "groups": os.getgroups()}))
"""


class PrivilegeTests(unittest.TestCase):
    def run_drop(self, data_dir, env_extra):
        import subprocess
        env = dict(os.environ)
        env.update(env_extra)
        return subprocess.run([sys.executable, "-c", DROP_SCRIPT, APP_DIR, data_dir],
                              capture_output=True, text=True, env=env, timeout=30)

    def test_rejects_bad_ids(self):
        for name, value in [("PUID", "0"), ("PUID", "abc"), ("PGID", "70000"), ("PGID", "-1")]:
            env = {"PUID": "99", "PGID": "100", name: value}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                with self.assertRaises(ValueError, msg=(name, value)):
                    server._id_from_env(name, 1)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs to run as root")
    def test_root_hands_data_dir_to_puid_and_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            existing = os.path.join(data, "tracker.db")
            open(existing, "w").close()
            res = self.run_drop(data, {"PUID": "4321", "PGID": "4322"})
            self.assertEqual(res.returncode, 0, res.stderr)
            out = json.loads(res.stdout)
            self.assertEqual((out["changed"], out["uid"], out["gid"], out["groups"]), (True, 4321, 4322, []))
            for path in (data, existing, os.path.join(data, "written-after-drop")):
                st = os.stat(path)
                self.assertEqual((st.st_uid, st.st_gid), (4321, 4322), path)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs to run as root")
    def test_root_creates_missing_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            data = os.path.join(tmp, "new", "data")
            res = self.run_drop(data, {"PUID": "99", "PGID": "100"})
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(os.stat(data).st_uid, 99)

    def test_non_root_is_left_alone(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            import subprocess
            with tempfile.TemporaryDirectory() as tmp:
                os.chmod(tmp, 0o777)
                script = ("import os, sys; os.setgid(100); os.setuid(99); sys.path.insert(0, sys.argv[1]); "
                          "import server; print(server.drop_privileges(sys.argv[2]), os.getuid())")
                res = subprocess.run([sys.executable, "-c", script, APP_DIR, tmp],
                                     capture_output=True, text=True, timeout=30,
                                     env=dict(os.environ, PUID="4321"))
                self.assertEqual(res.stdout.split(), ["False", "99"], res.stderr)
        else:
            self.assertFalse(server.drop_privileges(tempfile.gettempdir()))


class UnraidTemplateTests(unittest.TestCase):
    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def setUp(self):
        import xml.etree.ElementTree as ET
        self.root = ET.parse(os.path.join(self.ROOT, "unraid", "packing-tracker.xml")).getroot()
        self.configs = {c.get("Target"): c for c in self.root.findall("Config")}

    def test_points_at_published_image_and_repo_files(self):
        self.assertEqual(self.root.findtext("Repository"), "ghcr.io/thegspiro/packing_moving:latest")
        base = "https://raw.githubusercontent.com/thegspiro/Packing_Moving/main/"
        self.assertEqual(self.root.findtext("TemplateURL"), base + "unraid/packing-tracker.xml")
        self.assertEqual(self.root.findtext("Icon"), base + "unraid/icon.png")
        self.assertTrue(os.path.isfile(os.path.join(self.ROOT, "unraid", "icon.png")))
        with open(os.path.join(self.ROOT, ".github", "workflows", "docker.yml")) as fh:
            self.assertIn("IMAGE: ghcr.io/thegspiro/packing_moving", fh.read())

    def test_port_and_data_path_match_the_image(self):
        port = self.configs["5639"]
        self.assertEqual((port.get("Type"), port.get("Mode"), port.text), ("Port", "tcp", "5639"))
        self.assertIn("[PORT:5639]", self.root.findtext("WebUI"))
        data = self.configs["/data"]
        self.assertEqual((data.get("Type"), data.text), ("Path", "/mnt/user/appdata/packing-tracker"))

    def test_variables_are_ones_the_server_reads(self):
        with open(os.path.join(self.ROOT, "app", "server.py")) as fh:
            source = fh.read()
        variables = [t for t, c in self.configs.items() if c.get("Type") == "Variable"]
        self.assertEqual(sorted(variables), ["LOG_LEVEL", "PGID", "PUBLIC_URL", "PUID", "TRACKER_PASSWORD"])
        for name in variables:
            self.assertIn('"%s"' % name, source, name)
        password = self.configs["TRACKER_PASSWORD"]
        self.assertEqual((password.get("Mask"), password.get("Required")), ("true", "true"))
        self.assertFalse((password.text or "").strip(), "template must not ship a password")


class ConfigTests(unittest.TestCase):
    def test_refuses_to_start_with_bad_public_url(self):
        saved = {k: os.environ.get(k) for k in ("TRACKER_PASSWORD", "PUBLIC_URL")}
        try:
            os.environ["TRACKER_PASSWORD"] = PASSWORD
            os.environ["PUBLIC_URL"] = "not-a-url"
            self.assertEqual(server.main(), 2)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

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
