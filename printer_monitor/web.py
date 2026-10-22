"""Kis webszerver: a /api a nyomtatók adatait, a /api/history az előzményeket adja JSON-ban,
a többi a web/ mappa fájlja."""
import json
import mimetypes
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


def make_server(host, port, monitor, web_dir):
    web_dir = Path(web_dir).resolve()
    cfg = monitor.cfg

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass        # a böngésző közben bezárta a kapcsolatot, nem hiba

        def _history_payload(self, query):
            q = urllib.parse.parse_qs(query)
            printer = (q.get("printer") or [""])[0] or None
            try:
                days = int((q.get("days") or ["7"])[0])
            except ValueError:
                days = 7
            days = max(1, min(days, 3650))
            history = monitor.history
            return {
                "enabled": history is not None,
                "now": int(time.time()),
                "days": days,
                "printers": [n for n, _ in cfg.printers],
                "episodes": history.episodes(printer, days) if history is not None else [],
            }

        def do_GET(self):
            raw_path, _, query = self.path.partition("?")
            path = urllib.parse.unquote(raw_path)
            if path == "/api/history":
                try:
                    body = json.dumps(self._history_payload(query), ensure_ascii=False).encode("utf-8")
                except Exception as e:      # pl. sérült adatbázisfájl: ne dőljön el a szerver
                    self._send(500, json.dumps({"error": str(e)}).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api":
                payload = {
                    "settings": {"warn_pct": cfg.warn_pct, "crit_pct": cfg.crit_pct,
                                 "poll_seconds": cfg.poll_seconds},
                    "printers": monitor.snapshot(),
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
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

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
