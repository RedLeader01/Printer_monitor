"""A háttérben futó lekérdező ciklus és a hibafigyelés."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import printers


def issues_of(r, warn_pct):
    """A nyomtató jelenlegi problémái szövegként (az értesítések ebből döntik el, mi új)."""
    out = set()
    if not r["online"]:
        out.add("OFFLINE")
        return out
    for s in r["supplies"]:
        if s["pct"] is not None and s["pct"] < warn_pct:
            out.add("{}: {}%".format(s["name"], s["pct"]))
    for a in r["alerts"]:
        out.add("{}: {}".format(a["sev"], a["text"]))
    return out


class Monitor:
    def __init__(self, cfg, notifier):
        self.cfg = cfg
        self.notifier = notifier
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
        now = issues_of(r, self.cfg.warn_pct)
        with self.lock:
            self.state[name] = r
            before = self._last_issues.get(name, set())
            self._last_issues[name] = now
        new = now - before
        if new:
            self.notifier.send("{} ({}):\n{}".format(name, ip, "\n".join(sorted(new))))

    def poll_once(self):
        """Egyszerre kérdezi le az összes nyomtatót (párhuzamosan)."""
        workers = max(1, min(8, len(self.cfg.printers)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self._poll_one, self.cfg.printers))

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
