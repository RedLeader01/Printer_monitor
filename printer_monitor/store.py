"""A nyomtatólista és az admin fiókok tárolása egy külön SQLite fájlban (alapból printers.db).

Szándékosan külön van az előzményektől (history.db): a nyomtatólista a program beállítása,
az előzmények pedig összegyűjtött adat, amit akár törölni is lehet anélkül, hogy a lista elveszne.

Nincs alap lista: az első indításkor üres, az admin tölti fel a weboldalon (vagy a config.ini
régi [printers] szakaszából a `python run.py import-printers` paranccsal).
"""
import ipaddress
import re
import sqlite3
import threading
import time

MAX_PRINTERS = 500
NAME_MAX = 40
USERNAME_MAX = 32
RESERVED_NAMES = {"*"}         # a "*" az előzményekben a "minden nyomtató" jelölése

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    ip    TEXT    NOT NULL,
    added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    username TEXT    PRIMARY KEY COLLATE NOCASE,
    pw_hash  TEXT    NOT NULL,
    created  INTEGER NOT NULL
);
"""

_HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
_USER_RE = re.compile(r"[A-Za-z0-9._@-]+")


class StoreError(Exception):
    """Érthető, magyar hibaüzenetű hiba; a weboldalon is megjelenik a felhasználónak."""


class ValidationError(StoreError):
    pass


class DuplicateError(StoreError):
    pass


def normalize_name(raw):
    """A nyomtató megjelenített neve: szóközök nélkül a szélén, nyomtatható karakterekkel."""
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not name:
        raise ValidationError("A név megadása kötelező.")
    if len(name) > NAME_MAX:
        raise ValidationError("A név legfeljebb {} karakter lehet.".format(NAME_MAX))
    if not name.isprintable():
        raise ValidationError("A név nem tartalmazhat vezérlőkaraktert vagy sortörést.")
    if name in RESERVED_NAMES:
        raise ValidationError("Ez a név foglalt, válassz másikat.")
    return name


def normalize_host(raw):
    """IPv4-cím vagy gépnév ellenőrzése. A szám-pont alakú szöveget csak érvényes IPv4-ként fogadja el."""
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        raise ValidationError("Az IP-cím (vagy gépnév) megadása kötelező.")
    if re.fullmatch(r"[0-9.]+", s):
        try:
            ip = ipaddress.IPv4Address(s)
        except ValueError:
            raise ValidationError("Érvénytelen IPv4-cím: {}".format(s[:40]))
        if ip.is_unspecified or ip.is_multicast or ip == ipaddress.IPv4Address("255.255.255.255"):
            raise ValidationError("Ez nem nyomtató-cím (nem állítható be nyomtatónak): {}".format(s))
        return str(ip)
    if ":" in s:
        raise ValidationError("IPv6-cím nem támogatott, IPv4-címet vagy gépnevet adj meg.")
    if len(s) > 253 or not _HOST_RE.fullmatch(s):
        raise ValidationError("Érvénytelen IP-cím vagy gépnév.")
    return s.lower()


def normalize_username(raw):
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not name:
        raise ValidationError("A felhasználónév megadása kötelező.")
    if len(name) > USERNAME_MAX or not _USER_RE.fullmatch(name):
        raise ValidationError("A felhasználónév legfeljebb {} karakter, és csak betűt, számot "
                              "és . _ - @ jelet tartalmazhat.".format(USERNAME_MAX))
    return name


def parse_bulk(text):
    """Soronként egy nyomtató: `Név = IP`, `Név;IP`, `Név<tab>IP` vagy `Név IP` (Excelből is bemásolható).
    Üres sorok és a # / ; jellel kezdődők kimaradnak. Visszaad: ([(sor, név, ip)], [(sor, hiba)])."""
    items, errors = [], []
    for no, line in enumerate(re.split(r"\r\n|[\n\r]", str(text or "")), start=1):
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        parts = [x.strip() for x in re.split(r"\s*[=;\t]\s*", line) if x.strip()]
        if len(parts) == 1:
            parts = line.rsplit(None, 1)
        if len(parts) != 2:
            errors.append((no, "Ebben a sorban 'Név = IP-cím' kell: {}".format(line[:60])))
            continue
        items.append((no, parts[0], parts[1]))
    return items, errors


class PrinterStore:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        # A kapcsolatot több szál használja (webszerver, lekérdezők), ezért zárral védjük.
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._db.close()

    # -------------------------------------------------------------- nyomtatók
    def list(self):
        """A nyomtatók a felvétel sorrendjében: [{"id", "name", "ip"}, ...]."""
        with self._lock:
            rows = self._db.execute("SELECT id, name, ip FROM printers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM printers").fetchone()[0]

    def get(self, printer_id):
        with self._lock:
            row = self._db.execute("SELECT id, name, ip FROM printers WHERE id = ?", (printer_id,)).fetchone()
        return dict(row) if row else None

    def add(self, name, ip):
        """Új nyomtató felvétele. ValidationError / DuplicateError érthető üzenettel."""
        name = normalize_name(name)
        ip = normalize_host(ip)
        with self._lock:
            if self._db.execute("SELECT COUNT(*) FROM printers").fetchone()[0] >= MAX_PRINTERS:
                raise ValidationError("Legfeljebb {} nyomtató vehető fel.".format(MAX_PRINTERS))
            same_ip = self._db.execute("SELECT name FROM printers WHERE lower(ip) = ?", (ip.lower(),)).fetchone()
            if same_ip:
                raise DuplicateError("Ez az IP-cím már fel van véve: {}".format(same_ip["name"]))
            try:
                cur = self._db.execute("INSERT INTO printers (name, ip, added) VALUES (?, ?, ?)",
                                       (name, ip, int(self._clock())))
            except sqlite3.IntegrityError:
                raise DuplicateError("Már van ilyen nevű nyomtató: {}".format(name))
            return {"id": cur.lastrowid, "name": name, "ip": ip}

    def remove(self, printer_id):
        """Törli a nyomtatót; a nevét adja vissza, vagy None-t, ha nem volt ilyen."""
        with self._lock:
            row = self._db.execute("SELECT name FROM printers WHERE id = ?", (printer_id,)).fetchone()
            if not row:
                return None
            self._db.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
            return row["name"]

    # ------------------------------------------------------------ admin fiókok
    def set_admin_hash(self, username, pw_hash):
        """Új admin felvétele vagy a meglévő jelszavának cseréje. A jelszót a hívó már hash-elte."""
        username = normalize_username(username)
        with self._lock:
            self._db.execute(
                "INSERT INTO admins (username, pw_hash, created) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET pw_hash = excluded.pw_hash",
                (username, pw_hash, int(self._clock())))

    def get_admin_hash(self, username):
        with self._lock:
            row = self._db.execute("SELECT pw_hash FROM admins WHERE username = ?",
                                   (username or "",)).fetchone()
        return row["pw_hash"] if row else None

    def list_admins(self):
        with self._lock:
            rows = self._db.execute("SELECT username, created FROM admins ORDER BY username").fetchall()
        return [dict(r) for r in rows]

    def remove_admin(self, username):
        with self._lock:
            cur = self._db.execute("DELETE FROM admins WHERE username = ?", (username or "",))
            return cur.rowcount > 0
