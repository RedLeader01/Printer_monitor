"""Minimális SNMP v2c kliens (BER kódolás), csak a Python standard könyvtárával.

Egyetlen dolgot tud: walk() - egy OID-fa végigjárása GETNEXT kérésekkel.
"""
import random
import socket


class SnmpTimeout(RuntimeError):
    pass


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
    if tag in (0x41, 0x42, 0x43, 0x46):          # counter32, gauge32, timeticks, counter64
        return int.from_bytes(val, "big")
    if tag == 0x04:                              # octet string
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


def walk(ip, oid, community="public", port=161, timeout=2.0, retries=1):
    """Végigjárja az OID-fát -> {index-utótag: érték}.

    A kulcs az OID-nak az alapon túli része (pl. "1.2"). Ha a nyomtató nem
    válaszol, SnmpTimeout kivételt dob (a RuntimeError leszármazottja).
    """
    base = oid.strip(".")
    prefix = base + "."
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    out, cur = {}, base
    try:
        for _ in range(500):
            rid = random.randint(1, 2 ** 30)
            packet = _getnext_packet(community, cur, rid)
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
                except (socket.timeout, ConnectionResetError, ConnectionRefusedError):
                    # Windowson, ha a nyomtatón az SNMP ki van kapcsolva, a zárt port "kapcsolat
                    # visszautasítva" hibaként érkezik vissza. Ez is "nincs válasz".
                    pass
                if resp:
                    break
            if not resp:
                raise SnmpTimeout("nincs SNMP válasz (időtúllépés) - SNMP be van kapcsolva? jó az IP/community?")
            _, es, noid, vtag, val = resp
            if es != 0 or vtag >= 0x80 or not noid.startswith(prefix) or noid == cur:
                break
            out[noid[len(prefix):]] = val
            cur = noid
    finally:
        sock.close()
    return out
