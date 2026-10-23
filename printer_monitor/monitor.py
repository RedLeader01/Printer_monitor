"""A háttérben futó lekérdező ciklus és a hibafigyelés."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import printers
from .store import StoreError, parse_bulk

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
    """A nyomtatók listája a `store`-ból jön (printers.db), és futás közben is változhat.
    Tároló nélkül a régi módon a cfg.printers listát figyeli (ezt használják a tesztek)."""

    def __init__(self, cfg, notifier, history=None, store=None):
        self.cfg = cfg
        self.notifier = notifier
        self.history = history
        self.store = store
        self.state = {}
        self.lock = threading.Lock()
        self._last_issues = {}
        self._active = {}           # név -> ip: a most figyelt nyomtatók
        self._list_lock = threading.RLock()     # a lista olvasása és alkalmazása egyben történjen

    # ------------------------------------------------------------------ lista
    def printer_list(self):
        """[(id, név, ip), ...]. Az id None, ha nincs tároló."""
        if self.store is not None:
            return [(p["id"], p["name"], p["ip"]) for p in self.store.list()]
        return [(None, n, ip) for n, ip in self.cfg.printers]

    def _apply_list(self, items):
        """Rögzíti a figyelt nyomtatók aktuális listáját. Ami eltűnt belőle (törölték), annak az
        állapota törlődik, a még tartó eseményei pedig lezárulnak az előzményekben."""
        new = dict(items)
        with self.lock:
            for name in [n for n in self._active if n not in new]:
                self.state.pop(name, None)
                self._last_issues.pop(name, None)
                if self.history is not None:
                    try:
                        self.history.close_printer(name)
                    except Exception as e:
                        print("Előzmény-mentési hiba:", e)
            self._active = new

    def refresh_list(self):
        with self._list_lock:
            self._apply_list([(n, ip) for _, n, ip in self.printer_list()])

    def add_printer(self, name, ip):
        """Új nyomtató felvétele (StoreError-t dobhat), és azonnali első lekérdezése."""
        with self._list_lock:
            p = self.store.add(name, ip)
            self.refresh_list()
        threading.Thread(target=self._poll_one, args=((p["name"], p["ip"]),), daemon=True).start()
        return p

    def add_printers(self, text):
        """Több nyomtató egyszerre (soronként 'Név = IP'). Ami hibás vagy már megvan, azt kihagyja, a
        többit felveszi. Visszaad: (felvettek, hibák [(sor, üzenet)])."""
        items, errors = parse_bulk(text)
        added = []
        with self._list_lock:
            for no, name, ip in items:
                try:
                    added.append(self.store.add(name, ip))
                except StoreError as e:
                    errors.append((no, "{}: {}".format(name, e)))
            if added:
                self.refresh_list()
        if added:
            for p in added:
                threading.Thread(target=self._poll_one, args=((p["name"], p["ip"]),), daemon=True).start()
        return added, sorted(errors)

    def remove_printer(self, printer_id):
        """Törli a nyomtatót; a nevét adja vissza, vagy None-t, ha nem volt ilyen."""
        with self._list_lock:
            name = self.store.remove(printer_id)
            if name is not None:
                self.refresh_list()
        return name

    # ------------------------------------------------------------ lekérdezés
    def _poll_one(self, item):
        name, ip = item
        try:
            r = printers.poll(name, ip, self.cfg.community, self.cfg.snmp_port,
                              ignore_alerts=self.cfg.ignore_alerts)
        except Exception as e:      # váratlan hiba se állítsa le a figyelést
            r = {"name": name, "ip": ip, "online": False, "status": "-", "supplies": [],
                 "alerts": [], "info": {}, "error": "belső hiba: {}".format(e)[:200],
                 "time": time.strftime("%H:%M:%S")}
        found = issues_of(r, self.cfg.warn_pct)
        with self.lock:
            if self._active.get(name) != ip:
                return              # a lekérdezés közben törölték a nyomtatót: az eredmény nem számít
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
            # Az előzmény írása a zár alatt történik, hogy egy közben törölt nyomtatónak ne
            # nyíljon új esemény (a törlés ugyanezt a zárat használja).
            if self.history is not None:
                try:
                    self.history.record(name, r["online"], now, r["supplies"])
                except Exception as e:      # az előzmények hibája ne akassza meg a figyelést
                    print("Előzmény-mentési hiba:", e)
        new = sorted(text for key, (_, text) in now.items() if key not in before)
        if new:
            self.notifier.send("{} ({}):\n{}".format(name, ip, "\n".join(new)))

    def poll_once(self):
        """Egyszerre kérdezi le az összes nyomtatót (párhuzamosan)."""
        with self._list_lock:
            self.refresh_list()
            items = list(self._active.items())
        if items:
            workers = max(1, min(8, len(items)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(self._poll_one, items))
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
        """A nyomtatók utolsó ismert állapota a felvétel sorrendjében (id-vel együtt)."""
        items = self.printer_list()
        with self.lock:
            state = dict(self.state)
        out = []
        for pid, name, ip in items:
            r = dict(state[name]) if name in state else {"name": name, "ip": ip, "pending": True}
            r["id"] = pid
            out.append(r)
        return out
