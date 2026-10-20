#!/usr/bin/env python3
"""
Nyomtató figyelő - SNMP alapú, egyetlen fájl, csak a Python standard könyvtárát használja.

Követelmény:  sudo apt install snmp      (az snmpwalk parancs kell hozzá)
Indítás:      python3 printer_monitor.py
Megnyitás:    http://SZERVER_IP:8080

Opcionális Telegram értesítés (csak változáskor küld):
    export TELEGRAM_TOKEN="123:ABC"
    export TELEGRAM_CHAT_ID="-100123456"
"""
import html
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------- BEÁLLÍTÁSOK -----------------------------
PRINTERS = [
    # (név, IP)  - írd át a sajátjaidra
    ("Lexmark 1", "192.168.1.101"),
    ("Lexmark 2", "192.168.1.102"),
    ("Lexmark 3", "192.168.1.103"),
    ("Lexmark 4", "192.168.1.104"),
    ("Xerox 1", "192.168.1.105"),
    ("Xerox 2", "192.168.1.106"),
]
COMMUNITY = "public"
POLL_SECONDS = 300      # milyen gyakran kérdezze le a nyomtatókat
WARN_PCT = 20           # sárga: ennyi % alatt
CRIT_PCT = 10           # piros: ennyi % alatt
HTTP_PORT = 8080
# -----------------------------------------------------------------------

OID_SUP_DESC = "1.3.6.1.2.1.43.11.1.1.6"
OID_SUP_MAX = "1.3.6.1.2.1.43.11.1.1.8"
OID_SUP_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"
OID_ALERT_SEV = "1.3.6.1.2.1.43.18.1.1.2"
OID_ALERT_DESC = "1.3.6.1.2.1.43.18.1.1.8"
OID_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1"

PRINTER_STATUS = {1: "egyéb", 2: "ismeretlen", 3: "készenlét", 4: "nyomtat", 5: "bemelegít"}
SEVERITY = {1: "info", 3: "KRITIKUS", 4: "figyelmeztetés", 5: "figyelmeztetés"}

state = {}            # név -> utolsó eredmény
state_lock = threading.Lock()
last_issues = {}      # név -> előző problémák halmaza (értesítéshez)


def walk(ip, oid):
    """snmpwalk -> {indexek: érték}. Hiba/timeout esetén kivételt dob."""
    cmd = ["snmpwalk", "-v2c", "-c", COMMUNITY, "-t", "2", "-r", "1", "-Oqn", ip, oid]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError(out.stderr.strip() or "nincs SNMP válasz")
    result = {}
    prefix = "." + oid + "."
    for line in out.stdout.splitlines():
        if " " not in line:
            continue
        key, val = line.split(" ", 1)
        if not key.startswith(prefix):
            continue
        result[key[len(prefix):]] = val.strip().strip('"')
    return result


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def poll(name, ip):
    res = {"name": name, "ip": ip, "online": False, "status": "-", "supplies": [],
           "alerts": [], "error": "", "time": time.strftime("%H:%M:%S")}
    try:
        desc = walk(ip, OID_SUP_DESC)
        mx = walk(ip, OID_SUP_MAX)
        lvl = walk(ip, OID_SUP_LEVEL)
        res["online"] = True

        for idx, d in desc.items():
            m, l = to_int(mx.get(idx)), to_int(lvl.get(idx))
            pct = None
            if l is not None and l >= 0:
                if m is not None and m > 0:
                    pct = round(l * 100 / m)
                elif m == -2 and l <= 100:       # a max ismeretlen, a szint már %
                    pct = l
            note = ""
            if l == -3:
                note = "van még (pontos szint nem ismert)"
            elif l == -2 or (pct is None and l is None):
                note = "ismeretlen"
            res["supplies"].append({"name": d, "pct": pct, "note": note})

        try:
            st = walk(ip, OID_PRINTER_STATUS)
            if st:
                res["status"] = PRINTER_STATUS.get(to_int(next(iter(st.values()))), "?")
        except Exception:
            pass

        try:
            sev = walk(ip, OID_ALERT_SEV)
            ad = walk(ip, OID_ALERT_DESC)
            for idx, text in ad.items():
                s = SEVERITY.get(to_int(sev.get(idx)), "info")
                if s != "info":
                    res["alerts"].append({"sev": s, "text": text})
        except Exception:
            pass
    except Exception as e:
        res["error"] = str(e)[:200]
    return res


def issues_of(r):
    out = set()
    if not r["online"]:
        out.add("OFFLINE")
        return out
    for s in r["supplies"]:
        if s["pct"] is not None and s["pct"] < WARN_PCT:
            out.add(f"{s['name']}: {s['pct']}%")
    for a in r["alerts"]:
        out.add(f"{a['sev']}: {a['text']}")
    return out


def notify(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[értesítés]", text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                               data=data, timeout=10)
    except Exception as e:
        print("Telegram hiba:", e)


def poll_loop():
    while True:
        for name, ip in PRINTERS:
            r = poll(name, ip)
            with state_lock:
                state[name] = r
            now, before = issues_of(r), last_issues.get(name, set())
            new = now - before
            if new:
                notify(f"{name} ({ip}):\n" + "\n".join(sorted(new)))
            last_issues[name] = now
        time.sleep(POLL_SECONDS)


def color(pct):
    if pct is None:
        return "#888"
    return "#d33" if pct < CRIT_PCT else "#e90" if pct < WARN_PCT else "#2a2"


def render():
    cards = []
    with state_lock:
        items = [state.get(n) for n, _ in PRINTERS]
    for (name, ip), r in zip(PRINTERS, items):
        if r is None:
            cards.append(f"<div class=c><h2>{html.escape(name)}</h2><p>lekérdezés folyamatban…</p></div>")
            continue
        if not r["online"]:
            cards.append(f"<div class='c bad'><h2>{html.escape(name)} <small>{ip}</small></h2>"
                         f"<p><b>OFFLINE</b> – {html.escape(r['error'])}</p></div>")
            continue
        bad = any(a["sev"] == "KRITIKUS" for a in r["alerts"])
        warn = bool(r["alerts"]) or any(s["pct"] is not None and s["pct"] < WARN_PCT for s in r["supplies"])
        cls = "c bad" if bad else "c warn" if warn else "c"
        rows = ""
        for s in r["supplies"]:
            p = s["pct"]
            bar = (f"<div class=bar><div style='width:{p}%;background:{color(p)}'></div></div>"
                   f"<span>{p}%</span>") if p is not None else f"<span>{html.escape(s['note'])}</span>"
            rows += f"<div class=row><label>{html.escape(s['name'])}</label>{bar}</div>"
        al = "".join(f"<div class=al>{html.escape(a['sev'])}: {html.escape(a['text'])}</div>"
                     for a in r["alerts"])
        cards.append(f"<div class='{cls}'><h2>{html.escape(name)} <small>{ip}</small></h2>"
                     f"<p>Állapot: {html.escape(r['status'])} · frissítve {r['time']}</p>{rows}{al}</div>")
    return f"""<!doctype html><html lang=hu><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=60><title>Nyomtatók</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:12px;background:#f4f4f4}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}}
.c{{background:#fff;border-left:6px solid #2a2;border-radius:6px;padding:10px 14px}}
.c.warn{{border-color:#e90}} .c.bad{{border-color:#d33}}
h2{{margin:0 0 4px;font-size:18px}} small{{color:#777;font-weight:normal}}
p{{margin:4px 0;color:#555;font-size:13px}}
.row{{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px}}
.row label{{flex:0 0 42%}} .bar{{flex:1;background:#ddd;height:10px;border-radius:5px;overflow:hidden}}
.bar div{{height:100%}} .al{{color:#b00;font-size:13px;margin-top:4px}}
</style></head><body><h1 style="font-size:20px">Nyomtatók</h1><div class=g>{''.join(cards)}</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api":
            with state_lock:
                body, ctype = json.dumps(state, ensure_ascii=False).encode(), "application/json"
        else:
            body, ctype = render().encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"Fut: http://0.0.0.0:{HTTP_PORT}  (JSON: /api)")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
