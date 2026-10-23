"""Kis webszerver.

* GET  /api              a nyomtatók adatai (JSON)
* GET  /api/history      az előzmények (JSON)
* GET  /api/me           ki vagyok: admin-e, és a módosításhoz szükséges CSRF-jelzés
* POST /api/login        belépés admin fiókkal (munkamenet süti)
* POST /api/logout       kilépés
* POST /api/printers     nyomtató felvétele        (admin)
* POST /api/printers/bulk  több nyomtató egyszerre   (admin)
* DELETE /api/printers/N nyomtató törlése          (admin)
* minden más             a web/ mappa fájlja

A módosító kérések védelméről lásd az auth.py-t.
"""
import json
import mimetypes
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import auth as authmod
from .store import DuplicateError, MAX_PRINTERS, StoreError

# A Windows beállításjegyzéke néha rossz típust ad a .js fájlnak, ezért a fontosakat itt rögzítjük.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

MAX_BODY = 65536
JSON_TYPE = "application/json; charset=utf-8"
# Csak a saját fájljainkat tölti be az oldal, és más oldal nem keretezheti be (clickjacking ellen).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
}
_PRINTER_PATH = re.compile(r"/api/printers/(\d{1,12})")


class HttpError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


def make_server(host, port, monitor, web_dir, auth=None):
    """`auth` nélkül (vagy tároló nélkül) a nyomtatókezelés ki van kapcsolva: csak olvasni lehet."""
    web_dir = Path(web_dir).resolve()
    cfg = monitor.cfg

    def manage_enabled():
        return auth is not None and monitor.store is not None

    class Handler(BaseHTTPRequestHandler):
        timeout = 15            # lassú vagy félbehagyott kérés ne tartson fogva szálat
        server_version = "printer-monitor"
        sys_version = ""

        # ------------------------------------------------------------ válaszok
        def _send(self, code, body, ctype, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass        # a böngésző közben bezárta a kapcsolatot, nem hiba

        def _json(self, code, obj, headers=None):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), JSON_TYPE, headers)

        def _principal(self):
            if auth is None:
                return authmod.Principal()
            return auth.identify(self.client_address[0], self.headers)

        # ------------------------------------------------------------- GET
        def _history_payload(self, query):
            q = urllib.parse.parse_qs(query)
            printer = (q.get("printer") or [""])[0] or None
            try:
                days = int((q.get("days") or ["7"])[0])
            except ValueError:
                days = 7
            days = max(1, min(days, 3650))
            history = monitor.history
            live = [name for _, name, _ in monitor.printer_list()]
            # A törölt nyomtatók régi eseményei is megmaradnak: az időszakon belüliek látszanak.
            removed = [n for n in history.printer_names(days) if n not in live] if history is not None else []
            return {
                "enabled": history is not None,
                "now": int(time.time()),
                "days": days,
                "printers": live + removed,
                "removed": removed,
                "episodes": history.episodes(printer, days) if history is not None else [],
            }

        def _me_payload(self):
            p = self._principal()
            return {
                "manage": manage_enabled(),
                "admin": p.is_admin and manage_enabled(),
                "kind": p.kind,
                "user": p.user if p.is_admin else None,
                "csrf": p.csrf if p.is_admin else None,
                "expires": p.expires,
                "max_printers": MAX_PRINTERS,
            }

        def do_GET(self):
            raw_path, _, query = self.path.partition("?")
            path = urllib.parse.unquote(raw_path)
            if path == "/api/me":
                self._json(200, self._me_payload())
                return
            if path == "/api/history":
                try:
                    payload = self._history_payload(query)
                except Exception as e:      # pl. sérült adatbázisfájl: ne dőljön el a szerver
                    print("Előzmény-olvasási hiba:", e)
                    self._json(500, {"error": "Az előzmények most nem olvashatók."})
                    return
                self._json(200, payload)
                return
            if path == "/api":
                self._json(200, {
                    "settings": {"warn_pct": cfg.warn_pct, "crit_pct": cfg.crit_pct,
                                 "poll_seconds": cfg.poll_seconds},
                    "printers": monitor.snapshot(),
                })
                return
            try:
                target = (web_dir / (path.lstrip("/") or "index.html")).resolve()
                target.relative_to(web_dir)             # kilépés a web/ mappából -> ValueError
                if not target.is_file():
                    raise FileNotFoundError(path)
                body = target.read_bytes()
            except (ValueError, OSError):
                self._send(404, "Nincs ilyen oldal.".encode("utf-8"), "text/plain; charset=utf-8")
                return
            ctype = CONTENT_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] \
                or "application/octet-stream"
            self._send(200, body, ctype)

        # ------------------------------------------------- módosító kérések
        def _read_body(self):
            """A kérés törzse bájtokban (a hossza korlátozott). A törzs nélküli kérésnél b''."""
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                return b""
            try:
                n = int(raw_len)
            except ValueError:
                raise HttpError(400, "Hibás Content-Length.")
            if n < 0 or n > MAX_BODY:
                raise HttpError(413, "Túl nagy kérés.")
            return self.rfile.read(n) if n else b""

        def _json_body(self, raw):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HttpError(415, "A kérés típusa application/json legyen.")
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise HttpError(400, "Hibás JSON.")
            if not isinstance(data, dict):
                raise HttpError(400, "Hibás kérés.")
            return data

        def _require_same_origin(self):
            if not authmod.same_origin(self.headers):
                raise HttpError(403, "A kérés nem erről az oldalról érkezett.")

        def _require_admin(self):
            """Admin jogosultság és CSRF-jelzés ellenőrzése. Visszaadja a Principal-t."""
            self._require_same_origin()
            p = self._principal()
            if not p.is_admin:
                raise HttpError(401, "Ehhez admin jogosultság kell. Lépj be admin fiókkal.")
            if not auth.check_csrf(p, self.headers.get("X-CSRF-Token")):
                raise HttpError(403, "Érvénytelen vagy hiányzó biztonsági jelzés. Töltsd újra az oldalt.")
            return p

        def _login(self, raw):
            self._require_same_origin()
            data = self._json_body(raw)
            try:
                token, csrf, user, expires = auth.login(self.client_address[0],
                                                        data.get("username"), data.get("password"))
            except authmod.AuthError as e:
                headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
                raise HttpError(e.status, e.message, headers)
            cookie = authmod.session_cookie(token, auth.session_seconds)
            self._json(200, {"ok": True, "user": user, "csrf": csrf, "expires": expires},
                       {"Set-Cookie": cookie})

        def _logout(self):
            self._require_same_origin()
            p = self._principal()
            if p.kind == "admin" and not auth.check_csrf(p, self.headers.get("X-CSRF-Token")):
                raise HttpError(403, "Érvénytelen vagy hiányzó biztonsági jelzés. Töltsd újra az oldalt.")
            auth.logout(authmod.parse_session_cookie(self.headers))
            self._json(200, {"ok": True}, {"Set-Cookie": authmod.session_cookie("", 0)})

        def _add_printer(self, raw):
            self._require_admin()
            data = self._json_body(raw)
            try:
                printer = monitor.add_printer(data.get("name"), data.get("ip"))
            except DuplicateError as e:
                raise HttpError(409, str(e))
            except StoreError as e:
                raise HttpError(400, str(e))
            self._json(201, {"ok": True, "printer": printer})

        def _add_bulk(self, raw):
            self._require_admin()
            data = self._json_body(raw)
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HttpError(400, "Írj be legalább egy sort (Név = IP-cím).")
            added, errors = monitor.add_printers(text)
            self._json(200, {"ok": not errors, "added": added,
                             "errors": [{"line": n, "error": m} for n, m in errors]})

        def _delete_printer(self, printer_id):
            self._require_admin()
            name = monitor.remove_printer(printer_id)
            if name is None:
                raise HttpError(404, "Nincs ilyen nyomtató (talán már törölte valaki).")
            self._json(200, {"ok": True, "name": name})

        def _mutation(self, method):
            path = urllib.parse.unquote(self.path.split("?", 1)[0])
            try:
                raw = self._read_body()          # mindig beolvassuk, hogy a válasz előtt ne maradjon a hálózaton
                if not manage_enabled():
                    raise HttpError(404, "A nyomtatókezelés ki van kapcsolva.")
                if method == "POST" and path == "/api/login":
                    self._login(raw)
                elif method == "POST" and path == "/api/logout":
                    self._logout()
                elif method == "POST" and path == "/api/printers/bulk":
                    self._add_bulk(raw)
                elif method == "POST" and path == "/api/printers":
                    self._add_printer(raw)
                elif method == "DELETE" and _PRINTER_PATH.fullmatch(path):
                    self._delete_printer(int(_PRINTER_PATH.fullmatch(path).group(1)))
                else:
                    raise HttpError(404, "Nincs ilyen végpont.")
            except HttpError as e:
                self._json(e.status, {"error": e.message}, e.headers)
            except Exception as e:               # váratlan hiba se döntse el a szervert
                print("Hiba a kérés feldolgozásakor:", e)
                self._json(500, {"error": "Belső hiba."})

        def do_POST(self):
            self._mutation("POST")

        def do_DELETE(self):
            self._mutation("DELETE")

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
