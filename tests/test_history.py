"""Az előzmények (SQLite) tesztjei. Hamis órát és hamis lekérdezési eredményeket használnak."""
import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from printer_monitor import config
from printer_monitor.config import ROOT
from printer_monitor.history import ALL_PRINTERS, GAP_KIND, History
from printer_monitor.monitor import Monitor
from printer_monitor.web import make_server

DAY = 86400
LOW = {"supply:Festék": ("supply_low", "Festék: 15%")}
ALERT = {"alert:figyelmeztetés:Tálca üres": ("alert", "figyelmeztetés: Tálca üres")}
OFFLINE = {"offline": ("offline", "OFFLINE")}


class Clock:
    def __init__(self, t=1_700_000_000):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "h.db"
        self.clock = Clock()
        self.opened = []

    def tearDown(self):
        for h in self.opened:
            try:
                h.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def open(self, keep_days=365):
        h = History(self.path, keep_days, clock=self.clock)
        self.opened.append(h)
        return h


class EpisodeTests(HistoryCase):
    def test_episode_has_start_and_end(self):
        h = self.open()
        h.record("P1", True, LOW, [])
        started = self.clock.t
        ep, = h.episodes()
        self.assertEqual((ep["printer"], ep["kind"], ep["detail"]), ("P1", "supply_low", "Festék: 15%"))
        self.assertEqual(ep["started"], started)
        self.assertIsNone(ep["ended"])                      # még tart

        self.clock.advance(600)
        h.record("P1", True, LOW, [])                       # változatlan: nincs új epizód
        self.assertEqual(len(h.episodes()), 1)

        self.clock.advance(600)
        h.record("P1", True, {}, [])                        # megszűnt
        ep, = h.episodes()
        self.assertEqual(ep["ended"] - ep["started"], 1200)

    def test_changing_level_updates_the_open_episode(self):
        h = self.open()
        h.record("P1", True, LOW, [])
        h.record("P1", True, {"supply:Festék": ("supply_low", "Festék: 14%")}, [])
        ep, = h.episodes()
        self.assertEqual(ep["detail"], "Festék: 14%")

    def test_a_problem_that_comes_back_is_a_new_episode(self):
        h = self.open()
        h.record("P1", True, ALERT, [])
        self.clock.advance(60)
        h.record("P1", True, {}, [])
        self.clock.advance(60)
        h.record("P1", True, ALERT, [])
        eps = h.episodes()
        self.assertEqual(len(eps), 2)
        self.assertEqual([e["ended"] is None for e in eps], [True, False])   # a legfrissebb elöl

    def test_offline_does_not_close_other_open_problems(self):
        h = self.open()
        h.record("P1", True, {**LOW, **ALERT}, [])
        self.clock.advance(60)
        h.record("P1", False, OFFLINE, [])                  # nem tudjuk, megszűnt-e a többi
        by_kind = {e["kind"]: e for e in h.episodes()}
        self.assertIsNone(by_kind["supply_low"]["ended"])
        self.assertIsNone(by_kind["alert"]["ended"])
        self.assertIsNone(by_kind["offline"]["ended"])

        self.clock.advance(120)
        h.record("P1", True, {**LOW, **ALERT}, [])          # újra elérhető, minden ugyanúgy fennáll
        by_kind = {e["kind"]: e for e in h.episodes()}
        self.assertEqual(by_kind["offline"]["ended"] - by_kind["offline"]["started"], 120)
        self.assertIsNone(by_kind["supply_low"]["ended"])
        self.assertEqual(len(h.episodes()), 3)              # a kellék és a hiba nem nyílt újra

    def test_printers_are_independent(self):
        h = self.open()
        h.record("P1", True, LOW, [])
        h.record("P2", True, LOW, [])
        h.record("P1", True, {}, [])
        by_printer = {e["printer"]: e for e in h.episodes()}
        self.assertIsNotNone(by_printer["P1"]["ended"])
        self.assertIsNone(by_printer["P2"]["ended"])

    def test_filter_by_printer_and_days(self):
        h = self.open()
        h.record("P1", True, LOW, [])
        h.record("P2", True, ALERT, [])
        self.assertEqual([e["printer"] for e in h.episodes("P1")], ["P1"])
        self.clock.advance(3 * DAY)
        h.record("P1", True, {}, [])                        # P1 epizódja most zárult
        self.clock.advance(10 * DAY)
        # P1 régen zárult epizódja kiesik a 7 napos ablakból, a P2 még tartó epizódja bent marad
        self.assertEqual([e["printer"] for e in h.episodes(days=7)], ["P2"])
        self.assertEqual({e["printer"] for e in h.episodes(days=30)}, {"P1", "P2"})


class ReadingTests(HistoryCase):
    def test_levels_are_stored_only_when_they_change(self):
        h = self.open()
        for pct in (50, 50, 50, 49, 49, 48):
            h.record("P1", True, {}, [{"name": "Festék", "pct": pct}])
            self.clock.advance(30)
        self.assertEqual([p for _, p in h.readings("P1", "Festék")], [50, 49, 48])

    def test_unknown_levels_and_offline_are_skipped(self):
        h = self.open()
        h.record("P1", True, {}, [{"name": "Henger", "pct": None}])
        h.record("P1", False, OFFLINE, [{"name": "Festék", "pct": 10}])
        self.assertEqual(h.readings("P1", "Henger"), [])
        self.assertEqual(h.readings("P1", "Festék"), [])

    def test_a_replacement_is_visible_in_the_levels(self):
        h = self.open()
        for pct in (6, 5, 100):
            h.record("P1", True, {}, [{"name": "Festék", "pct": pct}])
            self.clock.advance(3600)
        self.assertEqual([p for _, p in h.readings("P1", "Festék")], [6, 5, 100])


class RestartTests(HistoryCase):
    def test_open_episode_continues_after_restart(self):
        h = self.open()
        h.start(30)
        h.record("P1", True, LOW, [])
        h.close()
        self.clock.advance(20)
        h2 = self.open()
        h2.start(30)
        h2.record("P1", True, LOW, [])                      # ugyanaz a probléma: nem nyílik újra
        self.assertEqual(len(h2.episodes()), 1)
        h2.record("P1", True, {}, [])
        self.assertIsNotNone(h2.episodes()[0]["ended"])

    def test_short_restart_is_not_a_gap(self):
        h = self.open()
        h.start(30)
        h.cycle_done()
        h.close()
        self.clock.advance(60)
        h2 = self.open()
        h2.start(30)
        self.assertEqual(h2.episodes(), [])

    def test_long_pause_is_recorded_as_a_gap(self):
        h = self.open()
        h.start(30)
        h.cycle_done()
        last = self.clock.t
        h.close()
        self.clock.advance(2 * 3600)
        h2 = self.open()
        h2.start(30)
        gap, = h2.episodes()
        self.assertEqual((gap["kind"], gap["printer"]), (GAP_KIND, ALL_PRINTERS))
        self.assertEqual((gap["started"], gap["ended"]), (last, self.clock.t))
        # egy szűrt nézetben is megjelenik, mert minden nyomtatót érint
        self.assertEqual(len(h2.episodes("P1")), 1)

    def test_first_start_has_no_gap(self):
        h = self.open()
        h.start(30)
        self.assertEqual(h.episodes(), [])


class PruneTests(HistoryCase):
    def test_old_closed_episodes_and_levels_are_deleted_open_ones_stay(self):
        h = self.open(keep_days=30)
        h.record("P1", True, LOW, [{"name": "Festék", "pct": 10}])
        h.record("P2", True, ALERT, [])
        self.clock.advance(1 * DAY)
        h.record("P1", True, {}, [])                        # P1 epizódja lezárul
        self.clock.advance(40 * DAY)
        h.prune()
        self.assertEqual([e["printer"] for e in h.episodes(days=3650)], ["P2"])    # a tartó megmarad
        self.assertEqual(h.readings("P1", "Festék", days=3650), [])


class ConcurrencyTests(HistoryCase):
    def test_parallel_records_from_many_threads(self):
        h = self.open()
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    h.record("P%d" % n, True, LOW if i % 2 == 0 else {}, [{"name": "F", "pct": i}])
                    h.episodes()
            except Exception as e:      # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(h.episodes(days=1)), 8 * 13)    # nyomtatónként 13 nyitás (i = 0, 2, ..., 24)


def result(online=True, supplies=(), alerts=()):
    return {"name": "T1", "ip": "10.0.0.1", "online": online, "status": "készenlét", "error": "",
            "supplies": [dict(s, pn=None, note="") for s in supplies],
            "alerts": [dict(a) for a in alerts], "info": {}, "time": "00:00:00"}


class StubNotifier:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


class MonitorHistoryTests(HistoryCase):
    def setUp(self):
        super().setUp()
        cfg = config.Config()
        cfg.printers = [("T1", "10.0.0.1")]
        self.cfg = cfg
        self.notifier = StubNotifier()
        self.history = self.open()
        self.monitor = Monitor(cfg, self.notifier, self.history)

    def poll(self, res):
        with mock.patch("printer_monitor.printers.poll", return_value=res):
            self.monitor.poll_once()
        self.clock.advance(30)

    def test_problems_are_recorded_with_start_and_end(self):
        self.poll(result(supplies=[{"name": "Festék", "pct": 15}],
                         alerts=[{"sev": "figyelmeztetés", "text": "Tálca üres"}]))
        kinds = sorted(e["kind"] for e in self.history.episodes())
        self.assertEqual(kinds, ["alert", "supply_low"])
        self.poll(result(supplies=[{"name": "Festék", "pct": 90}]))
        self.assertTrue(all(e["ended"] is not None for e in self.history.episodes()))

    def test_level_drop_does_not_repeat_the_notification(self):
        self.poll(result(supplies=[{"name": "Festék", "pct": 15}]))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("Festék: 15%", self.notifier.messages[0])
        for pct in (14, 13, 12):
            self.poll(result(supplies=[{"name": "Festék", "pct": pct}]))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(self.history.episodes()[0]["detail"], "Festék: 12%")   # de az előzmény követi

    def test_offline_blip_does_not_repeat_old_notifications(self):
        low = result(supplies=[{"name": "Festék", "pct": 15}])
        self.poll(low)
        self.poll(result(online=False))                      # elérhetetlen
        self.assertEqual(len(self.notifier.messages), 2)     # + az OFFLINE értesítés
        self.assertIn("OFFLINE", self.notifier.messages[1])
        self.poll(low)                                       # visszajött
        self.assertEqual(len(self.notifier.messages), 2)     # a kevés festék nem jelez újra
        eps = {e["kind"]: e for e in self.history.episodes()}
        self.assertEqual(len(eps), 2)
        self.assertIsNotNone(eps["offline"]["ended"])
        self.assertIsNone(eps["supply_low"]["ended"])

    def test_a_problem_that_ends_and_returns_notifies_again(self):
        low = result(supplies=[{"name": "Festék", "pct": 15}])
        self.poll(low)
        self.poll(result(supplies=[{"name": "Festék", "pct": 100}]))     # kicserélték
        self.poll(low)
        self.assertEqual(len(self.notifier.messages), 2)

    def test_history_failure_does_not_stop_monitoring(self):
        with mock.patch.object(self.history, "record", side_effect=RuntimeError("lemez tele")), \
                contextlib.redirect_stdout(io.StringIO()):          # a hibaüzenet ne zavarja a kimenetet
            self.poll(result(supplies=[{"name": "Festék", "pct": 15}]))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertTrue(self.monitor.snapshot()[0]["online"])


class HistoryApiTests(HistoryCase):
    def setUp(self):
        super().setUp()
        cfg = config.Config()
        cfg.printers = [("T1", "10.0.0.1"), ("T2", "10.0.0.2")]
        self.history = self.open()
        self.monitor = Monitor(cfg, StubNotifier(), self.history)
        self.history.record("T1", True, LOW, [])
        self.history.record("T2", True, ALERT, [])
        self.server = make_server("127.0.0.1", 0, self.monitor, ROOT / "web")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body.decode("utf-8"))

    def test_all_episodes(self):
        status, data = self.get("/api/history")
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["printers"], ["T1", "T2"])
        self.assertEqual({e["printer"] for e in data["episodes"]}, {"T1", "T2"})

    def test_filter_by_printer(self):
        _, data = self.get("/api/history?printer=T2&days=30")
        self.assertEqual([e["printer"] for e in data["episodes"]], ["T2"])
        self.assertEqual(data["days"], 30)

    def test_bad_or_extreme_days_are_handled(self):
        self.assertEqual(self.get("/api/history?days=abc")[1]["days"], 7)
        self.assertEqual(self.get("/api/history?days=0")[1]["days"], 1)
        self.assertEqual(self.get("/api/history?days=99999")[1]["days"], 3650)

    def test_sql_injection_attempt_is_harmless(self):
        status, data = self.get("/api/history?printer=" + "x'%20OR%201=1%20--")
        self.assertEqual(status, 200)
        self.assertEqual(data["episodes"], [])

    def test_disabled_history(self):
        self.monitor.history = None
        status, data = self.get("/api/history")
        self.assertEqual(status, 200)
        self.assertFalse(data["enabled"])
        self.assertEqual(data["episodes"], [])

    def test_history_page_is_served(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/history.html")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
