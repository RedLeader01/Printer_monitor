# Nyomtatófigyelő

Egyszerű, SNMP-alapú nyomtatófigyelő Pythonból. A program rendszeres időközönként lekérdezi a megadott nyomtatók kellékanyag-szintjét, állapotát, hibáit és egyéb adatait, majd egy böngészőből elérhető oldalon jeleníti meg. Új probléma esetén opcionálisan Telegram-értesítést küld.

**Semmit nem kell telepíteni:** csak Python 3.8 vagy újabb kell hozzá. A saját SNMP-klienst tartalmazza, ezért nincs szükség külső Python-csomagra, `snmpwalk` parancsra vagy WSL-re. Windowson és Linuxon is fut.

## Mappaszerkezet

```text
printer-monitor/
  run.py                 ← innen indítod a programot  config.example.ini     ← példa beállítófájl (ebből lesz a config.ini)
  config.ini             ← a te beállításaid (nem kerül verziókezelésbe)
  printer_monitor/       ← a Python kód
    snmp.py              SNMP-kliens (walk)
    printers.py          egy nyomtató lekérdezése, Részletek adatai
    monitor.py           lekérdező ciklus, új problémák észlelése
    history.py           előzmények mentése SQLite-ba (hibák, elérhetetlenség, szintek)
    notify.py            értesítések (most: Telegram)
    config.py            a config.ini beolvasása és ellenőrzése
    web.py               kis webszerver: /api, /api/history és a web/ mappa fájljai
    store.py             a nyomtatók és az admin fiókok adatbázisa (printers.db)
    auth.py              admin belépés, munkamenetek, védelem
    cli.py               parancssor (indítás, test, admin, printers)
  web/                   ← a weboldal (a böngészőben fut)
    index.html           a nyomtatók áttekintője
    history.html         az Előzmények oldal
    style.css
    app.js               kirajzolja a kártyákat a /api adataiból
    history.js           kirajzolja az előzményeket az /api/history adataiból
  tests/                 ← automatikus tesztek hamis nyomtatóval
  history.db             ← az előzmények adatbázisa (a program hozza létre, nem kerül verziókezelésbe)
  printers.db            ← a felvett nyomtatók és az admin fiókok (a program hozza létre, nem kerül verziókezelésbe)
```

A Python csak adatot ad (`/api`), a kinézetet a `web/` mappa adja. A `style.css`-t és az `app.js`-t újraindítás nélkül is átírhatod, elég frissíteni a böngészőt.

## Követelmények

- Python 3.8 vagy újabb (ellenőrzés: `python --version`; Windowson ha a `python` nem működik, próbáld a `py` parancsot)
- SNMP v2c engedélyezve a nyomtatókon, a beállított közösségnévvel (alapértelmezés: `public`)
- A program futtató gépről elérhető a nyomtatók UDP 161-es portja

## Első indítás és beállítás

1. Másold le a `config.example.ini` fájlt `config.ini` néven, vagy egyszerűen indítsd el a programot: ha nincs `config.ini`, a program létrehozza a példából, és szól, hogy nézd át.
2. Nyisd meg a `config.ini` fájlt (Jegyzettömb is jó, mentsd UTF-8 kódolással), és szükség esetén írd át a közösségnevet.
3. Indítsd el:

```powershell
python run.py
```

Linuxon vagy macOS-en `python3 run.py`. Ezután nyisd meg a böngészőben: `http://localhost:8080`. A leállítás `Ctrl+C`.

Az első indításkor **nincs nyomtató felvéve**: az admin tölti fel őket (lásd lent, "Nyomtatók kezelése").

### A config.ini beállításai

| Szakasz | Beállítás | Jelentés |
| --- | --- | --- |
| `[general]` | `community` | SNMP v2c közösségnév, egyeznie kell a nyomtatókon beállítottal (alapérték: `public`) |
| `[general]` | `poll_seconds` | Milyen gyakran kérdezze le a nyomtatókat, másodpercben (alapérték: 30, legalább 5) |
| `[general]` | `warn_pct` | Sárga jelzés és "Hamarosan elfogy" lista, ha a fogyóanyag ennyi % alá esik (alapérték: 20) |
| `[general]` | `crit_pct` | Piros jelzés ennyi % alatt (alapérték: 10) |
| `[general]` | `snmp_port` | A nyomtatók SNMP-portja, ritkán kell (alapérték: 161) |
| `[general]` | `ignore_alerts` | Vesszővel elválasztott szövegrészletek: az ezeket tartalmazó nyomtatói figyelmeztetéseket a program kihagyja (nem látszik a kártyán, nem kerül az előzményekbe, és nem megy róla értesítés). Alapérték: `016-425` (a Xerox "szendergő módban van" üzenete). Üresen hagyva minden figyelmeztetés megjelenik |
| `[web]` | `host` | `127.0.0.1`: csak erről a gépről érhető el az oldal. `0.0.0.0`: a hálózat többi gépéről is |
| `[web]` | `port` | A weboldal portja (alapérték: 8080) |
| `[history]` | `enabled` | Az előzmények mentése be (`yes`) vagy ki (`no`) (alapérték: `yes`) |
| `[history]` | `keep_days` | Ennyi napig őrzi meg az adatokat, utána törli (alapérték: 365) |
| `[history]` | `file` | Az adatbázisfájl neve a program mappájában (alapérték: `history.db`) |
| `[storage]` | `printers_file` | A nyomtatók és az admin fiókok adatbázisa (alapérték: `printers.db`) |
| `[admin]` | `trust_localhost` | `yes`: a futtató gépről (localhost) belépés nélkül lehet kezelni a nyomtatókat. `no`: mindenhonnan admin fiók kell (alapérték: `yes`) |
| `[admin]` | `session_hours` | Ennyi óráig marad érvényes egy belépés (1-720, alapérték: 8) |
| `[telegram]` | `token`, `chat_id` | Opcionális, lásd lent |

A `;` és `#` jellel kezdődő sorok megjegyzések. Hibás érték esetén a program érthető hibaüzenettel áll le.

## Nyomtatók kezelése

A nyomtatók nem a `config.ini`-ben, hanem a `printers.db` adatbázisban vannak, és futás közben, újraindítás nélkül kezelhetők. Megnézni mindenki megnézheti őket; felvenni és törölni csak admin tud.

**Weboldalon** (az oldal tetején az admin sáv):

- **+ Nyomtató hozzáadása**: név és IP-cím (vagy gépnév). A felvétel után azonnal lekérdezi a nyomtatót.
- **+ Több egyszerre**: soronként egy nyomtató, `Név = IP-cím` formában (`;` vagy tabulátor is jó, így Excelből is bemásolható). A hibás vagy már létező sorokat kihagyja, a többit felveszi, és megmutatja, melyik sor miért maradt ki; csak ezek maradnak a mezőben.
- **Törlés** a kártyán: megerősítés után törli a nyomtatót. A régi eseményei megmaradnak az Előzményekben, de csak az éppen kiválasztott időszakon belül látszanak (két hete törölt nyomtató a 7 napos nézetben már nincs ott, a 30 naposban még igen; ilyenkor "(törölve)" jelzést kap). Ugyanazzal a névvel újra felvehető.

**Parancssorból** (például GUI nélküli Linux szerveren, a program közben is futhat):

```bash
python3 run.py printers list
python3 run.py printers add Iroda-1 192.168.1.11
python3 run.py printers remove Iroda-1
python3 run.py printers import lista.txt      # soronként: Név = IP-cím
```

**Ki kezelhet?** Két mód együtt működik:

1. **A futtató gépről (localhost)** nem kell belépni. Ez csak akkor érvényes, ha a böngésző a `localhost`, `127.0.0.1` vagy `[::1]` címen nyitja meg az oldalt, és a kérés nem proxyn keresztül érkezik.
2. **Más gépekről** admin fiókkal kell belépni (az oldal tetején "Belépés (admin)"). A belépés `session_hours` óráig érvényes, tehát nem kell minden felvételnél jelszót írni. Az admin fiókot a futtató gépen, parancssorból lehet létrehozni, így GUI nélkül is megy:

```bash
python3 run.py admin set NÉV      # létrehozás vagy jelszócsere (kétszer kéri a jelszót, nem látszik gépeléskor)
python3 run.py admin list
python3 run.py admin remove NÉV
```

A jelszó legalább 8 karakter. A jelszócsere vagy a fiók törlése minden belépését érvényteleníti. Öt hibás belépés után az adott gépről 10 percig nem lehet próbálkozni.

Ha a régi, `config.ini`-ben lévő `[printers]` listát át akarod venni: `python3 run.py import-printers`, utána a szakaszt törölheted.

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
http://localhost:8080/api/history?printer=NÉV&days=30
```

## Előzmények

A program minden problémát elment egy helyi SQLite fájlba (`history.db`), így visszanézhető, **mikor** volt a gond és **mennyi ideig tartott**. Az oldal tetején az **Előzmények →** linkre kattintva (vagy a `http://localhost:8080/history.html` címen) nyílik meg.

Egy sor egy **esemény**, kezdettel, véggel és időtartammal:

| Esemény | Mikor kerül be |
| --- | --- |
| Nem elérhető | A nyomtató nem válaszol az SNMP-kérésekre |
| Hiba / figyelmeztetés | A nyomtató riasztási táblájában kritikus vagy figyelmeztető üzenet jelenik meg (pl. nyomtatófej-hiba) |
| Kevés kellékanyag | Egy kellékanyag a `warn_pct` érték alá esik |
| A figyelő nem futott | A program hosszabb ideig (pár percnél tovább) nem futott, például leállították vagy a gép ki volt kapcsolva |

- Amíg egy probléma fennáll, a **Vége** oszlopban "tart még" áll, és a sor sárga. Amint megszűnik, a sor lezárul, és megjelenik az időtartam.
- Az oldalon szűrhetsz nyomtatóra, időszakra (24 óra, 7, 30, 90 nap, 1 év) és eseménytípusra (például csak a "Nem elérhető" események). A címsorba írt `?printer=NÉV&days=30&kind=offline` előre beállítja a szűrőket (`kind` értékei: `offline`, `alert`, `supply_low`, `gap`).
- Ha a kellékanyag szintje közben csökken (15% → 14%), az ugyanaz az esemény marad, csak a százalék frissül. Nem lesz új sor, és nem megy új értesítés sem.
- Ha a nyomtató átmenetileg elérhetetlenné válik, a közben fennálló hibák és az alacsony szint nyitva maradnak, mert nem tudható, hogy megszűntek-e. Az elérhetőség visszatértekor folytatódnak.
- A kellékanyagok szintjét a program **csak változáskor** menti el, ezért a fájl kicsi marad (becslés szerint hét nyomtatónál évente legfeljebb pár MB). Ezek az adatok később előrejelzéshez és csere-észleléshez használhatók (például egy hirtelen ugrás 5%-ról 100%-ra tonercserét jelent). Az Előzmények oldal jelenleg az eseményeket mutatja, a szinteket nem.
- A `keep_days` napnál régebbi lezárt események és szintek automatikusan törlődnek (naponta egyszer). A még tartó eseményeket a program nem törli.
- Ha a program a `history.db` fájlt nem tudja megnyitni (például nincs írási jog a mappában), a figyelés akkor is fut, csak az előzmények nem mentődnek, és erről az indításkor üzenet jelenik meg.
- A fájl bármilyen SQLite-megjelenítővel (például a [DB Browser for SQLite](https://sqlitebrowser.org/)) megnyitható, a táblák: `episodes`, `readings`.

Kikapcsolás: a `config.ini`-ben `[history]` alatt `enabled = no`.

## Telegram-értesítések

Opcionálisan Telegram-üzenet küldhető, amikor egy nyomtatónál új probléma jelenik meg (kevés kellékanyag, hiba, vagy a nyomtató elérhetetlenné válik). Ehhez hozz létre egy Telegram-botot, majd add meg a bot tokent és a célcsevegés azonosítóját a `config.ini` `[telegram]` szakaszában:

```ini
[telegram]
token = 123456:ABC...
chat_id = -100123456
```

Ugyanezek környezeti változóként is megadhatók (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`), és azok felülírják a `config.ini` értékét. Ha egyik sincs beállítva, az értesítések csak a program konzoljára kerülnek.

A program csak az **újonnan** észlelt problémákról küld üzenetet, nem ismétli azokat minden lekérdezéskor. Ha egy probléma megszűnik, majd később újra megjelenik, az új értesítést eredményez. Az alábbiak nem számítanak új problémának:

- ha egy kellékanyag szintje tovább csökken (15% → 14%), mert az már ugyanaz a "kevés kellékanyag" probléma;
- ha egy nyomtató átmenetileg elérhetetlenné válik, majd visszajön: az OFFLINE állapotról kap értesítést, de a közben is fennálló régebbi hibákról nem küld újat.

A program újraindításakor a már fennálló problémákról újra kapsz egy értesítést, mert a program az emlékezetét nem őrzi meg.

## Tesztek

A projekt mappájából:

```powershell
python -m unittest -v
```

A tesztek egy hamis (helyi) SNMP-nyomtatót indítanak, és ellenőrzik a lekérdezést, a nyomtatók felvételét és törlését (webről és parancssorból), az admin belépést (jelszókivonat, lejárat, hibás próbálkozások korlátozása, localhost-szabály, CSRF/eredet-ellenőrzés), a beállítások beolvasását, az értesítések logikáját, az előzmények mentését (események kezdete és vége, újraindítás, szünet-észlelés, takarítás, párhuzamos írás) és a webszervert is (többek között azt, hogy a `web/` mappán kívüli fájlok nem érhetők el). Az előzményeknél ideiglenes adatbázist használnak, a valódi `history.db` fájlodhoz nem nyúlnak. Nincs szükségük valódi nyomtatóra vagy hálózatra, és nem módosítanak semmit.

## Frissítés

A program frissítéséhez cseréld le a mappa tartalmát az újra, **kivéve a `config.ini`, a `history.db` és a `printers.db` fájlt**. Az első a te beállításaidat, a második az összegyűjtött előzményeket, a harmadik a nyomtatókat és az admin fiókokat tartalmazza. Így nem kell újra beírni semmit, és az előzmények sem vesznek el. Frissítés előtt állítsd le a programot.

## Futtatás Linux szerveren

Ez akkor hasznos, ha a figyelőnek folyamatosan, a háttérben kell futnia. Debian vagy Ubuntu szerveren elég a Python 3:

```bash
sudo apt update
sudo apt install python3
```

Helyezd a mappát például az `/opt/printer-monitor` könyvtárba, hozd létre benne a `config.ini` fájlt, és ha más gépekről is el kell érni az oldalt, állítsd a `host` értékét `0.0.0.0`-ra. Mivel a szerveren nincs böngésző, a nyomtatókat és az admin fiókot parancssorból veheted fel (a program futása közben is):

```bash
python3 run.py admin set NÉV                   # admin fiók a távoli (böngészős) kezeléshez
python3 run.py printers import lista.txt       # a nyomtatólista soronként: Név = IP-cím
```

Kézi próba:

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

- **A `config.ini` és a `history.db` ne kerüljön nyilvános tárolóba.** A `config.ini` a belső IP-címeket és a Telegram-tokent, a `history.db` a nyomtatók nevét, hibaüzeneteit és az eseményeik idejét tartalmazza. A mellékelt `.gitignore` mindkettőt kizárja a Git használatakor; csak a `config.example.ini` kerül be.
- Az SNMP v2c nem titkosítja a forgalmat, a közösségnevet szövegesen küldi. Csak megbízható belső hálózaton használd, és ahol lehet, ne hagyd az alapértelmezett `public` értéket.
- A webkiszolgáló alapból csak a helyi gépről érhető el (`127.0.0.1`). Ha a `host` értékét `0.0.0.0`-ra állítod, az oldalt a hálózaton bárki megnézheti (nyomtatók állapota, előzmények), a nyomtatólistát viszont csak a helyi gépről vagy admin fiókkal lehet módosítani. A webszerver nyomtatót nem tud vezérelni.
- **Nincs HTTPS.** Az admin jelszó a belépéskor titkosítatlanul megy át a hálózaton, ezért csak megbízható belső hálózaton használd, és ne használd máshol is ugyanazt a jelszót. A jelszót a program csak sózott PBKDF2 kivonatként tárolja a `printers.db`-ben.
- **Fordított proxy (nginx, Apache) ugyanazon a gépen:** a proxy a program felé helyi címről érkezik, ezért a "localhost belépés nélkül" szabály a távoli felhasználókra is érvényes lehet. Ha proxy mögé teszed, állítsd be: `[admin] trust_localhost = no`.
- **A `printers.db` ne kerüljön nyilvános tárolóba.** Az admin jelszókivonatokat és a nyomtatók címeit tartalmazza. A `.gitignore`-ban szerepeljen a `printers.db` és a `printers.db-journal` is.
- A nyomtatókat a program párhuzamosan kérdezi le (egyszerre legfeljebb 8-at), így egy elérhetetlen nyomtató nem lassítja a többit. Az offline nyomtató lekérdezése kb. 4 másodpercig vár az időtúllépésre.
- A 30 másodperces lekérdezés a hálózatot és a nyomtatókat gyakorlatilag nem terheli (nyomtatónként kb. 50-60 apró UDP csomag).
- Hibaként a program a nyomtató riasztási tábláját (kritikus és figyelmeztető szint) jeleníti meg. Az információs szintű üzeneteket kihagyja. Ha egy nyomtató egy hibát (például nyomtatófej-hibát) nem ebben a táblában jelez, a `test` parancs kimenetéből derül ki, hol jelenik meg.
- Ha egy nyomtató nem érhető el, az oldal `OFFLINE` állapotot és a lekérdezési hibát mutatja. A hiányzó vagy nem értelmezhető kellékanyag-szint "ismeretlen" vagy "van még" jelzéssel látszik.
- A kellékanyagok nevét a nyomtató adja meg; a cikkszám és a sorozatszám részt a program leválasztja a névről, a cikkszámot külön mutatja.
