"""A config.ini beolvasása és ellenőrzése."""
import configparser
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.ini"
EXAMPLE_PATH = ROOT / "config.example.ini"


class ConfigError(Exception):
    """Érthető hibaüzenetű konfigurációs hiba."""


class Config:
    def __init__(self):
        self.community = "public"
        self.poll_seconds = 30
        self.warn_pct = 20
        self.crit_pct = 10
        self.snmp_port = 161
        self.host = "127.0.0.1"
        self.port = 8080
        self.printers = []          # [(név, ip), ...] a megadott sorrendben
        self.telegram_token = None
        self.telegram_chat_id = None


def _int(cp, section, key, default, minimum=None, maximum=None):
    if not cp.has_option(section, key):
        return default
    raw = cp.get(section, key).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("[{}] {}: egész számot várok, ezt kaptam: {!r}".format(section, key, raw))
    if minimum is not None and value < minimum:
        raise ConfigError("[{}] {}: legalább {} legyen (most: {})".format(section, key, minimum, value))
    if maximum is not None and value > maximum:
        raise ConfigError("[{}] {}: legfeljebb {} lehet (most: {})".format(section, key, maximum, value))
    return value


def _str(cp, section, key, default):
    if not cp.has_option(section, key):
        return default
    return cp.get(section, key).strip() or default


def load(path=None):
    """Beolvassa a konfigurációt. Ha a config.ini még nincs meg, létrehozza a példából."""
    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        if path == DEFAULT_PATH and EXAMPLE_PATH.exists():
            shutil.copyfile(str(EXAMPLE_PATH), str(path))
            raise ConfigError(
                "Még nem volt config.ini, ezért létrehoztam a példából:\n  {}\n"
                "Írd bele a saját nyomtatóid nevét és IP-címét, majd indítsd újra.".format(path))
        raise ConfigError("Nem találom a konfigurációs fájlt: {}".format(path))

    cp = configparser.ConfigParser(
        interpolation=None,
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=("#", ";"),
    )
    cp.optionxform = str            # a nyomtatónevek kis/nagybetűje maradjon meg
    try:
        cp.read(str(path), encoding="utf-8-sig")
    except configparser.Error as e:
        raise ConfigError("Hiba a(z) {} fájlban:\n{}".format(path, e))

    cfg = Config()
    cfg.community = _str(cp, "general", "community", cfg.community)
    cfg.poll_seconds = _int(cp, "general", "poll_seconds", cfg.poll_seconds, minimum=5)
    cfg.warn_pct = _int(cp, "general", "warn_pct", cfg.warn_pct, minimum=0, maximum=100)
    cfg.crit_pct = _int(cp, "general", "crit_pct", cfg.crit_pct, minimum=0, maximum=100)
    cfg.snmp_port = _int(cp, "general", "snmp_port", cfg.snmp_port, minimum=1, maximum=65535)
    cfg.host = _str(cp, "web", "host", cfg.host)
    cfg.port = _int(cp, "web", "port", cfg.port, minimum=1, maximum=65535)
    cfg.telegram_token = _str(cp, "telegram", "token", None)
    cfg.telegram_chat_id = _str(cp, "telegram", "chat_id", None)

    if not cp.has_section("printers"):
        raise ConfigError("A config.ini-ből hiányzik a [printers] szakasz.")
    for name, ip in cp.items("printers"):
        name, ip = name.strip(), ip.strip()
        if not name or not ip:
            raise ConfigError("[printers]: minden sorban 'Név = IP-cím' kell, hibás sor: {!r} = {!r}".format(name, ip))
        cfg.printers.append((name, ip))
    if not cfg.printers:
        raise ConfigError("A [printers] szakaszban nincs egyetlen nyomtató sem.")
    return cfg
