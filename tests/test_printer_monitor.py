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

from .fake_agent import FakeAgent


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
        self.assertEqual(issues_of(offline, 20), {"OFFLINE"})
        online = {"online": True, "supplies": [{"name": "Toner", "pct": 5}, {"name": "Dob", "pct": 80},
                                               {"name": "Henger", "pct": None}],
                  "alerts": [{"sev": "KRITIKUS", "text": "Printhead error"}]}
        self.assertEqual(issues_of(online, 20), {"Toner: 5%", "KRITIKUS: Printhead error"})


class ConfigTests(unittest.TestCase):
    def _load(self, text, encoding="utf-8"):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.ini"
            p.write_text(text, encoding=encoding)
            return config.load(p)

    def test_valid_config_keeps_order_and_names(self):
        cfg = self._load("[general]\ncommunity = titok\npoll_seconds = 15\nwarn_pct = 25\n"
                         "[web]\nport = 9000\n"
                         "[printers]\nHAT-KO-4 = 10.0.0.4   # megjegyzés\nHAT-BU-1 = 10.0.0.1\n")
        self.assertEqual(cfg.community, "titok")
        self.assertEqual(cfg.poll_seconds, 15)
        self.assertEqual(cfg.warn_pct, 25)
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.printers, [("HAT-KO-4", "10.0.0.4"), ("HAT-BU-1", "10.0.0.1")])

    def test_notepad_bom_is_accepted(self):
        cfg = self._load("[printers]\nA = 10.0.0.1\n", encoding="utf-8-sig")
        self.assertEqual(cfg.printers, [("A", "10.0.0.1")])

    def test_too_small_poll_interval_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._load("[general]\npoll_seconds = 1\n[printers]\nA = 10.0.0.1\n")

    def test_text_instead_of_number_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._load("[web]\nport = nyolcvan\n[printers]\nA = 10.0.0.1\n")

    def test_no_printers_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._load("[printers]\n")

    def test_example_config_is_valid(self):
        cfg = config.load(ROOT / "config.example.ini")
        self.assertTrue(cfg.printers)


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
