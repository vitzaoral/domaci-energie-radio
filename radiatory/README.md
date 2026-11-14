# Radiátory přes Zigbee (hlavice-cidlo)

[English version](README.en.md)

> [!WARNING]
> **Na vlastní nebezpečí.** Skript mění nastavení termostatických hlavic.
> Chyba v konfiguraci může nechat místnost přetopenou nebo vychladlou.
> Hlavice si při výpadku pokojového čidla sama přepne na vlastní čidlo,
> přesto si nastavte protimrazovou teplotu v hlavici a kontrolujte ji.
> Změny firmwaru a parametrů dělejte jen tam, kde smíte. V nájemním bytě se
> zeptejte majitele, výměnu hlavic na ventilu v domě s měřiči tepla
> konzultujte se správcem.

Termostatické hlavice **Sonoff TRVZB** (Zigbee 3.0) bez cloudu: USB
koordinátor **Sonoff ZBDongle-E**, **zigbee2mqtt** a **Mosquitto** na
Raspberry Pi. Hlavice jdou ovládat přes zigbee2mqtt (web, MQTT, Home Assistant).

Skript `hlavice_cidlo.py` k tomu přidává tři věci, které se v praxi osvědčily:

1. **Pokojové čidlo místo teploty u radiátoru.** Hlavice měří teplotu
   u žhavého radiátoru a zavírá moc brzy. Skript jí posílá teplotu
   z pokojového čidla (`external_temperature_input`) a přepne ji na
   `temperature_sensor_select = remote_temperature`. Hodí se jakékoli čidlo
   v zigbee2mqtt s polem `temperature` (například Aqara/Xiaomi). Když čidlo
   mlčí déle než 75 minut, vrátí hlavici na vlastní čidlo
   (`local_temperature`).
2. **Menší hystereze.** TRVZB z výroby zavře ventil na cíli a otevře ho až
   o 1 °C níž. Skript drží `temperature_accuracy` na -0,2 °C.
3. **Obejití chyby firmwaru 1.3.0.** Tato verze nastavenou hysterezi uloží,
   ale dál otevírá až pod cíl − 1 °C (zigbee2mqtt #28973). Když hlavice
   stojí a místnost je na prahu cíl − 0,2 °C nebo níž, skript na chvíli pošle
   teplotu cíl − 1,2 °C. Hlavice začne topit a skript hned vrátí skutečnou
   teplotu. Nejvýš jednou za 10 minut, vypíná se `wake_pulse = false`.

Navíc se hlavice zeptá na teplotu, když se 20 minut neozvala. Při stálé
teplotě se sama hlásí jen jednou za hodinu.

## Co bylo potřeba vědět

- **Hlavice bez pokojové teploty**: firmware 1.3.0 po asi 2 hodinách bez
  nové hodnoty sám přejde na vlastní čidlo (`remote_source_offline`).
  Při výpadku Pi nebo skriptu tedy hlavice dál reguluje.
- **Skutečnou polohu ventilu TRVZB nehlásí.** `valve_opening_degree` je jen
  strop z konfigurace. Topí/netopí ukazuje `running_state` (`heat`/`idle`).
- **Čidla Aqara/Xiaomi** posílají novou hodnotu při změně, nejpozději po
  60 minutách (typicky 30–40). Hranice čerstvosti 30 minut by hlavici
  pořád přepínala, proto 75.
- **Kanál Zigbee** volte co nejdál od Wi-Fi (Wi-Fi kanál 1 → Zigbee 25).
  Měnit ho jde jen před párováním.
- **Dongle dejte na USB prodlužovačku** (0,5–1 m) od Pi a dalších USB
  zařízení, jinak klesá dosah.
- **USB hub s vlastním napájením.** Hub napájený z Pi Zero 2 W (dongle
  a další USB zařízení) se pod zátěží odpojoval a shazoval Pi.
- **Na Pi Zero 2 W nekompilujte zigbee2mqtt.** Trvá to desítky minut
  a plná zátěž shodila napájení. `scripts/build-z2m-arm64.sh` sestaví balík
  v Dockeru na jiném počítači.

## Hardware

| Díl | Poznámka |
|---|---|
| Sonoff TRVZB | závit M30×1,5, adaptéry pro Danfoss v balení, 2× AA |
| Sonoff ZBDongle-**E** | EFR32MG21, v z2m adaptér `ember`. Verze P je jiný čip (`zstack`). |
| pokojové čidlo Zigbee | např. Aqara/Xiaomi s teplotou |
| Raspberry Pi | stačí Zero 2 W (s OTG hubem) |

## Rozjetí

1. **Mosquitto:**

   ```bash
   sudo apt install mosquitto mosquitto-clients python3-paho-mqtt
   sudo cp mosquitto.example.conf /etc/mosquitto/conf.d/local.conf
   sudo mosquitto_passwd -c /etc/mosquitto/passwd z2m
   sudo mosquitto_passwd /etc/mosquitto/passwd hlavice
   sudo systemctl restart mosquitto
   ```

2. **zigbee2mqtt** podle [oficiálního návodu](https://www.zigbee2mqtt.io/guide/installation/01_linux.html)
   (Node.js 22), případně z balíku z `scripts/build-z2m-arm64.sh`. Jako
   základ `configuration.yaml` použijte `configuration.example.yaml`
   (`last_seen: ISO_8601` je potřeba). Jednotka: `systemd/zigbee2mqtt.service`.
3. **Párování**: ve webu z2m zapnout Permit join. Na hlavici vložit baterie,
   nasadit na ventil a počkat na kalibraci. Pak kolečkem na `OF` a podržet
   horní tlačítko asi 3 s. Hlavici i čidlo přejmenujte (friendly name),
   pak Permit join vypněte.
4. **hlavice-cidlo:**

   ```bash
   sudo mkdir -p /opt/hlavice-cidlo && sudo cp hlavice_cidlo.py /opt/hlavice-cidlo/
   sudo cp config.example.ini /etc/hlavice-cidlo.conf
   sudo nano /etc/hlavice-cidlo.conf      # MQTT heslo, dvojice hlavice = čidlo
   sudo useradd -r -s /usr/sbin/nologin hlavice
   sudo chown root:hlavice /etc/hlavice-cidlo.conf && sudo chmod 640 /etc/hlavice-cidlo.conf
   sudo cp systemd/hlavice-cidlo.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now hlavice-cidlo
   journalctl -u hlavice-cidlo -f
   ```

Ovládání hlavice přímo přes MQTT:

```bash
mosquitto_pub -u z2m -P '<heslo>' -t 'zigbee2mqtt/Obyvak hlavice/set' -m '{"occupied_heating_setpoint": 20.5}'
```

## Zálohujte síť, nezveřejňujte ji

`/opt/zigbee2mqtt/data/` obsahuje `configuration.yaml` se **síťovým klíčem
Zigbee**, `database.db` a `coordinator_backup.json`. Bez zálohy se po výměně
donglu páruje všechno znovu. Tyto soubory nikdy nedávejte do gitu ani
na internet.

## Testy bez hardwaru

```bash
python3 -m unittest discover -s tests -v
```
