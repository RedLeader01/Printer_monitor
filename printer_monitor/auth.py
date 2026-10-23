"""Hitelesítés a nyomtatók kezeléséhez (hozzáadás, törlés).

Két út van, és együtt működnek:

* **A futtató gépről (localhost) nem kell belépni.** Ha a böngésző ugyanazon a gépen fut, mint a
  program, és `http://localhost:PORT` címen éri el, akkor admin. (Ha a program egy reverse proxy
  mögött fut, vagy a kérés a gép saját hálózati címén érkezik, ez nem érvényes.)
* **Máshonnan admin fiókkal kell belépni.** Egyszeri belépés elég: a jelszó helyett egy munkamenet
  sütit kap a böngésző, így 30 nyomtató felvételéhez sem kell újra és újra jelszót írni.
  Az admin fiókot a futtató gépen lehet létrehozni: `python run.py admin set NÉV`.

Védelem: a jelszó PBKDF2-vel van hash-elve, a belépési próbálkozások korlátozottak, a munkamenet
süti HttpOnly és SameSite=Strict, a módosító kérésekhez CSRF-jelzés és azonos eredet kell
(ez a localhost-ról érkező kéréseknél is, mert egy másik weboldal a böngészőn át elérhetné a
localhost-ot).

Figyelem: a program HTTP-n szolgál ki (nincs HTTPS), ezért a belépéskor a jelszó titkosítatlanul
utazik a hálózaton. Csak megbízható belső hálózaton használd.
"""
import base64
import hashlib
import hmac
import http.cookies
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit

PBKDF2_ITERATIONS = 600000
MIN_PASSWORD = 8
MAX_PASSWORD = 256
COOKIE_NAME = "pm_session"

LOGIN_MAX_FAILS = 5            # ennyi hibás próbálkozás után zár ki egy címet ...
LOGIN_WINDOW = 600             # ... ennyi másodpercre (és ennyi idő alatt számolja a próbákat)
GLOBAL_MAX_FAILS = 40          # az összes címről együtt ennyi hibás próba / ablak

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]"}
# Ha ezek valamelyike megvan, a kérés valószínűleg reverse proxyn át jött, tehát a "localhost"
# kapcsolat valójában távoli felhasználó is lehet: ilyenkor nem adunk automatikus admin jogot.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
                 "x-real-ip", "forwarded", "via")


class AuthError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class PasswordError(ValueError):
    """A jelszó nem felel meg a szabályoknak (érthető magyar üzenettel)."""


# ----------------------------------------------------------------------- jelszó-hash
def hash_password(password, iterations=None):
    """'pbkdf2_sha256$ITER$SÓ$HASH' alakú szöveg; a só véletlen, az iterációszám benne van."""
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode("ascii"),
                                           base64.b64encode(digest).decode("ascii"))


def verify_password(password, stored):
    """Igaz, ha a jelszó egyezik a tárolt hash-sel. Hibás formátumú hash-nél hamis (nem kivétel)."""
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password_rules(username, password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise PasswordError("A jelszó legalább {} karakter legyen.".format(MIN_PASSWORD))
    if len(password) > MAX_PASSWORD:
        raise PasswordError("A jelszó legfeljebb {} karakter lehet.".format(MAX_PASSWORD))
    if password.strip().lower() == (username or "").strip().lower():
        raise PasswordError("A jelszó ne egyezzen meg a felhasználónévvel.")


def set_admin_password(store, username, password):
    """Admin fiók létrehozása vagy a jelszavának cseréje. Visszaadja a (normalizált) felhasználónevet."""
    from .store import normalize_username
    username = normalize_username(username)
    check_password_rules(username, password)
    store.set_admin_hash(username, hash_password(password))
    return username


def _stamp(pw_hash):
    """A hash ujjlenyomata: ha a jelszót megváltoztatják, a régi munkamenetek érvénytelenek lesznek."""
    return hashlib.sha256(pw_hash.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------- kérés-ellenőrzések
def _lower_headers(headers):
    return {k.lower(): v for k, v in headers.items()}


def same_origin(headers):
    """A böngésző jelzései szerint a kérés a saját oldalunkról jött-e (nem másik weboldalról)."""
    hd = _lower_headers(headers)
    site = hd.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = hd.get("origin")
    if origin is not None:
        host = hd.get("host")
        if not host or origin == "null":
            return False
        try:
            netloc = urlsplit(origin).netloc
        except ValueError:
            return False
        if netloc.lower() != host.lower():
            return False
    return True


def parse_session_cookie(headers):
    raw = _lower_headers(headers).get("cookie")
    if not raw:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(raw)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


def session_cookie(token, max_age):
    return "{}={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}".format(COOKIE_NAME, token, int(max_age))


class Principal:
    """Ki küldte a kérést: 'local' (a futtató gép), 'admin' (belépett fiók) vagy 'anon'."""

    def __init__(self, kind="anon", user=None, csrf=None, expires=None):
        self.kind = kind
        self.user = user
        self.csrf = csrf
        self.expires = expires

    @property
    def is_admin(self):
        return self.kind in ("local", "admin")


class Auth:
    def __init__(self, store, trust_localhost=True, session_hours=8, clock=time.time):
        self.store = store
        self.trust_localhost = trust_localhost
        self.session_seconds = int(session_hours * 3600)
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}                      # token -> {user, csrf, stamp, expires}
        self._local_csrf = secrets.token_urlsafe(32)
        self._fails = {}                         # cím -> [időpontok]
        self._global_fails = []
        self._dummy_hash = None
        self._busy = threading.BoundedSemaphore(4)       # egyszerre legfeljebb ennyi jelszó-ellenőrzés fut

    # ------------------------------------------------------------ azonosítás
    def _is_local(self, client_ip, hd):
        if not self.trust_localhost:
            return False
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if not addr.is_loopback:
            return False
        if any(name in hd for name in PROXY_HEADERS):
            return False
        host = (hd.get("host") or "").strip().lower()
        hostname = host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
        return hostname in LOCAL_HOSTNAMES

    def identify(self, client_ip, headers):
        hd = _lower_headers(headers)
        if self._is_local(client_ip, hd):
            return Principal("local", "localhost", self._local_csrf)
        token = parse_session_cookie(hd)
        if not token:
            return Principal()
        now = self._clock()
        with self._lock:
            self._purge(now)
            sess = self._sessions.get(token)
            if sess is None:
                return Principal()
            user, stamp, csrf, expires = sess["user"], sess["stamp"], sess["csrf"], sess["expires"]
        # A fiók közben megszűnhetett, vagy megváltozhatott a jelszava: akkor a munkamenet is érvénytelen.
        current = self.store.get_admin_hash(user)
        if current is None or _stamp(current) != stamp:
            with self._lock:
                self._sessions.pop(token, None)
            return Principal()
        return Principal("admin", user, csrf, expires)

    def check_csrf(self, principal, token):
        if not principal.csrf or not isinstance(token, str):
            return False
        return hmac.compare_digest(token.encode("utf-8", "replace"), principal.csrf.encode("utf-8"))

    # ----------------------------------------------------------------- belépés
    def _purge(self, now):
        for tok in [t for t, s in self._sessions.items() if s["expires"] <= now]:
            del self._sessions[tok]

    def _recent(self, stamps, now):
        return [t for t in stamps if now - t < LOGIN_WINDOW]

    def _dummy(self):
        if self._dummy_hash is None:
            self._dummy_hash = hash_password(secrets.token_urlsafe(12))
        return self._dummy_hash

    def login(self, client_ip, username, password):
        """Sikeres belépésnél (token, csrf, felhasználónév, lejárat). Hibánál AuthError."""
        now = self._clock()
        with self._lock:
            mine = self._fails[client_ip] = self._recent(self._fails.get(client_ip, []), now)
            self._global_fails = self._recent(self._global_fails, now)
            if len(mine) >= LOGIN_MAX_FAILS or len(self._global_fails) >= GLOBAL_MAX_FAILS:
                oldest = (mine if len(mine) >= LOGIN_MAX_FAILS else self._global_fails)[0]
                wait = max(1, int(oldest + LOGIN_WINDOW - now))
                raise AuthError(429, "Túl sok sikertelen belépési kísérlet. Próbáld újra {} perc múlva."
                                .format(max(1, (wait + 59) // 60)), retry_after=wait)
            # A kísérletet előre beírjuk (sikeres belépésnél töröljük), így a párhuzamos kérések sem
            # tudják megkerülni a korlátot, amíg a lassú jelszó-ellenőrzés fut.
            mine.append(now)
            self._global_fails.append(now)
            if len(self._fails) > 1000:
                self._fails = {ip: v for ip, v in ((i, self._recent(x, now)) for i, x in self._fails.items()) if v}
                self._fails.setdefault(client_ip, mine)
        if not self._busy.acquire(blocking=False):
            raise AuthError(429, "A kiszolgáló most sok belépési kérést kap. Próbáld újra egy perc múlva.", retry_after=60)
        try:
            return self._finish_login(client_ip, username, password, now)
        finally:
            self._busy.release()

    def _finish_login(self, client_ip, username, password, now):
        username = username.strip() if isinstance(username, str) else ""
        try:
            username.encode("utf-8")
        except UnicodeEncodeError:
            username = ""
        valid_input = bool(username) and isinstance(password, str) and 0 < len(password) <= MAX_PASSWORD
        stored = self.store.get_admin_hash(username) if valid_input else None
        # Ismeretlen felhasználónál is ugyanannyi munkát végzünk, hogy az idő ne áruljon el semmit.
        matches = verify_password(password if valid_input else "", stored or self._dummy())
        if not (valid_input and stored and matches):
            raise AuthError(401, "Hibás felhasználónév vagy jelszó.")

        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = now + self.session_seconds
        with self._lock:
            self._purge(now)
            self._fails.pop(client_ip, None)
            if now in self._global_fails:
                self._global_fails.remove(now)
            self._sessions[token] = {"user": username, "csrf": csrf, "stamp": _stamp(stored), "expires": expires}
        return token, csrf, username, expires

    def logout(self, token):
        if token:
            with self._lock:
                self._sessions.pop(token, None)
