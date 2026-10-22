"""Parancssori belépési pont: figyelő indítása vagy egy nyomtató gyors tesztje."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import config, printers
from .config import ROOT
from .history import History
from .monitor import Monitor
from .notify import Notifier
from .web import make_server


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python run.py", description="Nyomtatófigyelő (SNMP)")
    ap.add_argument("--config", help="másik konfigurációs fájl (alapból a config.ini)")
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("test", help="egyetlen nyomtató lekérdezése, webszerver nélkül")
    t.add_argument("ip", help="a nyomtató IP-címe")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")    # a konzol kódolása ne okozzon hibát az ékezetekkel

    if args.cmd == "test":
        # A teszthez csak a közösségnév kell; ha van config.ini, onnan veszi, egyébként "public".
        # Nem hoz létre és nem módosít semmit.
        cfg = config.Config()
        path = Path(args.config) if args.config else config.DEFAULT_PATH
        if path.exists():
            try:
                cfg = config.load(path)
            except config.ConfigError:
                pass
        r = printers.poll(args.ip, args.ip, cfg.community, cfg.snmp_port)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(e)
        return 1

    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
    monitor = Monitor(cfg, notifier)
    try:
        server = make_server(cfg.host, cfg.port, monitor, ROOT / "web")
    except OSError as e:
        print("Nem sikerült elindítani a webszervert a(z) {}:{} címen: {}".format(cfg.host, cfg.port, e))
        print("Lehet, hogy a port foglalt. Állíts be másikat a config.ini [web] szakaszában.")
        return 1

    # Az előzményeket csak a webszerver sikeres létrehozása után nyitjuk meg, hogy egy foglalt port
    # miatt félbemaradt indítás ne módosítsa az adatbázist.
    history_note = "ki"
    if cfg.history_enabled:
        try:
            monitor.history = History(cfg.history_path, cfg.history_keep_days)
            monitor.history.start(cfg.poll_seconds)
            history_note = "be ({}, {} napig őrzi)".format(cfg.history_path.name, cfg.history_keep_days)
        except (sqlite3.Error, OSError) as e:
            monitor.history = None
            history_note = "KI - nem sikerült megnyitni a(z) {} fájlt: {}".format(cfg.history_path, e)

    monitor.start()
    print("{} nyomtató figyelve, lekérdezés {} másodpercenként.".format(len(cfg.printers), cfg.poll_seconds))
    print("Telegram értesítés: {}".format("be" if notifier.telegram_enabled else "ki (csak a konzolra ír)"))
    print("Előzmények: {}".format(history_note))
    print("Fut: http://{}:{}   (leállítás: Ctrl+C)".format(cfg.host, cfg.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLeállítás...")
    finally:
        server.server_close()
        if monitor.history is not None:
            monitor.history.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
