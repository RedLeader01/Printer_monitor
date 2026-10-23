"""Tesztek hamis nyomtatóval. Futtatás a projekt mappájából:  python -m unittest -v"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from printer_monitor import config, printers
from printer_monitor.config import ROOT
from printer_monitor.monitor import Monitor, issues_of
from printer_monitor.web import make_server

from .fake_agent import DEFAULT_DATA, FakeAgent


class StubNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


class PollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = FakeAgent().start()
        cls.r = printers.poll("T", "127.0.0.1", "public", cls.agent.port, timeout=0.5)

    @classmethod
    def tearDownClass(cls):
        cls.agent.stop()

    def test_online_and_status(self):
        self.assertTrue(self.r["online"])
        self.assertEqual(self.r["status"], "készenlét")

    def test_supplies_are_cleaned_and_scaled(self):
        s = {x["name"]: x for x in self.r["supplies"]}
        self.assertEqual(set(s), {"Festékkazetta", "Dobkazetta", "Átvivőhenger"})
        self.assertEqual(s["Festékkazetta"]["pct"], 15)
        self.assertEqual(s["Festékkazetta"]["pn"], "106R03396")
        self.assertEqual(s["Dobkazetta"]["pct"], 90)
        self.assertIsNone(s["Átvivőhenger"]["pct"])          # -3: "van még", pontos szint nincs
        self.assertIn("van még", s["Átvivőhenger"]["note"])

    def test_alerts(self):
        self.assertEqual(self.r["alerts"], [{"sev": "figyelmeztetés", "text": "Cartridge low"}])

    def test_ignored_alerts_are_left_out(self):
        r = printers.poll("T", "127.0.0.1", "public", self.agent.port, timeout=0.5,
                          ignore_alerts=["CARTRIDGE low"])        # kis/nagybetű nem számít
        self.assertEqual(r["alerts"], [])
        self.assertTrue(r["online"])
        self.assertEqual(len(r["supplies"]), 3)                   # a többi adat változatlan

    def test_sleep_mode_message_is_ignored_by_default(self):
        data = dict(DEFAULT_DATA)
        data["1.3.6.1.2.1.43.18.1.1.8.1.1"] = "016-425 A készülék szendergő módban van. Nincs szükség beavatkozásra."
        agent = FakeAgent(data).start()
        try:
            shown = printers.poll("T", "127.0.0.1", "public", agent.port, timeout=0.5)
            hidden = printers.poll("T", "127.0.0.1", "public", agent.port, timeout=0.5,
                                   ignore_alerts=config.Config().ignore_alerts)
        finally:
            agent.stop()
        self.assertEqual(len(shown["alerts"]), 1)                 # szűrő nélkül megjelenne
        self.assertEqual(hidden["alerts"], [])

    def test_info(self):
        i = self.r["info"]
        self.assertEqual(i["model"], "Lexmark MX722adhe")
        self.assertEqual(i["serial"], "5051234ABC")
        self.assertEqual(i["uptime"], 123456)
        self.assertIn("MXTGM.050.041", i["descr"])

    def test_manual_tray_empty_is_not_a_warning(self):
        trays = {t["name"]: t for t in self.r["info"]["inputs"]}
        self.assertEqual(trays["Tray 1"]["state"], "ÜRES")
        self.assertTrue(trays["Tray 1"]["warn"])             # a fő tálca üres: figyelmeztetés
        self.assertEqual(trays["MP Tray"]["state"], "ÜRES")
        self.assertFalse(trays["MP Tray"]["warn"])           # a kézi adagoló mindig üres lehet
        self.assertFalse(trays["Tray 2"]["warn"])

    def test_full_output_tray_is_a_warning(self):
        out = self.r["info"]["outputs"][0]
        self.assertEqual(out["state"], "TELE")
        self.assertTrue(out["warn"])

    def test_wrong_community_means_offline(self):
        r = printers.poll("T", "127.0.0.1", "rossz", self.agent.port, timeout=0.2)
        self.assertFalse(r["online"])
        self.assertIn("nincs SNMP válasz", r["error"])


class HelperTests(unittest.TestCase):
    def test_split_supply_name(self):
        self.assertEqual(printers.split_supply_name("Festékkazetta, PN 106R03396;SN8012"),
                         ("Festékkazetta", "106R03396"))
        self.assertEqual(printers.split_supply_name("Black Cartridge"), ("Black Cartridge", None))

    def test_issues_of(self):
        offline = {"online": False}
        self.assertEqual(issues_of(offline, 20), {"offline": ("offline", "OFFLINE")})
        online = {"online": True, "supplies": [{"name": "Toner", "pct": 5}, {"name": "Dob", "pct": 80},
                                               {"name": "Henger", "pct": None}],
                  "alerts": [{"sev": "KRITIKUS", "text": "Printhead error"}]}
        self.assertEqual({k: v[1] for k, v in issues_of(online, 20).items()},
                         {"supply:Toner": "Toner: 5%",
                          "alert:KRITIKUS:Printhead error": "KRITIKUS: Printhead error"})

    def test_issue_key_does_not_depend_on_the_level(self):
        online = {"online": True, "supplies": [{"name": "Toner", "pct": 5}], "alerts": []}
        before = set(issues_of(online, 20))
        online["supplies"][0]["pct"] = 4
        self.assertEqual(set(issues_of(online, 20)), before)


class ConfigTests(unittest.TestCase):
    def _load(self, text, encoding="utf-8"):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.ini"
            p.write_text(text, encoding=encoding)
            return config.load(p)

    def test_valid_config_keeps_order_and_names(self):
        cfg = self._load("[general]\ncommunity = titok\npoll_seconds = 15\nwarn_pct = 25\n"
                         "[web]\nport = 9000\n"
                         "[printers]\nIroda-4 = 10.0.0.4   # megjegyzés\nIroda-1 = 10.0.0.1\n")
        self.assertEqual(cfg.community, "titok")
        self.assertEqual(cfg.poll_seconds, 15)
        self.assertEqual(cfg.warn_pct, 25)
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.printers, [("Iroda-4", "10.0.0.4"), ("Iroda-1", "10.0.0.1")])

    def test_notepad_bom_is_accepted(self):
        cfg = self._load("[printers]\nA = 10.0.0.1\n", encoding="utf-8-sig")
        self.assertEqual(cfg.printers, [("A", "10.0.0.1")])

    def test_too_small_poll_interval_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._load("[general]\npoll_seconds = 1\n[printers]\nA = 10.0.0.1\n")

    def test_text_instead_of_number_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._load("[web]\nport = nyolcvan\n[printers]\nA = 10.0.0.1\n")

    def test_printers_section_is_optional(self):
        # A nyomtatókat a weboldalon veszik fel, a config.ini-ben a [printers] szakasz már nem kötelező.
        self.assertEqual(self._load("[general]\ncommunity = x\n").printers, [])
        self.assertEqual(self._load("[printers]\n").printers, [])

    def test_bad_printers_line_is_still_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._load("[printers]\nA =\n")

    def test_admin_and_storage_options(self):
        cfg = self._load("")
        self.assertTrue(cfg.trust_localhost)
        self.assertEqual(cfg.session_hours, 8)
        self.assertEqual(cfg.printers_path, ROOT / "printers.db")
        cfg = self._load("[admin]\ntrust_localhost = no\nsession_hours = 2\n[storage]\nprinters_file = lista.db\n")
        self.assertFalse(cfg.trust_localhost)
        self.assertEqual(cfg.session_hours, 2)
        self.assertEqual(cfg.printers_path, ROOT / "lista.db")
        for text in ("[admin]\nsession_hours = 0\n", "[admin]\nsession_hours = sok\n", "[admin]\ntrust_localhost = talán\n"):
            with self.assertRaises(config.ConfigError, msg=text):
                self._load(text)

    def test_example_config_is_valid(self):
        cfg = config.load(ROOT / "config.example.ini")
        self.assertEqual(cfg.printers, [])                  # a példában nincs nyomtató: a weboldalon kell felvenni
        self.assertTrue(cfg.trust_localhost)
        self.assertTrue(cfg.history_enabled)
        self.assertEqual(cfg.history_keep_days, 365)

    def test_history_defaults_and_options(self):
        cfg = self._load("[printers]\nA = 10.0.0.1\n")
        self.assertTrue(cfg.history_enabled)
        self.assertEqual(cfg.history_path, ROOT / "history.db")
        cfg = self._load("[history]\nenabled = nem\nkeep_days = 30\nfile = elozmeny.db\n"
                         "[printers]\nA = 10.0.0.1\n")
        self.assertFalse(cfg.history_enabled)
        self.assertEqual(cfg.history_keep_days, 30)
        self.assertEqual(cfg.history_path, ROOT / "elozmeny.db")

    def test_ignore_alerts_option(self):
        self.assertEqual(self._load("[printers]\nA = 10.0.0.1\n").ignore_alerts, ["016-425"])      # alapérték
        cfg = self._load("[general]\nignore_alerts = 016-425, Szendergő ,\n[printers]\nA = 10.0.0.1\n")
        self.assertEqual(cfg.ignore_alerts, ["016-425", "Szendergő"])
        cfg = self._load("[general]\nignore_alerts =\n[printers]\nA = 10.0.0.1\n")
        self.assertEqual(cfg.ignore_alerts, [])                                                    # üres: semmi sem marad ki

    def test_bad_history_values_are_rejected(self):
        for text in ("[history]\nenabled = talán\n", "[history]\nkeep_days = 0\n",
                     "[history]\nkeep_days = sok\n"):
            with self.assertRaises(config.ConfigError, msg=text):
                self._load(text + "[printers]\nA = 10.0.0.1\n")


class MonitorAndWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = FakeAgent().start()
        cfg = config.Config()
        cfg.printers = [("T1", "127.0.0.1")]
        cfg.snmp_port = cls.agent.port
        cls.notifier = StubNotifier()
        cls.monitor = Monitor(cfg, cls.notifier)
        cls.monitor.poll_once()
        cls.server = make_server("127.0.0.1", 0, cls.monitor, ROOT / "web")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.agent.stop()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), body

    def test_notification_only_for_new_problems(self):
        self.assertEqual(len(self.notifier.messages), 1)        # az első körben jelez
        self.assertIn("Festékkazetta: 15%", self.notifier.messages[0])
        self.monitor.poll_once()
        self.assertEqual(len(self.notifier.messages), 1)        # változatlan állapotnál nem ismétel

    def test_api(self):
        status, ctype, body = self._get("/api")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["settings"]["warn_pct"], 20)
        self.assertEqual(data["printers"][0]["name"], "T1")
        self.assertTrue(data["printers"][0]["online"])

    def test_static_files(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("Nyomtatók", body.decode("utf-8"))
        status, ctype, _ = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        status, ctype, _ = self._get("/style.css")
        self.assertIn("text/css", ctype)

    def test_cannot_leave_web_folder(self):
        for path in ("/../config.example.ini", "/%2e%2e/config.example.ini",
                     "/..%2fconfig.example.ini", "//etc/passwd", "/nincs-ilyen.html"):
            status, _, body = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"[printers]", body, path)


if __name__ == "__main__":
    unittest.main()
