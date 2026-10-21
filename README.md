# Nyomtatófigyelő

Egyszerű, SNMP-alapú nyomtatófigyelő Pythonból. A program rendszeres időközönként lekérdezi a megadott nyomtatók kellékanyag-szintjét, állapotát, hibáit és egyéb adatait, majd egy böngészőből elérhető oldalon jeleníti meg. Új probléma esetén opcionálisan Telegram-értesítést küld.

**Semmit nem kell telepíteni:** csak Python 3.8 vagy újabb kell hozzá. A saját SNMP-klienst tartalmazza, ezért nincs szükség külső Python-csomagra, `snmpwalk` parancsra vagy WSL-re. Windowson és Linuxon is fut.

## Mappaszerkezet

```text
printer-monitor/
  run.py                 ← innen indítod a programot
  config.example.ini     ← példa beállítófájl (ebből lesz a config.ini)
  config.ini             ← a te beállításaid (nem kerül verziókezelésbe)
  printer_monitor/       ← a Python kód
    snmp.py              SNMP-kliens (walk)
    printers.py          egy nyomtató lekérdezése, Részletek adatai
    monitor.py           lekérdező ciklus, új problémák észlelése
    notify.py            értesítések (most: Telegram)
    config.py            a config.ini beolvasása és ellenőrzése
    web.py               kis webszerver: /api és a web/ mappa fájljai
    cli.py               parancssor (indítás, test)
  web/                   ← a weboldal (a böngészőben fut)
    index.html
    style.css
    app.js               kirajzolja a kártyákat a /api adataiból
  tests/                 ← automatikus tesztek hamis nyomtatóval
```

A Python csak adatot ad (`/api`), a kinézetet a `web/` mappa adja. A `style.css`-t és az `app.js`-t újraindítás nélkül is átírhatod, elég frissíteni a böngészőt.

## Követelmények

- Python 3.8 vagy újabb (ellenőrzés: `python --version`; Windowson ha a `python` nem működik, próbáld a `py` parancsot)
- SNMP v2c engedélyezve a nyomtatókon, a beállított közösségnévvel (alapértelmezés: `public`)
- A program futtató gépről elérhető a nyomtatók UDP 161-es portja

## Első indítás és beállítás

1. Másold le a `config.example.ini` fájlt `config.ini` néven, vagy egyszerűen indítsd el a programot: ha nincs `config.ini`, a program létrehozza a példából, és szól, hogy írd át.
2. Nyisd meg a `config.ini` fájlt (Jegyzettömb is jó, mentsd UTF-8 kódolással), és írd át a nyomtatókat.
3. Indítsd el:

```powershell
python run.py
```

Linuxon vagy macOS-en `python3 run.py`. Ezután nyisd meg a böngészőben: `http://localhost:8080`. A leállítás `Ctrl+C`.

### A config.ini beállításai

| Szakasz | Beállítás | Jelentés |
| --- | --- | --- |
| `[general]` | `community` | SNMP v2c közösségnév, egyeznie kell a nyomtatókon beállítottal (alapérték: `public`) |
| `[general]` | `poll_seconds` | Milyen gyakran kérdezze le a nyomtatókat, másodpercben (alapérték: 30, legalább 5) |
| `[general]` | `warn_pct` | Sárga jelzés és "Hamarosan elfogy" lista, ha a fogyóanyag ennyi % alá esik (alapérték: 20) |
| `[general]` | `crit_pct` | Piros jelzés ennyi % alatt (alapérték: 10) |
| `[general]` | `snmp_port` | A nyomtatók SNMP-portja, ritkán kell (alapérték: 161) |
| `[web]` | `host` | `127.0.0.1`: csak erről a gépről érhető el az oldal. `0.0.0.0`: a hálózat többi gépéről is |
| `[web]` | `port` | A weboldal portja (alapérték: 8080) |
| `[printers]` | `Név = IP-cím` | Egy sor egy nyomtató, a megadott sorrendben jelennek meg |
| `[telegram]` | `token`, `chat_id` | Opcionális, lásd lent |

A `;` és `#` jellel kezdődő sorok megjegyzések. Hibás érték esetén a program érthető hibaüzenettel áll le.

## Gyors teszt egy nyomtatóra

Mielőtt a teljes figyelőt elindítod, egyetlen nyomtatót ellenőrizhetsz. Ez nem indít webszervert, csak kiírja a lekérdezés eredményét:

```powershell
python run.py test NYOMTATO_IP
```

Mire figyelj:

- `"online": true` és kitöltött `supplies` lista: a lekérdezés működik.
- `"online": false` és `nincs SNMP válasz` hiba: az SNMP nincs bekapcsolva a nyomtatón, rossz az IP-cím vagy a közösségnév, vagy egy tűzfal blokkolja az UDP 161-es portot.
- Egy kellékanyagnál a `note` mezőben szereplő "van még (pontos szint nem ismert)" azt jelenti, hogy a nyomtató nem ad pontos százalékot (ez gyakori például a Xerox továbbítóhengernél és beégetőnél). Erre a program nem küld riasztást.

A `test` a `config.ini`-ből csak a közösségnevet használja (ha nincs `config.ini`, akkor `public`), és nem módosít semmit.

## A weboldal

Az oldal nyomtatónként egy kártyát mutat: állapotot, a kellékanyagok szintjét csíkkal és százalékkal, valamint az aktív hibákat. A kártya színe jelzi a helyzetet (zöld, sárga, piros), az elérhetetlen nyomtató **OFFLINE** jelzést kap. Az oldal magától, 15 másodpercenként frissül a háttérben, újratöltés nélkül. Ez csak a program által már lekért adatot olvassa újra; a nyomtatókat a `poll_seconds` szerint kérdezi le a program, függetlenül ettől. Ha a figyelő nem érhető el, az oldal tetején piros figyelmeztetés jelenik meg.

### Részletek menü

Minden online nyomtató kártyáján van egy lenyitható **Részletek** menü, amely a következőket mutatja, amennyit a nyomtató elárul magáról:

- típus és sorozatszám
- üzemidő (mióta nem indult újra a nyomtató)
- papírtálcák állapota (üres / van papír) és a kimeneti tálca állapota (tele / van hely); az üres fő tálca és a tele kimeneti tálca pirosan látszik. A kézi adagolók (MP Tray, Manual stb.) üresen is normál színűek, mert ezek általában üresek
- a fogyóanyagok cikkszáma (ha a nyomtató megadja, például a Xeroxnál)
- a nyomtató saját rendszerleírása (Lexmarknál ebben szokott lenni a firmware-verzió is)

Amit egy típus nem ad meg, az egyszerűen nem jelenik meg. A menü tartalma kizárólag tájékoztató, a Telegram-értesítést nem érinti. A lenyitott menük frissítéskor nyitva maradnak.

### Hamarosan elfogy lista

Ha bármelyik nyomtatón egy fogyóanyag a `warn_pct` érték alá esik, az oldal tetején megjelenik egy sárga **Hamarosan elfogy** doboz. Ebben nyomtatónként látszik a fogyóanyag neve, a szintje és (ha a nyomtató megadja) a cikkszáma, így közvetlenül rendelhető. A doboz magától eltűnik, ha nincs mit rendelni.

### JSON adatok

Az aktuális adatok JSON-formátumban is elérhetők, más programoknak is használhatók:

```text
http://localhost:8080/api
```

## Telegram-értesítések

Opcionálisan Telegram-üzenet küldhető, amikor egy nyomtatónál új probléma jelenik meg (kevés kellékanyag, hiba, vagy a nyomtató elérhetetlenné válik). Ehhez hozz létre egy Telegram-botot, majd add meg a bot tokent és a célcsevegés azonosítóját a `config.ini` `[telegram]` szakaszában:

```ini
[telegram]
token = 123456:ABC...
chat_id = -100123456
```

Ugyanezek környezeti változóként is megadhatók (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`), és azok felülírják a `config.ini` értékét. Ha egyik sincs beállítva, az értesítések csak a program konzoljára kerülnek.

A program csak az **újonnan** észlelt problémákról küld üzenetet, nem ismétli azokat minden lekérdezéskor. Ha egy probléma megszűnik, majd később újra megjelenik, az új értesítést eredményez.

## Tesztek

A projekt mappájából:

```powershell
python -m unittest -v
```

A tesztek egy hamis (helyi) SNMP-nyomtatót indítanak, és ellenőrzik a lekérdezést, a beállítások beolvasását, az értesítések logikáját és a webszervert is (többek között azt, hogy a `web/` mappán kívüli fájlok nem érhetők el). Nincs szükségük valódi nyomtatóra vagy hálózatra, és nem módosítanak semmit.

## Frissítés

A program frissítéséhez cseréld le a mappa tartalmát az újra, **kivéve a `config.ini` fájlt**, ami a te beállításaidat tartalmazza. Így nem kell újra beírni a nyomtatókat.

## Futtatás Linux szerveren

Ez akkor hasznos, ha a figyelőnek folyamatosan, a háttérben kell futnia. Debian vagy Ubuntu szerveren elég a Python 3:

```bash
sudo apt update
sudo apt install python3
```

Helyezd a mappát például az `/opt/printer-monitor` könyvtárba, hozd létre benne a `config.ini` fájlt, és ha más gépekről is el kell érni az oldalt, állítsd a `host` értékét `0.0.0.0`-ra. Kézi próba:

```bash
cd /opt/printer-monitor
python3 run.py test 192.168.1.101
python3 run.py
```

A tartós, automatikus indításhoz hozz létre egy `systemd` szolgáltatást:

```bash
sudo nano /etc/systemd/system/printer-monitor.service
```

Illeszd be a következőt (ha máshová telepítetted, módosítsd az útvonalakat):

```ini
[Unit]
Description=Nyomtatófigyelő
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/printer-monitor
ExecStart=/usr/bin/python3 /opt/printer-monitor/run.py
Restart=on-failure
RestartSec=10
EnvironmentFile=-/etc/printer-monitor.env

[Install]
WantedBy=multi-user.target
```

Engedélyezd és indítsd el a szolgáltatást:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now printer-monitor
sudo systemctl status printer-monitor
```

A napló megtekintése:

```bash
sudo journalctl -u printer-monitor -f
```

A Telegram-adatokat megadhatod a `config.ini`-ben, vagy az opcionális `/etc/printer-monitor.env` fájlban (`TELEGRAM_TOKEN=...` és `TELEGRAM_CHAT_ID=...` sorokkal). Ez utóbbit korlátozd: `sudo chmod 600 /etc/printer-monitor.env`. A módosítás után indítsd újra a szolgáltatást: `sudo systemctl restart printer-monitor`.

## Megjegyzések és biztonság

- **A `config.ini` ne kerüljön nyilvános tárolóba.** Tartalmazza a belső IP-címeket és a Telegram-token is lehet benne. A mellékelt `.gitignore` ezt megakadályozza a Git használatakor; csak a `config.example.ini` kerül be.
- Az SNMP v2c nem titkosítja a forgalmat, a közösségnevet szövegesen küldi. Csak megbízható belső hálózaton használd, és ahol lehet, ne hagyd az alapértelmezett `public` értéket.
- A webkiszolgáló alapból csak a helyi gépről érhető el (`127.0.0.1`). Ha a `host` értékét `0.0.0.0`-ra állítod, a felület a hálózaton bárkinek elérhető lesz, mert nincs hitelesítés vagy HTTPS. Ilyenkor tűzfallal korlátozd a portot a megbízható hálózatra. A webszerver csak olvasni enged: nyomtatót nem tud vezérelni.
- A nyomtatókat a program párhuzamosan kérdezi le (egyszerre legfeljebb 8-at), így egy elérhetetlen nyomtató nem lassítja a többit. Az offline nyomtató lekérdezése kb. 4 másodpercig vár az időtúllépésre.
- A 30 másodperces lekérdezés a hálózatot és a nyomtatókat gyakorlatilag nem terheli (nyomtatónként kb. 50-60 apró UDP csomag).
- Hibaként a program a nyomtató riasztási tábláját (kritikus és figyelmeztető szint) jeleníti meg. Az információs szintű üzeneteket kihagyja. Ha egy nyomtató egy hibát (például nyomtatófej-hibát) nem ebben a táblában jelez, a `test` parancs kimenetéből derül ki, hol jelenik meg.
- Ha egy nyomtató nem érhető el, az oldal `OFFLINE` állapotot és a lekérdezési hibát mutatja. A hiányzó vagy nem értelmezhető kellékanyag-szint "ismeretlen" vagy "van még" jelzéssel látszik.
- A kellékanyagok nevét a nyomtató adja meg; a cikkszám és a sorozatszám részt a program leválasztja a névről, a cikkszámot külön mutatja.
