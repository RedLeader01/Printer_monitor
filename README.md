# Nyomtatófigyelő

Egyszerű, SNMP-alapú nyomtatófigyelő Pythonból. A program rendszeres időközönként lekérdezi a megadott nyomtatók kellékanyag-szintjét, állapotát és hibáit, majd ezeket egy böngészőből elérhető oldalon jeleníti meg. Új probléma esetén opcionálisan Telegram-értesítést küld.

A program **egyetlen fájl** (`printer_monitor.py`), és a saját SNMP v2c klienst tartalmazza. Ezért **semmit nem kell hozzá telepíteni**: nincs szükség külső Python-csomagra, `snmpwalk` parancsra, WSL-re vagy Net-SNMP-re. Windowson és Linuxon is fut.

## Követelmények

- Python 3.8 vagy újabb
- SNMP v2c engedélyezése a nyomtatókon, a beállított közösségnévvel (alapértelmezés: `public`)
- UDP 161-es portú elérés a nyomtatók felé a program futtató gépről

A Python verzióját a következő paranccsal ellenőrizheted (Windowson, ha a `python` nem működik, próbáld a `py` indítót):

```powershell
python --version
```

## Beállítás

Nyisd meg a `printer_monitor.py` fájlt, és módosítsd a fájl elején lévő beállításokat:

```python
PRINTERS = [
    ("Iroda 1", "192.168.1.101"),
    ("Iroda 2", "192.168.1.102"),
]
COMMUNITY = "public"
POLL_SECONDS = 30
WARN_PCT = 20
CRIT_PCT = 10
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8080
```

| Beállítás | Jelentés |
| --- | --- |
| `PRINTERS` | A nyomtatók megjelenített neve és IP-címe. A fájlban kezdetben `dummy` példaadatok vannak, ezeket cseréld le a saját nyomtatóidra. |
| `COMMUNITY` | Az SNMP v2c közösségnév. Egyeznie kell a nyomtatókon beállítottal. |
| `POLL_SECONDS` | A nyomtatók lekérdezésének gyakorisága másodpercben (alapérték: 30). |
| `WARN_PCT` | Sárga figyelmeztetés a kellékanyag ennyi százalék alatt (alapérték: 20). |
| `CRIT_PCT` | Piros jelzés a kellékanyag ennyi százalék alatt (alapérték: 10). |
| `HTTP_HOST` | Melyik címen figyeljen a weboldal. Az alapérték (`127.0.0.1`) csak a saját gépről engedi az elérést. Hogy más gépekről is látszódjon, állítsd `"0.0.0.0"`-ra. |
| `HTTP_PORT` | A weboldal portja (alapérték: 8080). |

## Gyors teszt egy nyomtatóra

Mielőtt a teljes figyelőt elindítod, egyetlen nyomtatót ellenőrizhetsz. Ez nem indít webszervert, csak kiírja a lekérdezés eredményét:

```powershell
python printer_monitor.py test NYOMTATO_IP
```

Linuxon `python3`-mal indítsd. A kimenet JSON, ahol az ékezetes betűk kódolva jelennek meg (például `készenlét` = „készenlét”); ez csak a teszt kimenetére igaz, a weboldalon normálisan látszanak.

Mire figyelj:

- `"online": true` és kitöltött `supplies` lista: a lekérdezés működik.
- `"online": false` és `nincs SNMP válasz (időtúllépés)` hiba: az SNMP nincs bekapcsolva a nyomtatón, rossz az IP-cím vagy a közösségnév, vagy egy tűzfal blokkolja az UDP 161-es portot.
- Egy kellékanyagnál a `note` mezőben szereplő „van még (pontos szint nem ismert)” azt jelenti, hogy a nyomtató nem ad pontos százalékot (ez gyakori például a Xerox továbbítóhengernél és beégetőnél). Erre a program nem küld riasztást.

## Indítás és használat

Windows PowerShellben:

```powershell
python printer_monitor.py
```

Linuxon vagy macOS-en:

```bash
python3 printer_monitor.py
```

Nyisd meg a böngészőben a `http://localhost:8080` címet. Ha a `HTTP_HOST` értéke `0.0.0.0`, más gépről a `http://A_GEP_IP_CIME:8080` címen érhető el. A leállítás `Ctrl+C`-vel történik.

Az oldal nyomtatónként egy kártyát mutat: állapotot, a kellékanyagok szintjét csíkkal és százalékkal, valamint az aktív hibákat. A kártya színe jelzi a helyzetet (zöld, sárga, piros). A böngészőoldal 60 másodpercenként frissül, de ez csak a már lekért adatot olvassa újra; a nyomtatókat a `POLL_SECONDS` szerint kérdezi le a program, függetlenül ettől. Indulás után az állapotok fokozatosan jelennek meg, ahogy az egyes nyomtatók sorra kerülnek.

Az aktuális adatok JSON-formátumban is elérhetők:

```text
http://localhost:8080/api
```

## Telegram-értesítések

Opcionálisan Telegram-üzenet küldhető, amikor egy nyomtatónál új probléma jelenik meg (kevés kellékanyag, hiba, vagy a nyomtató elérhetetlenné válik). Ehhez hozz létre egy Telegram-botot, majd állítsd be a bot tokent és a célcsevegés azonosítóját környezeti változóként.

Windows PowerShellben:

```powershell
$env:TELEGRAM_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID = "-100123456"
python printer_monitor.py
```

Linuxon vagy macOS-en:

```bash
export TELEGRAM_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="-100123456"
python3 printer_monitor.py
```

Ha valamelyik változó hiányzik, az értesítések csak a program konzoljára kerülnek. A program csak az **újonnan** észlelt problémákról küld üzenetet, nem ismétli azokat minden lekérdezéskor. Ha egy probléma megszűnik, majd később újra megjelenik, az új értesítést eredményez.

## Futtatás Linux szerveren

Ez akkor hasznos, ha a figyelőnek folyamatosan, a háttérben kell futnia. Debian vagy Ubuntu szerveren elég a Python 3 (az SNMP-hez nem kell további csomag):

```bash
sudo apt update
sudo apt install python3
```

Helyezd a programot például az `/opt/printer-monitor` könyvtárba, módosítsd benne a nyomtatók adatait, és ha más gépekről is el kell érni az oldalt, állítsd a `HTTP_HOST` értékét `"0.0.0.0"`-ra. Kézi indítás és teszt:

```bash
cd /opt/printer-monitor
python3 printer_monitor.py test 192.168.1.101
python3 printer_monitor.py
```

A tartós, automatikus indításhoz hozz létre egy `systemd` szolgáltatást. Az alábbi példában az útvonal `/opt/printer-monitor/printer_monitor.py`; ha máshová telepítetted, módosítsd az útvonalakat.

```bash
sudo nano /etc/systemd/system/printer-monitor.service
```

Illeszd be a következőt:

```ini
[Unit]
Description=Nyomtatófigyelő
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/printer-monitor
ExecStart=/usr/bin/python3 /opt/printer-monitor/printer_monitor.py
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

Telegram használatához hozd létre az opcionális környezeti fájlt:

```bash
sudo nano /etc/printer-monitor.env
```

Tartalma:

```text
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-100123456
```

Korlátozd a fájl olvasását, majd indítsd újra a szolgáltatást:

```bash
sudo chmod 600 /etc/printer-monitor.env
sudo systemctl restart printer-monitor
```

## Megjegyzések és biztonság

- Az SNMP v2c nem titkosítja a forgalmat, a közösségnevet szövegesen küldi. Csak megbízható belső hálózaton használd, és ahol lehet, ne hagyd az alapértelmezett `public` értéket.
- A webkiszolgáló alapból csak a helyi gépről érhető el (`127.0.0.1`). Ha a `HTTP_HOST` értékét `0.0.0.0`-ra állítod, a felület a hálózaton bárkinek elérhető lesz, mert nincs hitelesítés vagy HTTPS. Ilyenkor tűzfallal korlátozd a `HTTP_PORT` portot a megbízható hálózatra.
- A nyomtatókat a program egymás után kérdezi le. Az elérhetetlen (offline) nyomtató lekérdezése kb. 4 másodpercet vár az időtúllépésre, ezért sok offline eszköz esetén a teljes kör hosszabb lehet a `POLL_SECONDS` értékénél. Ez a hat-hét nyomtatós használatnál nem okoz gondot.
- A 30 másodperces lekérdezés a hálózatot és a nyomtatókat gyakorlatilag nem terheli (nyomtatónként néhány tucat apró UDP csomag). A kellékanyagok lassan változnak, de így a hibák gyorsan, 30 másodpercen belül észrevehetők.
- Hibaként a program a nyomtató riasztási tábláját (kritikus és figyelmeztető szint) jeleníti meg. Az információs szintű üzeneteket kihagyja. Ha egy nyomtató egy hibát (például nyomtatófej-hibát) nem ebben a táblában jelez, a `test` paranccsal futtatott lekérdezés kimenetéből derül ki, hol jelenik meg.
- Ha egy nyomtató nem érhető el, az oldal `OFFLINE` állapotot és a lekérdezési hibát mutatja. A hiányzó vagy nem értelmezhető kellékanyag-szint „ismeretlen” vagy „van még” jelzéssel látszik.
- A kellékanyagok nevét a nyomtató adja meg, ezért tartalmazhat cikkszámot és sorozatszámot (például `PN 106R03396;SN...`).
