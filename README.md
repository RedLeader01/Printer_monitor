# Nyomtatófigyelő

Egyszerű, SNMP-alapú nyomtatófigyelő Pythonból. A program rendszeres időközönként lekérdezi a megadott nyomtatók kellékanyag-szintjét, állapotát és riasztásait, majd ezeket egy böngészőből elérhető oldalon jeleníti meg. Külső Python-csomag nem szükséges; az `snmpwalk` parancsnak viszont telepítve és a `PATH`-ban elérhetőnek kell lennie.

## Követelmények

- Python 3
- Net-SNMP `snmpwalk` parancs
- SNMP v2c engedélyezése a nyomtatókon, a beállított közösségnévvel

Debian vagy Ubuntu rendszeren az `snmpwalk` telepítése:

```bash
sudo apt update
sudo apt install snmp
```

Windows rendszeren telepíts egy Net-SNMP-csomagot, és ellenőrizd, hogy az `snmpwalk.exe` elérhető-e a `PATH`-ból.

## Beállítás

Nyisd meg a `printer_monitor.py` fájlt, és módosítsd a fájl elején található beállításokat:

```python
PRINTERS = [
	("Iroda 1", "192.168.1.101"),
	("Iroda 2", "192.168.1.102"),
]
COMMUNITY = "public"
POLL_SECONDS = 300
WARN_PCT = 20
CRIT_PCT = 10
HTTP_PORT = 8080
```

A `PRINTERS` lista elemei a nyomtató megjelenített nevét és IP-címét tartalmazzák. A `COMMUNITY` értékének egyeznie kell a nyomtató SNMP v2c közösségnevével. A lekérdezési idő másodpercben értendő; az alapérték öt perc. A kellékanyag-szint 20% alatt figyelmeztető, 10% alatt kritikus színnel jelenik meg.

## Indítás és használat

Linuxon vagy macOS-en:

```bash
python3 printer_monitor.py
```

Windows PowerShellben:

```powershell
python printer_monitor.py
```

Nyisd meg a `http://SZERVER_IP:8080` címet, ahol a `SZERVER_IP` annak a gépnek az IP-címe, amelyen a program fut. A böngészőoldal 60 másodpercenként frissül; a nyomtatók lekérdezési gyakoriságát ettől függetlenül a `POLL_SECONDS` szabályozza. Indulás után az állapotok fokozatosan, a nyomtatók lekérdezésével jelennek meg.

Az aktuális adatok JSON-formátumban is elérhetők:

```text
http://SZERVER_IP:8080/api
```

## Telegram-értesítések

Opcionálisan Telegram-üzenet küldhető, amikor egy nyomtatónál új probléma jelenik meg. Ehhez hozz létre egy botot, és állítsd be a bot tokent, valamint a célcsevegés azonosítóját környezeti változóként.

Linuxon vagy macOS-en:

```bash
export TELEGRAM_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="-100123456"
python3 printer_monitor.py
```

Windows PowerShellben:

```powershell
$env:TELEGRAM_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID = "-100123456"
python printer_monitor.py
```

Ha valamelyik változó nincs megadva, az értesítések a program konzoljára kerülnek. A program csak az újonnan észlelt problémákról küld értesítést; a megszűnt probléma újbóli megjelenése új értesítést eredményez.

## Megjegyzések és biztonság

- Az SNMP v2c nem titkosítja a hálózati forgalmat. A közösségnevet és a nyomtatók elérhetőségét a hálózati környezethez igazítsd.
- A webkiszolgáló alapértelmezés szerint minden hálózati interfészen figyel (`0.0.0.0`), és nem használ hitelesítést vagy HTTPS-t. Csak megbízható belső hálózaton tedd elérhetővé; szükség esetén korlátozd tűzfallal a `HTTP_PORT` portot.
- A program egyszerre, egymás után kérdezi le a nyomtatókat. Sok eszköz vagy lassú hálózat esetén a teljes frissítési ciklus tovább tarthat a `POLL_SECONDS` értékénél.
- Ha egy nyomtató nem érhető el, az oldal `OFFLINE` állapotot és a lekérdezési hibát mutatja. A kellékanyag-szint hiányzó vagy nem értelmezhető adata „ismeretlenként” jelenhet meg.
