# Radiators over Zigbee (hlavice-cidlo)

[Česká verze](README.md)

> [!WARNING]
> **At your own risk.** The script changes thermostatic valve settings.
> A configuration mistake can overheat or chill a room. When the room sensor
> fails, the valve falls back to its own sensor. Still, set a frost
> protection temperature on the valve and keep an eye on it. Only change
> firmware and parameters where you are allowed to. In a rented flat ask
> the owner. In buildings with heat cost allocators, check with the
> building manager before replacing valve heads.

**Sonoff TRVZB** (Zigbee 3.0) thermostatic radiator valves without a cloud:
a **Sonoff ZBDongle-E** USB coordinator, **zigbee2mqtt** and **Mosquitto**
on a Raspberry Pi. You control the valves through zigbee2mqtt (web UI, MQTT,
Home Assistant).

`hlavice_cidlo.py` adds three things that proved useful:

1. **Room sensor instead of radiator temperature.** The valve measures next
   to the hot radiator and closes too early. The script feeds it the room
   sensor temperature (`external_temperature_input`) and sets
   `temperature_sensor_select = remote_temperature`. Any zigbee2mqtt device
   with a `temperature` field works (e.g. Aqara/Xiaomi). If the sensor is
   silent for more than 75 minutes, the valve is switched back to its own
   sensor (`local_temperature`).
2. **Smaller hysteresis.** Out of the box TRVZB closes at the target and
   reopens 1 °C below it. The script keeps `temperature_accuracy` at -0.2 °C.
3. **Workaround for firmware 1.3.0.** This version stores the hysteresis
   but still opens only below target − 1 °C (zigbee2mqtt #28973). When the
   valve is idle and the room is at target − 0.2 °C or below, the script
   briefly sends target − 1.2 °C. The valve starts heating and the real
   temperature is restored right away. At most once per 10 minutes,
   disable with `wake_pulse = false`.

It also asks a valve for its temperature after 20 minutes of silence. At a
steady temperature the valve reports on its own only once an hour.

## Things worth knowing

- **Without room temperature**, firmware 1.3.0 switches back to its own
  sensor after about 2 hours (`remote_source_offline`). If the Pi or the
  script dies, the valve keeps regulating.
- **TRVZB does not report the real valve position.** `valve_opening_degree`
  is only the configured limit. Heating/idle is `running_state`
  (`heat`/`idle`).
- **Aqara/Xiaomi sensors** report on change, at the latest after 60 minutes
  (typically 30–40). A 30-minute freshness limit would flip the valve all
  the time, hence 75.
- **Pick the Zigbee channel** as far from your Wi-Fi as possible (Wi-Fi 1 →
  Zigbee 25). It can only be changed before pairing.
- **Put the dongle on a USB extension** (0.5–1 m) away from the Pi and other
  USB devices, otherwise range drops.
- **Use a powered USB hub.** A hub powered from a Pi Zero 2 W (dongle plus
  other USB devices) kept disconnecting under load and crashed the Pi.
- **Do not compile zigbee2mqtt on a Pi Zero 2 W.** It takes tens of minutes
  and the full load brought the power supply down.
  `scripts/build-z2m-arm64.sh` builds a package in Docker on another machine.

## Hardware

| Part | Note |
|---|---|
| Sonoff TRVZB | M30×1.5 thread, Danfoss adapters included, 2× AA |
| Sonoff ZBDongle-**E** | EFR32MG21, z2m adapter `ember`. The P version is a different chip (`zstack`). |
| Zigbee room sensor | e.g. Aqara/Xiaomi with temperature |
| Raspberry Pi | a Zero 2 W is enough (with an OTG hub) |

## Getting started

1. **Mosquitto:**

   ```bash
   sudo apt install mosquitto mosquitto-clients python3-paho-mqtt
   sudo cp mosquitto.example.conf /etc/mosquitto/conf.d/local.conf
   sudo mosquitto_passwd -c /etc/mosquitto/passwd z2m
   sudo mosquitto_passwd /etc/mosquitto/passwd hlavice
   sudo systemctl restart mosquitto
   ```

2. **zigbee2mqtt** per the [official guide](https://www.zigbee2mqtt.io/guide/installation/01_linux.html)
   (Node.js 22), or from the package built by `scripts/build-z2m-arm64.sh`.
   Start `configuration.yaml` from `configuration.example.yaml`
   (`last_seen: ISO_8601` is required). Unit: `systemd/zigbee2mqtt.service`.
3. **Pairing**: enable Permit join in the z2m web UI. Insert batteries into
   the valve, mount it and wait for calibration. Then turn the wheel to
   `OF` and hold the top button for about 3 s. Rename the valve and the
   sensor (friendly name), then disable Permit join.
4. **hlavice-cidlo:**

   ```bash
   sudo mkdir -p /opt/hlavice-cidlo && sudo cp hlavice_cidlo.py /opt/hlavice-cidlo/
   sudo cp config.example.ini /etc/hlavice-cidlo.conf
   sudo nano /etc/hlavice-cidlo.conf      # MQTT password, valve = sensor pairs
   sudo useradd -r -s /usr/sbin/nologin hlavice
   sudo chown root:hlavice /etc/hlavice-cidlo.conf && sudo chmod 640 /etc/hlavice-cidlo.conf
   sudo cp systemd/hlavice-cidlo.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now hlavice-cidlo
   journalctl -u hlavice-cidlo -f
   ```

Control a valve directly over MQTT:

```bash
mosquitto_pub -u z2m -P '<password>' -t 'zigbee2mqtt/Obyvak hlavice/set' -m '{"occupied_heating_setpoint": 20.5}'
```

## Back up your network, never publish it

`/opt/zigbee2mqtt/data/` holds `configuration.yaml` with the **Zigbee
network key**, `database.db` and `coordinator_backup.json`. Without a backup
you re-pair everything after replacing the dongle. Never put these files in
git or online.

## Tests without hardware

```bash
python3 -m unittest discover -s tests -v
```
