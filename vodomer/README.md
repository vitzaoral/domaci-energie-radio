# Vodoměr přes rádio (vodomer-radio)

[English version](README.en.md)

Odečet bytového vodoměru přes rádio. Vodoměry s rádiovým modulem
**Apator Metra E-RM 30** samy vysílají stav na 868,9 MHz kvůli dálkovému
odečtu. Levný USB přijímač RTL-SDR a Raspberry Pi to umí poslouchat také,
takže vidíte spotřebu po minutách a ne jednou za rok z vyúčtování.

Projekt je malý obal kolem [rtl_433](https://github.com/merbanan/rtl_433),
který zprávy dekóduje (dekodér 277). Skript `vodomer_radio.py` spustí rtl_433,
vybere rámce vašeho vodoměru a stav vypíše nebo pošle dál:

- na stdout jako text nebo JSON řádky,
- HTTP POST na libovolnou adresu (Home Assistant webhook, Node-RED, vlastní server),
- MQTT (vlastní minimální klient, bez dalších knihoven).

Stačí Python 3.9+ a jeho standardní knihovna.

## Hardware

- **RTL-SDR dongle** s čipem RTL2832U a tunerem R820T2, např. RTL-SDR Blog V3,
  zhruba 500–900 Kč. Verze V4 potřebuje novější ovladač od RTL-SDR Blog.
- **Anténa na 868 MHz.** Stačí přiložená teleskopická, zkrácená na čtvrtvlnu
  (asi 8,6 cm). Ve stejném bytě nebo domě signál obvykle stačí s rezervou.
- **Raspberry Pi** s Raspberry Pi OS (stačí i Pi Zero 2 W), nebo jakýkoli
  počítač s Linuxem.

Zkušenosti z provozu:

- Když RTL-SDR visí na USB hubu, dejte hubu vlastní napájení. Hub napájený
  z Pi se pod zátěží odpojoval.
- RTL-SDR dejte na USB prodlužovačku aspoň 0,5–1 m od USB hubu, jeho zdroje
  a Zigbee nebo jiných rádiových donglů. Jejich rušení zvedlo šum a slabší
  rámce se přestaly dekódovat.
- Automatický zisk (`gain = 0`) fungoval lépe než pevný.

## Jak to rozjet za 15 minut

Bez sestavení rtl_433. To na Pi Zero 2 W trvá asi půl hodiny.

1. **rtl_433 25.12 nebo novější.** Dekodér E-RM 30 je až ve verzi 25.12,
   balíček z `apt` bývá starší. Ověření:

   ```bash
   rtl_433 -R help 2>&1 | grep -i "apator metra"
   ```

   Když nic nevypíše, sestavte novou verzi:

   ```bash
   sudo bash scripts/build-rtl433.sh
   ```

2. **Ovladač DVB-T z jádra nesmí zabrat dongle:**

   ```bash
   sudo cp systemd/blacklist-rtlsdr.conf /etc/modprobe.d/
   sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
   ```

3. **Najděte svůj modul.** ID je natištěné na rádiovém modulu E-RM 30
   (ne na číselníku vodoměru). Spusťte na pár minut hledání:

   ```bash
   python3 vodomer_radio.py --list
   ```

   Uvidíte i moduly sousedů. Ukončete pomocí Ctrl+C, vypíše se souhrn.
   Svůj modul poznáte podle ID, nejsilnějšího signálu a stavu, který odpovídá
   číselníku.

4. **Konfigurace:**

   ```bash
   sudo mkdir -p /opt/vodomer-radio
   sudo cp vodomer_radio.py /opt/vodomer-radio/
   sudo cp config.example.ini /etc/vodomer-radio.conf
   sudo nano /etc/vodomer-radio.conf      # [meters] vaše ID, případně [http] nebo [mqtt]
   python3 /opt/vodomer-radio/vodomer_radio.py -c /etc/vodomer-radio.conf
   ```

5. **Služba systemd:**

   ```bash
   sudo useradd -r -s /usr/sbin/nologin -G plugdev vodomer
   sudo chown root:vodomer /etc/vodomer-radio.conf && sudo chmod 640 /etc/vodomer-radio.conf
   sudo cp systemd/vodomer-radio.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now vodomer-radio
   journalctl -u vodomer-radio -f
   ```

Zkouška bez hardwaru (rtl_433 stačí na libovolném počítači, dongle ne):

```bash
python3 -m unittest discover -s tests -v
python3 vodomer_radio.py --file tests/sample-rtl433.jsonl
```

## Výstup

Odečet jako JSON (stdout s `--json`, tělo HTTP POST, zpráva MQTT):

```json
{
  "meter_id": "123456789",
  "name": "studena",
  "volume_m3": 254.321,
  "volume_l": 254321,
  "meter_date": "2026-11-01",
  "time": "2026-11-01T08:00:00+0100",
  "rssi": -0.4,
  "snr": 22.1,
  "noise": -28.3
}
```

- `volume_m3` / `volume_l`: stav vodoměru. Modul posílá celé litry.
- `meter_date`: datum, které modul posílá v rámci (zřejmě datum posledního uložení stavu).
- `time`: čas přijetí. `rssi`, `snr` a `noise`: úroveň signálu podle rtl_433.

Výchozí `emit = changes` posílá jen změnu stavu a jednou za `heartbeat_minutes`
potvrzení, že příjem běží. `emit = all` pošle každý přijatý rámec.
Neodeslané HTTP požadavky čekají ve frontě v paměti (až 1 000) a odešlou se znovu.

### Jak často modul vysílá

Podle pozorování jednoho domu: 1. až 3. den v měsíci každou minutu, jinak
zhruba jednou za 4 minuty. Občas rámec chybí, to je normální. Spotřebu
počítejte z rozdílu stavů, ne ze součtu.

## Které vodoměry to umí a které ne

**Umí:** rádiový modul **Apator Metra E-RM 30**. Výrobce je Apator Metra
a v českých bytech se kvůli dálkovému odečtu vyskytuje často. Modul poznáte
podle štítku s označením E-RM 30. Z rámce je dekódované ID modulu, stav
v litrech a datum. Další údaje, které modul podle návodu posílá (zpětný tok,
datum manipulace magnetem a podobně), zatím nikdo nerozluštil.

**Neumí:**

- **Vodoměry a moduly s wireless M-Bus (wM-Bus, OMS).** Patří sem například
  moduly Apator s wM-Bus, Itron, Diehl/Sensus, Kamstrup, Bmeters, Techem
  a ista. Na ty je [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters)
  s rtl_wmbus a většinou potřebujete klíč (viz níže).
- **Vodoměry bez rádia.** Tam pomůže optické čtení nebo kamera, například
  projekt AI-on-the-edge-device.
- Jiné rádiové protokoly, než má rtl_433. Seznam vypíše `rtl_433 -R help`.
  Některé další vodoměry rtl_433 zná, tento skript ale filtruje jen E-RM 30.

## Šifrování a klíč

**E-RM 30 nepoužívá wM-Bus.** Má vlastní protokol se zakódovaným obsahem
a pevnou šifrou, kterou přispěvatelé rtl_433 zpětně rozluštili. Šifra je
pro všechny moduly stejná, **žádný klíč k vodoměru nepotřebujete** a nikde
o něj nežádáte.

**Vodoměry s wM-Bus** obvykle šifrují AES-128 (OMS, režim 5). Každý vodoměr
má vlastní klíč, který zná dodavatel vody, správce domu nebo firma, která
odečty dělá. Bez klíče vidíte jen hlavičku (výrobce, ID, typ), stav ne.
O klíč se dá požádat, vyhovět ale nemusí. S klíčem pak použijte wmbusmeters.

## Soukromí a sousedé

Přijímač zachytí i vodoměry sousedů. Do `[meters]` dejte jen svoje ID:
skript pak cizí rámce zahodí a nikam je neposílá. Stav cizího vodoměru
neukládejte ani nezveřejňujte. Z průběhu spotřeby se dá poznat, kdy je
někdo doma.

## Licence

MIT, viz [LICENSE](../LICENSE). rtl_433 má vlastní licenci GPL-2.0. Tento
projekt ho jen spouští jako samostatný program a nic z něj neobsahuje.
Testovací rámce v `tests/frames.txt` jsou syntetické a vymyšlené.
