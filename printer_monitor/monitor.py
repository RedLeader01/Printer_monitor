"""A háttérben futó lekérdező ciklus és a hibafigyelés."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import printers

OFFLINE_KEY = "offline"


def issues_of(r, warn_pct):
    """A nyomtató jelenlegi problémái: {kulcs: (fajta, szöveg)}.

    A kulcs azonosítja a problémát (az értesítések és az előzmények ebből döntik el, hogy
    ugyanaz-e még), ezért nem tartalmazhat változó értéket: a kellékanyag kulcsa csak a nevét
    tartalmazza, a százalék a szövegben van, így egy szintcsökkenés nem számít új problémának.
    """
    if not r["online"]:
        return {OFFLINE_KEY: ("offline", "OFFLINE")}
    out = {}
    for s in r["supplies"]:
        if s["pct"] is not None and s["pct"] < warn_pct:
            out["supply:" + s["name"]] = ("supply_low", "{}: {}%".format(s["name"], s["pct"]))
    for a in r["alerts"]:
        out["alert:{}:{}".format(a["sev"], a["text"])] = ("alert", "{}: {}".format(a["sev"], a["text"]))
    return out


class Monitor:
    def __init__(self, cfg, notifier, history=None):
        self.cfg = cfg
        self.notifier = notifier
        self.history = history
        self.state = {}
        self.lock = threading.Lock()
        self._last_issues = {}

    def _poll_one(self, item):
        name, ip = item
        try:
            r = printers.poll(name, ip, self.cfg.community, self.cfg.snmp_port)
        except Exception as e:      # váratlan hiba se állítsa le a figyelést
            r = {"name": name, "ip": ip, "online": False, "status": "-", "supplies": [],
                 "alerts": [], "info": {}, "error": "belső hiba: {}".format(e)[:200],
                 "time": time.strftime("%H:%M:%S")}
        found = issues_of(r, self.cfg.warn_pct)
        with self.lock:
            self.state[name] = r
            before = self._last_issues.get(name, {})
            if r["online"]:
                now = found
            else:
                # Elérhetetlen nyomtatónál nem tudjuk, megszűnt-e a többi hiba, ezért azokat
                # fennállónak tekintjük. Így az elérhetőség villanása után nem érkezik újra
                # ugyanaz az értesítés.
                now = dict(before)
                now.update(found)
            self._last_issues[name] = now
        new = sorted(text for key, (_, text) in now.items() if key not in before)
        if new:
            self.notifier.send("{} ({}):\n{}".format(name, ip, "\n".join(new)))
        if self.history is not None:
            try:
                self.history.record(name, r["online"], now, r["supplies"])
            except Exception as e:      # az előzmények hibája ne akassza meg a figyelést
                print("Előzmény-mentési hiba:", e)

    def poll_once(self):
        """Egyszerre kérdezi le az összes nyomtatót (párhuzamosan)."""
        workers = max(1, min(8, len(self.cfg.printers)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self._poll_one, self.cfg.printers))
        if self.history is not None:
            try:
                self.history.cycle_done()
            except Exception as e:
                print("Előzmény-mentési hiba:", e)

    def _loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print("Lekérdezési hiba:", e)
            time.sleep(self.cfg.poll_seconds)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def snapshot(self):
        """A nyomtatók utolsó ismert állapota a config.ini sorrendjében."""
        with self.lock:
            state = dict(self.state)
        return [state.get(n) or {"name": n, "ip": ip, "pending": True}
                for n, ip in self.cfg.printers]
