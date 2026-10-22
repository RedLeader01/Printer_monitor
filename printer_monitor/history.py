"""Előzmények: a hibák, az elérhetetlenség és a kellékanyag-szintek mentése egy helyi SQLite fájlba.

Két dolgot tárol:

* episodes - "epizódok": egy probléma kezdete és vége (nem elérhető, hiba/figyelmeztetés,
  kevés kellékanyag). Ebből látszik, mikor volt a gond és mennyi ideig tartott.
  Amíg a probléma fennáll, az `ended` üres.
* readings - a kellékanyag-szintek, de csak amikor változnak (nem minden lekérdezéskor),
  így a fájl kicsi marad. Ebből lehet később előrejelzést és csere-észlelést készíteni.

Az időpontokat UTC másodpercben (epoch) tárolja, a böngésző számolja át helyi időre.
"""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT    NOT NULL,
    kind    TEXT    NOT NULL,
    key     TEXT    NOT NULL,
    detail  TEXT    NOT NULL,
    started INTEGER NOT NULL,
    ended   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_episodes_started ON episodes (started);
CREATE INDEX IF NOT EXISTS ix_episodes_open ON episodes (printer, ended);

CREATE TABLE IF NOT EXISTS readings (
    ts      INTEGER NOT NULL,
    printer TEXT    NOT NULL,
    supply  TEXT    NOT NULL,
    pct     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_readings ON readings (printer, supply, ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

GAP_KIND = "gap"            # a figyelő program maga nem futott
ALL_PRINTERS = "*"          # a "gap" epizód nem egy nyomtatóhoz tartozik
PRUNE_EVERY = 24 * 3600


class History:
    def __init__(self, path, keep_days=365, clock=time.time):
        self.path = str(path)
        self.keep_days = keep_days
        self._clock = clock
        self._lock = threading.Lock()
        self._last_pct = {}                 # (nyomtató, kellék) -> utoljára mentett szint
        self._last_prune = 0
        # A kapcsolatot több szál használja (lekérdezők, webszerver), ezért zárral védjük.
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ belső
    def _now(self):
        return int(self._clock())

    def _set_meta(self, key, value):
        self._db.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                         "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, str(value)))

    def _transaction(self, fn):
        """Egy tranzakcióban futtatja fn-t; hiba esetén visszavonja."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                result = fn()
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return result

    # ------------------------------------------------------------- életciklus
    def start(self, poll_seconds):
        """Indításkor hívandó. Ha a figyelő hosszabb ideig nem futott, azt külön epizódként rögzíti
        (így a hosszak értelmezhetők), és takarít."""
        now = self._now()

        def work():
            row = self._db.execute("SELECT v FROM meta WHERE k = 'last_seen'").fetchone()
            if row:
                last = int(row["v"])
                if now - last > max(120, poll_seconds * 3):
                    self._db.execute(
                        "INSERT INTO episodes (printer, kind, key, detail, started, ended) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (ALL_PRINTERS, GAP_KIND, GAP_KIND, "A figyelő nem futott", last, now))
            self._set_meta("last_seen", now)

        self._transaction(work)
        self.prune()

    def cycle_done(self):
        """Minden lekérdezési kör végén hívandó: jelzi, hogy a figyelő él, és időnként takarít."""
        now = self._now()
        with self._lock:
            self._set_meta("last_seen", now)
        if now - self._last_prune >= PRUNE_EVERY:
            self.prune()

    def prune(self):
        """A keep_days-nél régebbi lezárt epizódok és szintek törlése."""
        cutoff = self._now() - self.keep_days * 86400

        def work():
            self._db.execute("DELETE FROM episodes WHERE ended IS NOT NULL AND ended < ?", (cutoff,))
            self._db.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))

        self._transaction(work)
        self._last_prune = self._now()

    def close(self):
        with self._lock:
            self._db.close()

    # --------------------------------------------------------------- rögzítés
    def record(self, printer, online, issues, supplies):
        """Egy nyomtató egy lekérdezésének rögzítése.

        issues: {kulcs: (fajta, szöveg)} - a nyomtató jelenlegi problémái
        supplies: [{"name":..., "pct":...}] - a kellékanyagok szintjei
        """
        now = self._now()
        new_levels = []

        def work():
            rows = self._db.execute(
                "SELECT id, key, detail FROM episodes "
                "WHERE printer = ? AND ended IS NULL AND kind != ?", (printer, GAP_KIND)).fetchall()
            open_by_key = {r["key"]: r for r in rows}

            for key, (kind, text) in issues.items():
                row = open_by_key.get(key)
                if row is None:
                    self._db.execute(
                        "INSERT INTO episodes (printer, kind, key, detail, started) VALUES (?, ?, ?, ?, ?)",
                        (printer, kind, key, text, now))
                elif row["detail"] != text:             # pl. a kellék szintje közben változott
                    self._db.execute("UPDATE episodes SET detail = ? WHERE id = ?", (text, row["id"]))

            # Elérhetetlen nyomtatónál nem tudjuk, megszűnt-e egy hiba, ezért csak az online
            # nyomtatóknál zárunk le epizódot.
            if online:
                for key, row in open_by_key.items():
                    if key not in issues:
                        self._db.execute("UPDATE episodes SET ended = ? WHERE id = ?", (now, row["id"]))

                for s in supplies:
                    pct = s.get("pct")
                    if pct is None:
                        continue
                    k = (printer, s["name"])
                    if self._last_pct.get(k) != pct:
                        self._db.execute("INSERT INTO readings (ts, printer, supply, pct) VALUES (?, ?, ?, ?)",
                                         (now, printer, s["name"], pct))
                        new_levels.append((k, pct))

        self._transaction(work)
        for k, pct in new_levels:               # csak sikeres mentés után jegyezzük meg
            self._last_pct[k] = pct

    # ------------------------------------------------------------ lekérdezés
    def episodes(self, printer=None, days=7, limit=2000):
        """Az elmúlt `days` nap epizódjai, a legfrissebb elöl. A még tartó epizód `ended` mezője None."""
        since = self._now() - int(days) * 86400
        sql = ("SELECT id, printer, kind, detail, started, ended FROM episodes "
               "WHERE (ended IS NULL OR ended >= ?)")
        args = [since]
        if printer:
            sql += " AND (printer = ? OR printer = ?)"
            args += [printer, ALL_PRINTERS]
        sql += " ORDER BY started DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def readings(self, printer, supply, days=30):
        """Egy kellékanyag szintjeinek változásai [(ts, pct), ...] időrendben."""
        since = self._now() - int(days) * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, pct FROM readings WHERE printer = ? AND supply = ? AND ts >= ? ORDER BY ts",
                (printer, supply, since)).fetchall()
        return [(r["ts"], r["pct"]) for r in rows]
