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

## Futtatás Windows alatt WSL-lel

Telepíts egy Ubuntu WSL-disztribúciót Windows PowerShellből, ha még nincs telepítve:

```powershell
wsl --install -d Ubuntu
```

Indítsd el az Ubuntut, majd a WSL-terminálban telepítsd a szükséges csomagokat:

```bash
sudo apt update
sudo apt install python3 snmp
```

Tedd a `printer_monitor.py` fájlt a WSL saját fájlrendszerébe, például a `~/printer-monitor` könyvtárba, állítsd be a nyomtatókat a [Beállítás](#beállítás) rész szerint, majd indítsd el:

```bash
cd ~/printer-monitor
python3 printer_monitor.py
```

Windows böngészőből általában a `http://localhost:8080` címen érhető el a felület, amíg a program fut. Ha nem nyílik meg, ellenőrizd, hogy a WSL-ben fut-e a program, a Windows tűzfal nem blokkolja-e a kapcsolatot, és az adott WSL hálózati mód támogatja-e a localhost porttovábbítást. A WSL-nek a nyomtatók IP-címeit is el kell érnie; ezt a WSL-terminálból például az `snmpwalk` paranccsal ellenőrizheted.

## Futtatás Linux szerveren

Debian vagy Ubuntu szerveren telepítsd a Python 3-at és az `snmpwalk` parancsot:

```bash
sudo apt update
sudo apt install python3 snmp
```

Helyezd a programot például az `/opt/printer-monitor` könyvtárba, és módosítsd benne a nyomtatók adatait. Indítás előtti hálózati próbaként futtasd le az alábbi parancsot; a címet és a közösségnevet igazítsd a saját nyomtatódhoz:

```bash
snmpwalk -v2c -c public -t 2 -r 1 -Oqn 192.168.1.101 1.3.6.1.2.1.43.11.1.1.6
```

Kézi indítás és teszt:

```bash
cd /opt/printer-monitor
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

A felület a szerver IP-címén, például a `http://192.168.1.10:8080` címen érhető el. A tűzfalon csak a megbízható belső hálózat felől engedélyezd a `8080`-as portot; a webkiszolgáló nem nyújt hitelesítést vagy titkosított kapcsolatot.

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
