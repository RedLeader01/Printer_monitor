"""A nyomtatókezelés tesztjei: tároló, belépés, webes végpontok, parancssor, előzmények törlés után."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from printer_monitor import auth as authmod
from printer_monitor import config, store as storemod
from printer_monitor.cli import main as cli_main
from printer_monitor.history import History
from printer_monitor.monitor import Monitor
from printer_monitor.store import DuplicateError, PrinterStore, ValidationError, parse_bulk
from printer_monitor.web import make_server

from .fake_agent import FakeAgent
from .test_history import ALERT, Clock

ROOT = config.ROOT
authmod.PBKDF2_ITERATIONS = 1000        # a tesztekben ne lassítson a jelszókivonat


class Silent:
    telegram_enabled = False

    def send(self, text):
        pass


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PrinterStore(Path(self.tmp.name) / "p.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_starts_empty(self):
        self.assertEqual(self.store.list(), [])

    def test_add_list_remove_keep_order(self):
        a = self.store.add("B-2", "10.0.0.2")
        self.store.add("A-1", "10.0.0.1")
        self.assertEqual([p["name"] for p in self.store.list()], ["B-2", "A-1"])
        self.assertEqual(self.store.remove(a["id"]), "B-2")
        self.assertIsNone(self.store.remove(a["id"]))
        self.assertEqual(self.store.count(), 1)

    def test_duplicates_are_rejected(self):
        self.store.add("X", "10.0.0.1")
        with self.assertRaises(DuplicateError):
            self.store.add("x", "10.0.0.9")          # név: kis/nagybetű nem számít
        with self.assertRaises(DuplicateError):
            self.store.add("Y", "10.0.0.1")          # ugyanaz az IP

    def test_bad_values_are_rejected(self):
        for name, ip in (("", "10.0.0.1"), ("*", "10.0.0.1"), ("N" * 41, "10.0.0.1"),
                         ("A", "0.0.0.0"), ("A", "255.255.255.255"), ("A", "224.0.0.1"),
                         ("A", "::1"), ("A", "nem cím!"), ("A", ""), ("A", "10.0.0.1; rm")):
            with self.assertRaises(ValidationError, msg=(name, ip)):
                self.store.add(name, ip)

    def test_hostname_is_accepted(self):
        self.assertEqual(self.store.add("H", "nyomtato1.example.local")["ip"], "nyomtato1.example.local")

    def test_survives_reopen(self):
        self.store.add("A", "10.0.0.1")
        self.store.close()
        self.store = PrinterStore(Path(self.tmp.name) / "p.db")
        self.assertEqual(self.store.count(), 1)

    def test_limit(self):
        old = storemod.MAX_PRINTERS
        storemod.MAX_PRINTERS = 2
        try:
            self.store.add("A", "10.0.0.1")
            self.store.add("B", "10.0.0.2")
            with self.assertRaises(ValidationError):
                self.store.add("C", "10.0.0.3")
        finally:
            storemod.MAX_PRINTERS = old

    def test_parse_bulk(self):
        items, errors = parse_bulk("A = 10.0.0.1\n# megjegyzés\n\nB;10.0.0.2\nC\t10.0.0.3\nD 10.0.0.4\nrossz\r\n")
        self.assertEqual([(n, i) for _, n, i in items],
                         [("A", "10.0.0.1"), ("B", "10.0.0.2"), ("C", "10.0.0.3"), ("D", "10.0.0.4")])
        self.assertEqual([no for no, _ in errors], [7])
        self.assertEqual(parse_bulk(None), ([], []))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PrinterStore(Path(self.tmp.name) / "p.db")
        self.clock = Clock()
        self.auth = authmod.Auth(self.store, True, 8, clock=self.clock)
        authmod.set_admin_password(self.store, "Admin", "titok-jelszo")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cookie(self, token):
        return {"Host": "gep:8080", "Cookie": "pm_session=" + token}

    def test_password_hash(self):
        h = authmod.hash_password("valami-hosszu")
        self.assertTrue(authmod.verify_password("valami-hosszu", h))
        self.assertFalse(authmod.verify_password("mas", h))
        self.assertNotIn("valami", h)
        self.assertFalse(authmod.verify_password("x", "szemét"))

    def test_password_rules(self):
        for bad in ("rovid", "", None, "A" * 300):
            with self.assertRaises(authmod.PasswordError):
                authmod.set_admin_password(self.store, "u", bad)
        with self.assertRaises(authmod.PasswordError):
            authmod.set_admin_password(self.store, "ugyanaz-nev", "ugyanaz-nev")

    def test_login_and_session(self):
        token, csrf, user, _ = self.auth.login("10.1.1.1", "admin", "titok-jelszo")      # név: kis/nagybetű mindegy
        p = self.auth.identify("10.1.1.1", self.cookie(token))
        self.assertTrue(p.is_admin)
        self.assertEqual(p.kind, "admin")
        self.assertTrue(self.auth.check_csrf(p, csrf))
        self.assertFalse(self.auth.check_csrf(p, "hamis"))
        self.assertFalse(self.auth.check_csrf(p, None))

    def test_wrong_password_and_unknown_user_look_the_same(self):
        with self.assertRaises(authmod.AuthError) as a:
            self.auth.login("10.1.1.1", "Admin", "rossz-jelszo")
        with self.assertRaises(authmod.AuthError) as b:
            self.auth.login("10.1.1.1", "nincs", "rossz-jelszo")
        self.assertEqual((a.exception.status, a.exception.message), (b.exception.status, b.exception.message))

    def test_throttle_per_ip(self):
        for _ in range(authmod.LOGIN_MAX_FAILS):
            with self.assertRaises(authmod.AuthError):
                self.auth.login("10.1.1.1", "Admin", "rossz-jelszo")
        with self.assertRaises(authmod.AuthError) as e:
            self.auth.login("10.1.1.1", "Admin", "titok-jelszo")      # a jó jelszó sem megy át tiltás alatt
        self.assertEqual(e.exception.status, 429)
        self.assertTrue(e.exception.retry_after)
        self.auth.login("10.1.1.2", "Admin", "titok-jelszo")          # másik gépet nem érint
        self.clock.advance(authmod.LOGIN_WINDOW + 1)
        self.auth.login("10.1.1.1", "Admin", "titok-jelszo")          # az idő letelte után újra lehet

    def test_parallel_wrong_logins_cannot_beat_the_throttle(self):
        results = []

        def attempt():
            try:
                self.auth.login("10.9.9.9", "Admin", "rossz-jelszo")
            except authmod.AuthError as e:
                results.append(e.status)
        threads = [threading.Thread(target=attempt) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(results.count(401), authmod.LOGIN_MAX_FAILS)
        self.assertEqual(len(results), 30)

    def test_odd_input_does_not_crash(self):
        with self.assertRaises(authmod.AuthError):
            self.auth.login("10.1.1.1", "\ud800", "titok-jelszo")
        p = self.auth.identify("10.1.1.1", self.cookie(self.auth.login("10.1.1.1", "Admin", "titok-jelszo")[0]))
        self.assertFalse(self.auth.check_csrf(p, "árvíztűrő"))

    def test_session_expires(self):
        token = self.auth.login("10.1.1.1", "Admin", "titok-jelszo")[0]
        self.clock.advance(8 * 3600 + 1)
        self.assertFalse(self.auth.identify("10.1.1.1", self.cookie(token)).is_admin)

    def test_password_change_or_removal_ends_sessions(self):
        token = self.auth.login("10.1.1.1", "Admin", "titok-jelszo")[0]
        authmod.set_admin_password(self.store, "Admin", "uj-titok-jelszo")
        self.assertFalse(self.auth.identify("10.1.1.1", self.cookie(token)).is_admin)
        token = self.auth.login("10.1.1.1", "Admin", "uj-titok-jelszo")[0]
        self.store.remove_admin("Admin")
        self.assertFalse(self.auth.identify("10.1.1.1", self.cookie(token)).is_admin)

    def test_logout(self):
        token = self.auth.login("10.1.1.1", "Admin", "titok-jelszo")[0]
        self.auth.logout(token)
        self.assertFalse(self.auth.identify("10.1.1.1", self.cookie(token)).is_admin)

    def test_localhost_trust(self):
        self.assertEqual(self.auth.identify("127.0.0.1", {"Host": "localhost:8080"}).kind, "local")
        self.assertEqual(self.auth.identify("::1", {"Host": "[::1]:8080"}).kind, "local")
        self.assertEqual(self.auth.identify("::ffff:127.0.0.1", {"Host": "127.0.0.1"}).kind, "local")
        # nem bízunk: távoli cím, idegen Host (DNS-rebinding), proxyra utaló fejléc
        self.assertEqual(self.auth.identify("10.1.1.1", {"Host": "localhost"}).kind, "anon")
        self.assertEqual(self.auth.identify("127.0.0.1", {"Host": "gep.local"}).kind, "anon")
        self.assertEqual(self.auth.identify("127.0.0.1", {"Host": "localhost", "X-Forwarded-For": "10.1.1.1"}).kind, "anon")
        self.assertEqual(self.auth.identify("127.0.0.1", {}).kind, "anon")

    def test_localhost_trust_can_be_turned_off(self):
        a = authmod.Auth(self.store, False, 8)
        self.assertEqual(a.identify("127.0.0.1", {"Host": "localhost"}).kind, "anon")

    def test_same_origin(self):
        self.assertTrue(authmod.same_origin({"Host": "a:1"}))
        self.assertTrue(authmod.same_origin({"Host": "a:1", "Origin": "http://a:1"}))
        self.assertTrue(authmod.same_origin({"Host": "a:1", "Sec-Fetch-Site": "same-origin"}))
        self.assertFalse(authmod.same_origin({"Host": "a:1", "Origin": "http://rossz.example"}))
        self.assertFalse(authmod.same_origin({"Host": "a:1", "Sec-Fetch-Site": "cross-site"}))


class WebManageBase(unittest.TestCase):
    trust = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PrinterStore(Path(self.tmp.name) / "p.db")
        authmod.set_admin_password(self.store, "Admin", "titok-jelszo")
        self.agent = FakeAgent().start()
        cfg = config.Config()
        cfg.snmp_port = self.agent.port
        self.monitor = Monitor(cfg, Silent(), store=self.store)
        self.auth = authmod.Auth(self.store, self.trust, 8)
        self.server = make_server("127.0.0.1", 0, self.monitor, ROOT / "web", self.auth)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.agent.stop()
        self.store.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, host=None, raw=None):
        h = {"Host": host or "localhost:%d" % self.port}
        if self.cookie:
            h["Cookie"] = self.cookie
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True)
        for k, v in h.items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(data or b"")))
        conn.endheaders(data or b"")
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        out = (resp.status, json.loads(text) if text.startswith("{") else text, resp)
        conn.close()
        return out

    def login(self, user="Admin", password="titok-jelszo", **kw):
        st, data, resp = self.req("POST", "/api/login", {"username": user, "password": password}, **kw)
        if st == 200:
            self.cookie = resp.getheader("Set-Cookie").split(";")[0]
            self.csrf = data["csrf"]
        return st, data, resp


class LocalAdminTests(WebManageBase):
    """A futtató gépről (localhost) nem kell belépni."""

    def test_me_is_local_admin(self):
        st, me, _ = self.req("GET", "/api/me")
        self.assertEqual((st, me["admin"], me["kind"]), (200, True, "local"))

    def test_add_and_delete(self):
        st, me, _ = self.req("GET", "/api/me")
        self.csrf = me["csrf"]
        st, data, _ = self.req("POST", "/api/printers", {"name": "T1", "ip": "127.0.0.1"})
        self.assertEqual(st, 201)
        pid = data["printer"]["id"]
        self.assertEqual([p["name"] for p in self.req("GET", "/api")[1]["printers"]], ["T1"])
        self.assertEqual(self.req("POST", "/api/printers", {"name": "t1", "ip": "127.0.0.2"})[0], 409)
        self.assertEqual(self.req("POST", "/api/printers", {"name": "T2", "ip": "0.0.0.0"})[0], 400)
        self.assertEqual(self.req("DELETE", "/api/printers/%d" % pid)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/printers/%d" % pid)[0], 404)
        self.assertEqual(self.req("GET", "/api")[1]["printers"], [])

    def test_missing_or_wrong_csrf_is_refused(self):
        body = {"name": "T1", "ip": "127.0.0.1"}
        self.assertEqual(self.req("POST", "/api/printers", body)[0], 403)
        self.csrf = "hamis"
        self.assertEqual(self.req("POST", "/api/printers", body)[0], 403)
        self.assertEqual(self.store.count(), 0)

    def test_cross_site_request_is_refused(self):
        self.csrf = self.req("GET", "/api/me")[1]["csrf"]
        body = {"name": "T1", "ip": "127.0.0.1"}
        for h in ({"Origin": "http://rossz.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.req("POST", "/api/printers", body, headers=h)[0], 403)
        self.assertEqual(self.store.count(), 0)

    def test_foreign_host_header_is_not_local(self):
        # DNS-rebinding: a böngésző idegen néven éri el a helyi szervert -> nem admin
        st, me, _ = self.req("GET", "/api/me", host="evil.example:%d" % self.port)
        self.assertFalse(me["admin"])

    def test_bad_requests(self):
        self.csrf = self.req("GET", "/api/me")[1]["csrf"]
        self.assertEqual(self.req("POST", "/api/printers", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/printers", raw=b"nem json", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.req("POST", "/api/printers", raw=b"[1]", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.req("POST", "/api/printers", raw=b"x" * (70 * 1024), headers={"Content-Type": "application/json"})[0], 413)
        self.assertEqual(self.req("POST", "/api/printers", {"name": 5, "ip": ["a"]})[0], 400)
        self.assertEqual(self.req("POST", "/api/nincs", {})[0], 404)
        self.assertEqual(self.req("DELETE", "/api/printers/abc")[0], 404)

    def test_bulk_add_with_partial_errors(self):
        self.csrf = self.req("GET", "/api/me")[1]["csrf"]
        text = "A = 127.0.0.1\nB = 127.0.0.2\nrossz sor ???\nA = 127.0.0.3\nC = 127.0.0.2\n"
        st, data, _ = self.req("POST", "/api/printers/bulk", {"text": text})
        self.assertEqual(st, 200)
        self.assertEqual([p["name"] for p in data["added"]], ["A", "B"])
        self.assertEqual(sorted(e["line"] for e in data["errors"]), [3, 4, 5])
        self.assertFalse(data["ok"])
        self.assertEqual(self.req("POST", "/api/printers/bulk", {"text": "   "})[0], 400)

    def test_bulk_30_printers(self):
        self.csrf = self.req("GET", "/api/me")[1]["csrf"]
        text = "\n".join("GEP-%02d = 10.56.63.%d" % (i, i) for i in range(1, 31))
        st, data, _ = self.req("POST", "/api/printers/bulk", {"text": text})
        self.assertEqual((st, len(data["added"]), data["errors"]), (200, 30, []))

    def test_security_headers(self):
        st, _, resp = self.req("GET", "/api/me")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))


class RemoteLoginTests(WebManageBase):
    """Nem helyi gép (itt: trust_localhost = nem): belépés admin fiókkal."""
    trust = False

    def test_anonymous_cannot_change_anything(self):
        st, me, _ = self.req("GET", "/api/me")
        self.assertFalse(me["admin"])
        self.assertIsNone(me["csrf"])
        self.assertEqual(self.req("POST", "/api/printers", {"name": "T", "ip": "10.0.0.1"})[0], 401)
        self.assertEqual(self.req("DELETE", "/api/printers/1")[0], 401)
        self.assertEqual(self.req("POST", "/api/printers/bulk", {"text": "A = 10.0.0.1"})[0], 401)

    def test_login_then_manage_then_logout(self):
        st, data, resp = self.login()
        self.assertEqual(st, 200)
        cookie = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertTrue(self.req("GET", "/api/me")[1]["admin"])
        self.assertEqual(self.req("POST", "/api/printers", {"name": "T", "ip": "127.0.0.1"})[0], 201)
        self.assertEqual(self.req("POST", "/api/logout")[0], 200)
        self.assertFalse(self.req("GET", "/api/me")[1]["admin"])
        self.assertEqual(self.req("POST", "/api/printers", {"name": "U", "ip": "127.0.0.2"})[0], 401)

    def test_wrong_password_and_throttle(self):
        self.assertEqual(self.login(password="rossz-jelszo")[0], 401)
        for _ in range(authmod.LOGIN_MAX_FAILS):
            self.login(password="rossz-jelszo")
        st, data, resp = self.login()
        self.assertEqual(st, 429)
        self.assertTrue(resp.getheader("Retry-After"))

    def test_login_from_other_site_is_refused(self):
        st, _, _ = self.login(headers={"Origin": "http://rossz.example"})
        self.assertEqual(st, 403)

    def test_view_is_open_without_login(self):
        self.assertEqual(self.req("GET", "/api")[0], 200)
        self.assertEqual(self.req("GET", "/api/history")[0], 200)

    def test_management_off_without_auth(self):
        server = make_server("127.0.0.1", 0, Monitor(config.Config(), Silent()), ROOT / "web")
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/printers", b"{}", {"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


class MonitorListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PrinterStore(Path(self.tmp.name) / "p.db")
        self.agent = FakeAgent().start()
        cfg = config.Config()
        cfg.snmp_port = self.agent.port
        self.clock = Clock()
        self.history = History(Path(self.tmp.name) / "h.db", 365, clock=self.clock)
        self.monitor = Monitor(cfg, Silent(), history=self.history, store=self.store)

    def tearDown(self):
        self.history.close()
        self.agent.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_new_printer_is_polled_and_removed_one_disappears(self):
        p = self.monitor.add_printer("T1", "127.0.0.1")
        self.monitor.poll_once()
        snap = self.monitor.snapshot()
        self.assertEqual([(s["name"], s["online"]) for s in snap], [("T1", True)])
        self.assertEqual(snap[0]["id"], p["id"])
        self.assertEqual(self.monitor.remove_printer(p["id"]), "T1")
        self.assertEqual(self.monitor.snapshot(), [])

    def test_removed_printer_closes_its_open_episodes_but_keeps_history(self):
        self.history.record("T1", True, ALERT, [])
        self.monitor._apply_list([("T1", "127.0.0.1")])
        self.clock.advance(600)
        self.monitor._apply_list([])                    # törölték
        eps = self.history.episodes("T1", 7)
        self.assertEqual(len(eps), 1)
        self.assertIsNotNone(eps[0]["ended"])

    def test_late_poll_result_of_removed_printer_is_discarded(self):
        p = self.monitor.add_printer("T1", "127.0.0.1")
        self.monitor.remove_printer(p["id"])
        self.monitor._poll_one(("T1", "127.0.0.1"))
        self.assertEqual(self.monitor.state, {})
        self.assertEqual(self.history.episodes(None, 7), [])

    def test_deleted_printer_history_depends_on_the_time_window(self):
        self.history.record("T1", True, ALERT, [])
        self.clock.advance(60)
        self.history.close_printer("T1")
        self.clock.advance(14 * 86400)                  # két hete töröltem
        self.assertEqual(self.history.episodes("T1", 7), [])
        self.assertEqual(self.history.printer_names(7), [])
        self.assertEqual(len(self.history.episodes("T1", 30)), 1)
        self.assertEqual(self.history.printer_names(30), ["T1"])

    def test_stale_poll_of_old_ip_is_dropped_after_readd(self):
        p = self.monitor.add_printer("T1", "127.0.0.1")
        self.monitor.remove_printer(p["id"])
        self.monitor.add_printer("T1", "127.0.0.2")
        self.monitor._poll_one(("T1", "127.0.0.1"))          # a régi IP-re indult lekérdezés késve ér véget
        self.assertNotIn("T1", self.monitor.state)

    def test_bulk_add_polls_everything(self):
        added, errors = self.monitor.add_printers("A = 127.0.0.1\nB = 127.0.0.2\nA = 127.0.0.3")
        self.assertEqual((len(added), len(errors)), (2, 1))
        self.assertEqual([n for _, n, _ in self.monitor.printer_list()], ["A", "B"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = t / "c.ini"
        # abszolút útvonal: a ROOT / abszolút útvonal az abszolút útvonalat adja (Windowson és Linuxon is)
        self.db = t / "p.db"
        self.cfg.write_text("[storage]\nprinters_file = %s\n" % self.db, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, pw=()):
        answers = iter(pw)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--config", str(self.cfg)] + list(args), lambda prompt: next(answers))
        return code, buf.getvalue()

    def test_printers_add_list_remove(self):
        self.assertEqual(self.run_cli("printers", "add", "HAT-1", "10.0.0.1")[0], 0)
        self.assertEqual(self.run_cli("printers", "add", "HAT-1", "10.0.0.2")[0], 1)
        code, out = self.run_cli("printers", "list")
        self.assertIn("HAT-1", out)
        self.assertEqual(self.run_cli("printers", "remove", "hat-1")[0], 0)
        self.assertEqual(self.run_cli("printers", "remove", "hat-1")[0], 1)

    def test_printers_import_file(self):
        f = Path(self.tmp.name) / "lista.txt"
        f.write_text("﻿A = 10.0.0.1\nB = 10.0.0.2\nrossz\n", encoding="utf-8")
        code, out = self.run_cli("printers", "import", str(f))
        self.assertEqual(code, 1)                       # volt hibás sor, de a jók felvételre kerültek
        self.assertIn("2 nyomtató felvéve", out)
        self.assertIn("3. sor", out)

    def test_admin_set_list_remove(self):
        self.assertEqual(self.run_cli("admin", "set", "Red", pw=["jelszo-1234", "jelszo-1234"])[0], 0)
        self.assertIn("Red", self.run_cli("admin", "list")[1])
        self.assertEqual(self.run_cli("admin", "set", "Red", pw=["jelszo-1234", "mas-jelszo-1"])[0], 1)
        self.assertEqual(self.run_cli("admin", "set", "Red", pw=["rovid", "rovid"])[0], 1)
        self.assertEqual(self.run_cli("admin", "remove", "Red")[0], 0)
        self.assertEqual(self.run_cli("admin", "remove", "Red")[0], 1)


if __name__ == "__main__":
    unittest.main()
