"""Hamis SNMP-nyomtató a teszteléshez: egy helyi UDP porton válaszol a GETNEXT kérésekre."""
import socket
import threading

from printer_monitor import snmp

SUP = "1.3.6.1.2.1.43.11.1.1."
IN = "1.3.6.1.2.1.43.8.2.1."
OUT = "1.3.6.1.2.1.43.9.2.1."

# Egy Xerox- és Lexmark-szerű nyomtató tipikus válaszai.
DEFAULT_DATA = {
    SUP + "6.1.1": "Festékkazetta, PN 106R03396;SN80120F0B62080000",
    SUP + "6.1.2": "Dobkazetta, PN 113R00779;SN8018441293070000",
    SUP + "6.1.3": "Átvivőhenger, PN 115R00116",
    SUP + "8.1.1": 10000, SUP + "8.1.2": 60000, SUP + "8.1.3": -2,
    SUP + "9.1.1": 1500, SUP + "9.1.2": 54000, SUP + "9.1.3": -3,
    "1.3.6.1.2.1.25.3.5.1.1.1": 3,                       # készenlét
    "1.3.6.1.2.1.43.18.1.1.2.1.1": 4,                    # figyelmeztetés
    "1.3.6.1.2.1.43.18.1.1.8.1.1": "Cartridge low",
    "1.3.6.1.2.1.1.1.0": "Lexmark MX722adhe version MXTGM.050.041 <b>",
    "1.3.6.1.2.1.1.3.0": 12345678,                       # századmásodperc
    "1.3.6.1.2.1.25.3.2.1.3.1": "Lexmark MX722adhe",
    "1.3.6.1.2.1.43.5.1.1.17.1": "5051234ABC",
    IN + "13.1.1": "Tray 1", IN + "13.1.2": "MP Tray", IN + "13.1.3": "Tray 2",
    IN + "10.1.1": 0, IN + "10.1.2": 0, IN + "10.1.3": 400,   # Tray 1 és MP Tray üres
    IN + "9.1.1": 550, IN + "9.1.2": 100, IN + "9.1.3": 550,
    OUT + "7.1.1": "Standard Bin",
    OUT + "5.1.1": 0,                                    # a kimeneti tálca tele
}


def _key(oid):
    return tuple(int(x) for x in oid.split("."))


def _encode(value):
    if isinstance(value, int):
        return snmp._int(value)
    return snmp._tlv(0x04, value.encode("utf-8"))


class FakeAgent:
    def __init__(self, data=None, community="public"):
        self.data = dict(DEFAULT_DATA if data is None else data)
        self.community = community
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._running = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._running.set()
        self._thread.start()
        return self

    def stop(self):
        self._running.clear()
        self._thread.join(timeout=2)
        self._sock.close()

    def _serve(self):
        keys = sorted(self.data, key=_key)
        while self._running.is_set():
            try:
                packet, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                reply = self._answer(packet, keys)
            except Exception:
                continue
            if reply:
                self._sock.sendto(reply, addr)

    def _answer(self, packet, keys):
        _, msg, _ = snmp._read(packet, 0)
        _, _, p = snmp._read(msg, 0)
        _, community, p = snmp._read(msg, p)
        if community.decode() != self.community:
            return None                                  # rossz közösségnév: a nyomtató nem válaszol
        _, pdu, _ = snmp._read(msg, p)
        _, rid, q = snmp._read(pdu, 0)
        _, _, q = snmp._read(pdu, q)
        _, _, q = snmp._read(pdu, q)
        _, varbinds, _ = snmp._read(pdu, q)
        _, varbind, _ = snmp._read(varbinds, 0)
        _, oid_bytes, _ = snmp._read(varbind, 0)
        asked = snmp._oid_str(oid_bytes)
        nxt = next((k for k in keys if _key(k) > _key(asked)), None)
        if nxt:
            vb = snmp._tlv(0x30, snmp._oid(nxt) + _encode(self.data[nxt]))
        else:
            vb = snmp._tlv(0x30, snmp._oid(asked) + b"\x82\x00")      # endOfMibView
        pdu_out = snmp._tlv(0xA2, snmp._tlv(0x02, rid) + snmp._int(0) + snmp._int(0)
                            + snmp._tlv(0x30, vb))
        return snmp._tlv(0x30, snmp._int(1) + snmp._tlv(0x04, community) + pdu_out)
