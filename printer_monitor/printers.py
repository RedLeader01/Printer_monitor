"""Egy nyomtató lekérdezése: fogyóanyagok, állapot, hibák és a Részletek adatai."""
import re
import time
from functools import partial

from .snmp import walk

OID_SUP_DESC = "1.3.6.1.2.1.43.11.1.1.6"
OID_SUP_MAX = "1.3.6.1.2.1.43.11.1.1.8"
OID_SUP_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"
OID_ALERT_SEV = "1.3.6.1.2.1.43.18.1.1.2"
OID_ALERT_DESC = "1.3.6.1.2.1.43.18.1.1.8"
OID_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1"

# Részletek menü
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

_PN_RE = re.compile(r"\bPN\s*([A-Za-z0-9][A-Za-z0-9\-./]*)")
# A kézi adagolók normálisan üresek, ezért az üres állapotuk nem hiba.
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
        return "{}%".format(round(lv * 100 / m))
    return ok_word


def poll_info(w):
    """Extra adatok a Részletek menühöz. `w` egy walk(oid) függvény. Ami nem érhető el, üresen marad."""
    info = {"model": None, "serial": None, "uptime": None, "descr": None,
            "inputs": [], "outputs": []}
    try:
        info["descr"] = _first(w(OID_SYS_DESCR))
    except Exception:
        pass
    try:
        ticks = to_int(_first(w(OID_SYS_UPTIME)))
        if ticks is not None:
            info["uptime"] = ticks // 100
    except Exception:
        pass
    try:
        info["model"] = _first(w(OID_DEV_DESCR))
    except Exception:
        pass
    try:
        info["serial"] = _first(w(OID_SERIAL))
    except Exception:
        pass
    try:
        names, lv, mx = w(OID_IN_NAME), w(OID_IN_LEVEL), w(OID_IN_MAX)
        for idx in sorted(set(names) | set(lv), key=_idx_key):
            st = tray_state(lv.get(idx), mx.get(idx), "ÜRES", "van papír")
            tname = names.get(idx) or "Tálca {}".format(idx)
            info["inputs"].append({"name": tname, "state": st,
                                   "warn": st == "ÜRES" and not _MANUAL_RE.search(str(tname))})
    except Exception:
        pass
    try:
        names, rem = w(OID_OUT_NAME), w(OID_OUT_REMAIN)
        for idx in sorted(set(names) | set(rem), key=_idx_key):
            st = tray_state(rem.get(idx), None, "TELE", "van hely")
            info["outputs"].append({"name": names.get(idx) or "Kimenet {}".format(idx),
                                    "state": st, "warn": st == "TELE"})
    except Exception:
        pass
    return info


def poll(name, ip, community="public", port=161, timeout=2.0):
    """Lekérdez egy nyomtatót, és egy JSON-barát szótárat ad vissza."""
    w = partial(walk, ip, community=community, port=port, timeout=timeout)
    res = {"name": name, "ip": ip, "online": False, "status": "-", "supplies": [],
           "alerts": [], "info": {}, "error": "", "time": time.strftime("%H:%M:%S")}
    try:
        desc = w(OID_SUP_DESC)
        mx = w(OID_SUP_MAX)
        lvl = w(OID_SUP_LEVEL)
        res["online"] = True

        for idx, d in desc.items():
            m, lv = to_int(mx.get(idx)), to_int(lvl.get(idx))
            pct = None
            if lv is not None and lv >= 0:
                if m is not None and m > 0:
                    pct = round(lv * 100 / m)
                elif m == -2 and lv <= 100:       # max ismeretlen, a szint már %
                    pct = lv
            note = ""
            if lv == -3:
                note = "van még (pontos szint nem ismert)"
            elif pct is None:
                note = "ismeretlen"
            sname, pn = split_supply_name(d)
            res["supplies"].append({"name": sname, "pn": pn, "pct": pct, "note": note})

        try:
            st = w(OID_PRINTER_STATUS)
            if st:
                res["status"] = PRINTER_STATUS.get(to_int(next(iter(st.values()))), "?")
        except Exception:
            pass

        try:
            sev = w(OID_ALERT_SEV)
            ad = w(OID_ALERT_DESC)
            for idx, text in ad.items():
                s = SEVERITY.get(to_int(sev.get(idx)), "info")
                if s != "info":
                    res["alerts"].append({"sev": s, "text": text})
        except Exception:
            pass

        try:
            res["info"] = poll_info(w)
        except Exception:
            pass
    except Exception as e:
        res["error"] = str(e)[:200]
    return res
