"""Parancssori belépési pont: figyelő indítása, egy nyomtató gyors tesztje, admin fiókok, import."""
import argparse
import getpass
import json
import sqlite3
import sys
import time
from pathlib import Path

from . import auth as authmod
from . import config, printers
from .config import ROOT
from .history import History
from .monitor import Monitor
from .notify import Notifier
from .store import PrinterStore, StoreError, parse_bulk
from .web import make_server


def _lenient_config(path_arg):
    """A config.ini-t beolvassa, ha megvan; ha nincs, az alapértékeket adja (és nem hoz létre semmit)."""
    path = Path(path_arg) if path_arg else config.DEFAULT_PATH
    if path.exists():
        return config.load(path)
    return config.Config()


def _admin_command(args, cfg, getpass_fn):
    try:
        store = PrinterStore(cfg.printers_path)
    except (sqlite3.Error, OSError) as e:
        print("Nem sikerült megnyitni a(z) {} fájlt: {}".format(cfg.printers_path, e))
        return 1
    try:
        if args.action == "list":
            admins = store.list_admins()
            if not admins:
                print("Még nincs admin fiók. Létrehozás: python run.py admin set NÉV")
                print("(A futtató gépen, localhost-ról a kezeléshez nem kell fiók.)")
            for a in admins:
                print("{}   (létrehozva: {})".format(a["username"], time.strftime("%Y-%m-%d", time.localtime(a["created"]))))
            return 0
        if not args.name:
            print("Add meg a felhasználónevet, pl.: python run.py admin {} NÉV".format(args.action))
            return 1
        if args.action == "remove":
            if store.remove_admin(args.name):
                print("Az admin fiók törölve: {}".format(args.name))
                return 0
            print("Nincs ilyen admin fiók: {}".format(args.name))
            return 1
        # set: létrehozás vagy jelszócsere
        try:
            existed = store.get_admin_hash(args.name) is not None
            password = getpass_fn("Jelszó (legalább {} karakter): ".format(authmod.MIN_PASSWORD))
            again = getpass_fn("Jelszó még egyszer: ")
            if password != again:
                print("A két jelszó nem egyezik, nem történt változás.")
                return 1
            user = authmod.set_admin_password(store, args.name, password)
        except (authmod.PasswordError, StoreError) as e:
            print(e)
            return 1
        print("{} {}.".format("Új jelszó beállítva:" if existed else "Admin fiók létrehozva:", user))
        if existed:
            print("A korábbi belépések érvénytelenek lettek.")
        return 0
    finally:
        store.close()


def _printers_command(args, cfg):
    """Nyomtatók kezelése parancssorból (pl. GUI nélküli Linux szerveren)."""
    try:
        store = PrinterStore(cfg.printers_path)
    except (sqlite3.Error, OSError) as e:
        print("Nem sikerült megnyitni a(z) {} fájlt: {}".format(cfg.printers_path, e))
        return 1
    try:
        if args.action == "list":
            items = store.list()
            if not items:
                print("Még nincs nyomtató felvéve. Felvétel: python run.py printers add NÉV IP, vagy printers import FÁJL")
            for p in items:
                print("{:<30} {}".format(p["name"], p["ip"]))
            return 0
        if args.action == "add":
            if len(args.rest) != 2:
                print("Használat: python run.py printers add NÉV IP-CÍM")
                return 1
            try:
                p = store.add(args.rest[0], args.rest[1])
            except StoreError as e:
                print(e)
                return 1
            print("Felvéve: {} ({})".format(p["name"], p["ip"]))
            return 0
        if args.action == "remove":
            if len(args.rest) != 1:
                print("Használat: python run.py printers remove NÉV")
                return 1
            match = [p for p in store.list() if p["name"].lower() == args.rest[0].lower()]
            if not match:
                print("Nincs ilyen nevű nyomtató: {}".format(args.rest[0]))
                return 1
            store.remove(match[0]["id"])
            print("Törölve: {} (az előzményei megmaradnak)".format(match[0]["name"]))
            return 0
        # import FÁJL: soronként 'Név = IP'
        if len(args.rest) != 1:
            print("Használat: python run.py printers import FÁJL   (soronként: Név = IP-cím)")
            return 1
        try:
            text = Path(args.rest[0]).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            print("Nem sikerült beolvasni a fájlt: {}".format(e))
            return 1
        items, errors = parse_bulk(text)
        added = 0
        for no, name, ip in items:
            try:
                store.add(name, ip)
                added += 1
            except StoreError as e:
                errors.append((no, "{}: {}".format(name, e)))
        print("{} nyomtató felvéve.".format(added))
        for no, why in sorted(errors):
            print("  {}. sor kihagyva: {}".format(no, why))
        return 0 if not errors else 1
    finally:
        store.close()


def _import_command(cfg):
    if not cfg.printers:
        print("A config.ini-ben nincs [printers] szakasz (vagy üres), nincs mit importálni.")
        return 1
    try:
        store = PrinterStore(cfg.printers_path)
    except (sqlite3.Error, OSError) as e:
        print("Nem sikerült megnyitni a(z) {} fájlt: {}".format(cfg.printers_path, e))
        return 1
    added, skipped = 0, []
    try:
        for name, ip in cfg.printers:
            try:
                store.add(name, ip)
                added += 1
            except StoreError as e:
                skipped.append((name, str(e)))
    finally:
        store.close()
    print("{} nyomtató átvéve a config.ini-ből ({}).".format(added, cfg.printers_path.name))
    for name, why in skipped:
        print("  kihagyva: {} - {}".format(name, why))
    print("A [printers] szakaszt innentől már nem használja a program, törölheted a config.ini-ből.")
    print("Ha a program közben fut, a nyomtatók a következő lekérdezési körben megjelennek.")
    return 0


def main(argv=None, getpass_fn=getpass.getpass):
    ap = argparse.ArgumentParser(prog="python run.py", description="Nyomtatófigyelő (SNMP)")
    ap.add_argument("--config", help="másik konfigurációs fájl (alapból a config.ini)")
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("test", help="egyetlen nyomtató lekérdezése, webszerver nélkül")
    t.add_argument("ip", help="a nyomtató IP-címe")
    a = sub.add_parser("admin", help="admin fiókok kezelése (a távoli belépéshez)")
    a.add_argument("action", choices=["set", "list", "remove"],
                   help="set: fiók létrehozása vagy jelszócsere, list: fiókok listája, remove: fiók törlése")
    a.add_argument("name", nargs="?", help="a fiók neve")
    pr = sub.add_parser("printers", help="nyomtatók kezelése parancssorból (list, add, remove, import)")
    pr.add_argument("action", choices=["list", "add", "remove", "import"],
                    help="list: lista, add NÉV IP, remove NÉV, import FÁJL (soronként 'Név = IP')")
    pr.add_argument("rest", nargs="*", help="az adott művelet adatai")
    sub.add_parser("import-printers", help="a config.ini régi [printers] szakaszának átvétele az adatbázisba")
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

    if args.cmd in ("admin", "printers", "import-printers"):
        try:
            cfg = _lenient_config(args.config)
        except config.ConfigError as e:
            print(e)
            return 1
        if args.cmd == "admin":
            return _admin_command(args, cfg, getpass_fn)
        if args.cmd == "printers":
            return _printers_command(args, cfg)
        return _import_command(cfg)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(e)
        return 1

    try:
        store = PrinterStore(cfg.printers_path)
    except (sqlite3.Error, OSError) as e:
        print("Nem sikerült megnyitni a nyomtatók adatbázisát ({}): {}".format(cfg.printers_path, e))
        return 1
    auth = authmod.Auth(store, cfg.trust_localhost, cfg.session_hours)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
    monitor = Monitor(cfg, notifier, store=store)
    try:
        server = make_server(cfg.host, cfg.port, monitor, ROOT / "web", auth)
    except OSError as e:
        store.close()
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
    count = store.count()
    admins = len(store.list_admins())
    print("{} nyomtató figyelve, lekérdezés {} másodpercenként.".format(count, cfg.poll_seconds))
    print("Telegram értesítés: {}".format("be" if notifier.telegram_enabled else "ki (csak a konzolra ír)"))
    print("Előzmények: {}".format(history_note))
    if cfg.trust_localhost:
        print("Nyomtatókezelés: a futtató gépről (localhost) belépés nélkül, máshonnan admin fiókkal ({} db).".format(admins))
    else:
        print("Nyomtatókezelés: csak admin fiókkal ({} db).".format(admins))
    if count == 0:
        if cfg.printers:
            print("Még nincs nyomtató felvéve, de a config.ini-ben van régi [printers] lista. "
                  "Átvétel: python run.py import-printers")
        else:
            print("Még nincs nyomtató felvéve. Nyisd meg a weboldalt, és a \"Nyomtató hozzáadása\" gombbal vedd fel őket.")
    if admins == 0:
        print("Távoli kezeléshez hozz létre admin fiókot: python run.py admin set NÉV")
    print("Fut: http://{}:{}   (leállítás: Ctrl+C)".format(cfg.host, cfg.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLeállítás...")
    finally:
        server.server_close()
        if monitor.history is not None:
            monitor.history.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
