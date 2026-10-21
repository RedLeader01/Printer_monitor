#!/usr/bin/env python3
"""
Nyomtató figyelő - SNMP alapú, EGYETLEN fájl, semmit nem kell hozzá telepíteni
(csak Python 3.8+). Windowson és Linuxon is fut.

Gyors teszt egy nyomtatóra (nem indít webszervert):
    python printer_monitor.py test Nyomtato_IP

Figyelő indítása:
    python printer_monitor.py
    -> böngészőben: http://localhost:8080

Opcionális Telegram értesítés (csak változáskor küld):
    TELEGRAM_TOKEN és TELEGRAM_CHAT_ID környezeti változó
"""
import html
import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------- BEÁLLÍTÁSOK -----------------------------
PRINTERS = [
    # (név, IP)  - írd át a sajátjaidra
    ("dummy1", "192.168.1.100"),
    ("dummy2", "192.168.1.101"),
    ("dummy3", "192.168.1.102"),
    ("dummy4", "192.168.1.103"),
    ("dummy5", "192.168.1.104"),
    ("dummy6", "192.168.1.105"),
    ("dummy7", "192.168.1.106"),
]
COMMUNITY = "public"
POLL_SECONDS = 30       # milyen gyakran kérdezze le a nyomtatókat
WARN_PCT = 20           # sárga: ennyi % alatt
CRIT_PCT = 10           # piros: ennyi % alatt
HTTP_HOST = "127.0.0.1"  # csak ezen a gépen érhető el; mások számára: "0.0.0.0"
HTTP_PORT = 8080
# -----------------------------------------------------------------------

OID_SUP_DESC = "1.3.6.1.2.1.43.11.1.1.6"
OID_SUP_MAX = "1.3.6.1.2.1.43.11.1.1.8"
OID_SUP_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"
OID_ALERT_SEV = "1.3.6.1.2.1.43.18.1.1.2"
OID_ALERT_DESC = "1.3.6.1.2.1.43.18.1.1.8"
OID_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1"

# adatlap (Részletek menü)
OID_SYS_DESCR = "1.3.6.1.2.1.1.1"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3"
OID_DEV_DESCR = "1.3.6.1.2.1.25.3.2.1.3"
OID_SERIAL = "1.3.6.1.2.1.43.5.1.1.17"
OID_IN_MAX = "1.3.6.1.2.1.43.8.2.1.9"
OID_IN_LEVEL = "1.3.6.1.2.1.43.8.2.1.10"
OID_IN_NAME = "1.3.6.1.2.1.43.8.2.1.13"
OID_OUT_REMAIN = "1.3.6.1.2.1.43.9.2.1.5"
OID_OUT_NAME = "1.3.6.1.2.1.43.9.2.1.7"

PRINTER_STATUS = {1: "egyéb", 2: "ismeretlen", 3: "készenlét", 4: "nyomtat", 5: "bemelegít"}
SEVERITY = {1: "info", 3: "KRITIKUS", 4: "figyelmeztetés", 5: "figyelmeztetés"}


# ------------------- minimális SNMP v2c kliens (BER) -------------------
def _len(n):
    if n < 128:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(n):
    size = max(1, (n.bit_length() + 8) // 8)
    return _tlv(0x02, n.to_bytes(size, "big", signed=True))


def _oid(s):
    a = [int(x) for x in s.strip(".").split(".")]
    body = bytearray([40 * a[0] + a[1]])
    for x in a[2:]:
        chunk = [x & 0x7F]
        x >>= 7
        while x:
            chunk.append(0x80 | (x & 0x7F))
            x >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _oid_str(b):
    out = [b[0] // 40, b[0] % 40]
    v = 0
    for c in b[1:]:
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            out.append(v)
            v = 0
    return ".".join(map(str, out))


def _read(buf, pos):
    tag = buf[pos]
    n = buf[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    return tag, buf[pos:pos + n], pos + n


def _value(tag, val):
    if tag == 0x02:
        return int.from_bytes(val, "big", signed=True)
    if tag in (0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(val, "big")
    if tag == 0x04:
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return val.decode("latin-1")
    if tag == 0x06:
        return _oid_str(val)
    return None


def _parse_response(data):
    _, msg, _ = _read(data, 0)
    _, _, p = _read(msg, 0)        # verzió
    _, _, p = _read(msg, p)        # community
    tag, pdu, _ = _read(msg, p)
    if tag != 0xA2:
        raise ValueError("nem válasz PDU")
    _, rid, p = _read(pdu, 0)
    _, es, p = _read(pdu, p)
    _, _, p = _read(pdu, p)
    _, vbl, _ = _read(pdu, p)
    _, vb, _ = _read(vbl, 0)
    _, oidb, p = _read(vb, 0)
    vtag, val, _ = _read(vb, p)
    return (int.from_bytes(rid, "big", signed=True),
            int.from_bytes(es, "big", signed=True),
            _oid_str(oidb), vtag, _value(vtag, val))


def _getnext_packet(community, oid, rid):
    varbind = _tlv(0x30, _oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA1, _int(rid) + _int(0) + _int(0) + _tlv(0x30, varbind))
    return _tlv(0x30, _int(1) + _tlv(0x04, community.encode()) + pdu)


def walk(ip, oid, port=161, timeout=2.0, retries=1):
    """SNMP walk -> {index-utótag: érték}. Nincs válasz esetén RuntimeError."""
    base = oid.strip(".")
    prefix = base + "."
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    out, cur = {}, base
    try:
        for _ in range(500):
            rid = random.randint(1, 2 ** 30)
            packet = _getnext_packet(COMMUNITY, cur, rid)
            resp = None
            for _try in range(retries + 1):
                sock.sendto(packet, (ip, port))
                try:
                    while True:
                        data, _ = sock.recvfrom(65535)
                        try:
                            r = _parse_response(data)
                        except Exception:
                            continue
                        if r[0] == rid:
                            resp = r
                            break
                except socket.timeout:
                    pass
                if resp:
                    break
            if not resp:
                raise RuntimeError("nincs SNMP válasz (időtúllépés) - SNMP be van kapcsolva? jó az IP/community?")
            _, es, noid, vtag, val = resp
            if es != 0 or vtag >= 0x80 or not noid.startswith(prefix) or noid == cur:
                break
            out[noid[len(prefix):]] = val
            cur = noid
    finally:
        sock.close()
    return out


# ----------------------------- figyelő logika --------------------------
state = {}
state_lock = threading.Lock()
last_issues = {}

_PN_RE = re.compile(r"\bPN\s*([A-Za-z0-9][A-Za-z0-9\-./]*)")
_MANUAL_RE = re.compile(r"manual|\bmp\b|multi|bypass|kézi|kezi", re.I)


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def split_supply_name(raw):
    """'Festékkazetta, PN 106R03396;SN8012...' -> ('Festékkazetta', '106R03396')"""
    raw = str(raw)
    m = _PN_RE.search(raw)
    pn = m.group(1) if m else None
    name = raw.split(", PN")[0].split(";SN")[0].strip() or raw
    return name, pn


def _idx_key(k):
    return [int(x) for x in k.split(".") if x.isdigit()]


def _first(d):
    for k in sorted(d, key=_idx_key):
        if d[k] not in (None, ""):
            return d[k]
    return None


def tray_state(level, mx, empty_word, ok_word):
    lv, m = to_int(level), to_int(mx)
    if lv is None or lv in (-1, -2):
        return "ismeretlen"
    if lv == 0:
        return empty_word
    if lv == -3:
        return ok_word
    if m is not None and m > 0:
        return f"{round(lv * 100 / m)}%"
    return ok_word


def poll_info(ip):
    """Extra adatok a Részletek menühöz. Ami nem érhető el, az üresen marad."""
    info = {"model": None, "serial": None, "uptime": None, "descr": None,
            "inputs": [], "outputs": []}
    try:
        info["descr"] = _first(walk(ip, OID_SYS_DESCR))
    except Exception:
        pass
    try:
        ticks = to_int(_first(walk(ip, OID_SYS_UPTIME)))
        if ticks is not None:
            info["uptime"] = ticks // 100
    except Exception:
        pass
    try:
        info["model"] = _first(walk(ip, OID_DEV_DESCR))
    except Exception:
        pass
    try:
        info["serial"] = _first(walk(ip, OID_SERIAL))
    except Exception:
        pass
    try:
        names = walk(ip, OID_IN_NAME)
        lv = walk(ip, OID_IN_LEVEL)
        mx = walk(ip, OID_IN_MAX)
        for idx in sorted(set(names) | set(lv), key=_idx_key):
            st = tray_state(lv.get(idx), mx.get(idx), "ÜRES", "van papír")
            tname = names.get(idx) or f"Tálca {idx}"
            info["inputs"].append({"name": tname, "state": st,
                                   "warn": st == "ÜRES" and not _MANUAL_RE.search(str(tname))})
    except Exception:
        pass
    try:
        names = walk(ip, OID_OUT_NAME)
        rem = walk(ip, OID_OUT_REMAIN)
        for idx in sorted(set(names) | set(rem), key=_idx_key):
            st = tray_state(rem.get(idx), None, "TELE", "van hely")
            info["outputs"].append({"name": names.get(idx) or f"Kimenet {idx}", "state": st,
                                    "warn": st == "TELE"})
    except Exception:
        pass
    return info


def poll(name, ip):
    res = {"name": name, "ip": ip, "online": False, "status": "-", "supplies": [],
           "alerts": [], "info": {}, "error": "", "time": time.strftime("%H:%M:%S")}
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
                elif m == -2 and l <= 100:       # max ismeretlen, a szint már %
                    pct = l
            note = ""
            if l == -3:
                note = "van még (pontos szint nem ismert)"
            elif pct is None:
                note = "ismeretlen"
            sname, pn = split_supply_name(d)
            res["supplies"].append({"name": sname, "pn": pn, "pct": pct, "note": note})

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

        try:
            res["info"] = poll_info(ip)
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


# ------------------------------ weboldal --------------------------------
PAGE_CSS = """
body{font-family:system-ui,sans-serif;margin:0;padding:12px;background:#f4f4f4}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;align-items:start}
.c{background:#fff;border-left:6px solid #2a2;border-radius:6px;padding:10px 14px}
.c.warn{border-color:#e90} .c.bad{border-color:#d33}
h2{margin:0 0 4px;font-size:18px} small{color:#777;font-weight:normal}
p{margin:4px 0;color:#555;font-size:13px}
.row{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px}
.row label{flex:0 0 42%} .bar{flex:1;background:#ddd;height:10px;border-radius:5px;overflow:hidden}
.bar div{height:100%} .al{color:#b00;font-size:13px;margin-top:4px}
.order{background:#fff7e0;border-left:6px solid #e90;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:14px}
.order div{margin:3px 0}
details{margin-top:8px} summary{cursor:pointer;color:#06c;font-size:13px}
table.info{border-collapse:collapse;font-size:12px;margin-top:6px;width:100%}
.info th{text-align:left;color:#777;font-weight:normal;padding:2px 10px 2px 0;vertical-align:top;white-space:nowrap}
.info td{padding:2px 0;word-break:break-word}
.warnt{color:#b00;font-weight:bold} .descr{color:#777}
"""

# Az oldal a háttérben frissíti magát, és a nyitott "Részletek" menük nyitva maradnak.
PAGE_JS = """
async function refresh(){
  try{
    const open=[...document.querySelectorAll('details[open]')].map(d=>d.dataset.k);
    const r=await fetch('/partial',{cache:'no-store'});
    if(!r.ok) return;
    document.getElementById('app').innerHTML=await r.text();
    document.querySelectorAll('details').forEach(d=>{ if(open.includes(d.dataset.k)) d.open=true; });
  }catch(e){}
}
setInterval(refresh,15000);
"""


def color(pct):
    if pct is None:
        return "#888"
    return "#d33" if pct < CRIT_PCT else "#e90" if pct < WARN_PCT else "#2a2"


def fmt_uptime(sec):
    d, rest = divmod(int(sec), 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d} nap {h} óra"
    if h:
        return f"{h} óra {m} perc"
    return f"{m} perc"


def info_html(r):
    i = r.get("info") or {}
    rows = []

    def row(label, value):
        if value:
            rows.append(f"<tr><th>{label}</th><td>{value}</td></tr>")

    esc = html.escape
    row("Típus", esc(str(i["model"])) if i.get("model") else "")
    row("Sorozatszám", esc(str(i["serial"])) if i.get("serial") else "")
    if i.get("uptime") is not None:
        row("Üzemidő", fmt_uptime(i["uptime"]))
    if i.get("inputs"):
        row("Papírtálcák", "".join(
            f"<div{' class=warnt' if t['warn'] else ''}>{esc(str(t['name']))}: {esc(t['state'])}</div>"
            for t in i["inputs"]))
    if i.get("outputs"):
        row("Kimeneti tálca", "".join(
            f"<div{' class=warnt' if t['warn'] else ''}>{esc(str(t['name']))}: {esc(t['state'])}</div>"
            for t in i["outputs"]))
    pns = [s for s in r["supplies"] if s.get("pn")]
    if pns:
        row("Cikkszámok", "".join(f"<div>{esc(s['name'])}: {esc(s['pn'])}</div>" for s in pns))
    row("Rendszerleírás", f"<span class=descr>{esc(str(i['descr']))}</span>" if i.get("descr") else "")
    if not rows:
        return "<p>Nincs elérhető extra adat.</p>"
    return "<table class=info>" + "".join(rows) + "</table>"


def render_inner():
    with state_lock:
        items = [state.get(n) for n, _ in PRINTERS]
    cards, low = [], []
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
            if p is not None and p < WARN_PCT:
                low.append((name, s))
        al = "".join(f"<div class=al>{html.escape(a['sev'])}: {html.escape(a['text'])}</div>"
                     for a in r["alerts"])
        details = (f"<details data-k=\"{html.escape(name)}\"><summary>Részletek</summary>"
                   f"{info_html(r)}</details>")
        cards.append(f"<div class='{cls}'><h2>{html.escape(name)} <small>{ip}</small></h2>"
                     f"<p>Állapot: {html.escape(r['status'])} · frissítve {r['time']}</p>"
                     f"{rows}{al}{details}</div>")
    order = ""
    if low:
        lines = ""
        for pname, s in low:
            pn = f" · cikkszám: <b>{html.escape(s['pn'])}</b>" if s.get("pn") else ""
            lines += f"<div>{html.escape(pname)} – {html.escape(s['name'])} ({s['pct']}%){pn}</div>"
        order = f"<div class=order><b>Hamarosan elfogy:</b>{lines}</div>"
    return order + "<div class=g>" + "".join(cards) + "</div>"


def render():
    return ("<!doctype html><html lang=hu><head><meta charset=utf-8>"
            "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
            "<noscript><meta http-equiv=refresh content=60></noscript>"
            "<title>Nyomtatók</title><style>" + PAGE_CSS + "</style></head><body>"
            "<h1 style=\"font-size:20px\">Nyomtatók</h1><div id=app>" + render_inner() + "</div>"
            "<script>" + PAGE_JS + "</script></body></html>")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api":
            with state_lock:
                body, ctype = json.dumps(state, ensure_ascii=False).encode(), "application/json"
        elif path == "/partial":
            body, ctype = render_inner().encode(), "text/html; charset=utf-8"
        else:
            body, ctype = render().encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_one(ip):
    r = poll(ip, ip)
    print(json.dumps(r, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "test":
        test_one(sys.argv[2])
        sys.exit(0)
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"Fut: http://{HTTP_HOST}:{HTTP_PORT}  (JSON: /api)  - leállítás: Ctrl+C")
    ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler).serve_forever()
