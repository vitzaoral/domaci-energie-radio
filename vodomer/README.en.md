# Water meter over radio (vodomer-radio)

[Česká verze](README.md)

Read your apartment water meter over the air. Water meters fitted with the
**Apator Metra E-RM 30** radio module broadcast their reading on 868.9 MHz
for remote meter reading. A cheap RTL-SDR USB dongle and a Raspberry Pi can
listen too, so you see consumption minute by minute instead of once a year
on the bill.

The project is a small wrapper around [rtl_433](https://github.com/merbanan/rtl_433),
which decodes the messages (decoder 277). `vodomer_radio.py` starts rtl_433,
picks the frames of your meter and prints or forwards the reading:

- to stdout as text or JSON lines,
- as an HTTP POST to any URL (Home Assistant webhook, Node-RED, your own server),
- to MQTT (tiny built-in client, no extra libraries).

Python 3.9+ standard library only.

## Hardware

- **RTL-SDR dongle** with RTL2832U and R820T2, e.g. RTL-SDR Blog V3
  (about 20–35 EUR). The V4 needs the newer RTL-SDR Blog driver.
- **868 MHz antenna.** The bundled telescopic antenna shortened to a quarter
  wave (about 8.6 cm) is enough. Inside the same building the signal is
  usually plenty.
- **Raspberry Pi** with Raspberry Pi OS (a Pi Zero 2 W is enough), or any
  Linux machine.

Lessons learned:

- If the RTL-SDR sits on a USB hub, give the hub its own power supply.
  A hub powered from the Pi kept disconnecting under load.
- Put the RTL-SDR on a USB extension cable at least 0.5–1 m away from the
  USB hub, its power supply and any Zigbee or other radio dongles. Their
  interference raised the noise floor and weaker frames stopped decoding.
- Automatic gain (`gain = 0`) worked better than a fixed gain.

## Up and running in 15 minutes

Not counting an rtl_433 build, which takes about 30 minutes on a Pi Zero 2 W.

1. **rtl_433 25.12 or newer.** The E-RM 30 decoder first shipped in 25.12,
   and distro packages are usually older. Check:

   ```bash
   rtl_433 -R help 2>&1 | grep -i "apator metra"
   ```

   If nothing is printed, build it:

   ```bash
   sudo bash scripts/build-rtl433.sh
   ```

2. **Keep the kernel DVB-T driver off the dongle:**

   ```bash
   sudo cp systemd/blacklist-rtlsdr.conf /etc/modprobe.d/
   sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
   ```

3. **Find your module.** The ID is printed on the E-RM 30 radio module,
   not on the meter dial. Run discovery for a few minutes:

   ```bash
   python3 vodomer_radio.py --list
   ```

   You will see your neighbours' modules too. Stop with Ctrl+C to get a
   summary. Your module is the one with your ID, usually the strongest
   signal and a reading that matches the dial.

4. **Configure:**

   ```bash
   sudo mkdir -p /opt/vodomer-radio
   sudo cp vodomer_radio.py /opt/vodomer-radio/
   sudo cp config.example.ini /etc/vodomer-radio.conf
   sudo nano /etc/vodomer-radio.conf      # your ID in [meters], optionally [http] or [mqtt]
   python3 /opt/vodomer-radio/vodomer_radio.py -c /etc/vodomer-radio.conf
   ```

5. **systemd service:**

   ```bash
   sudo useradd -r -s /usr/sbin/nologin -G plugdev vodomer
   sudo chown root:vodomer /etc/vodomer-radio.conf && sudo chmod 640 /etc/vodomer-radio.conf
   sudo cp systemd/vodomer-radio.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now vodomer-radio
   journalctl -u vodomer-radio -f
   ```

Test without hardware (rtl_433 must be installed, the dongle is not needed):

```bash
python3 -m unittest discover -s tests -v
python3 vodomer_radio.py --file tests/sample-rtl433.jsonl
```

## Output

A reading as JSON (stdout with `--json`, HTTP POST body, MQTT payload):

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

- `volume_m3` / `volume_l`: meter reading. The module reports whole litres.
- `meter_date`: date sent inside the frame (probably the date the reading was last stored).
- `time`: time of reception. `rssi`, `snr` and `noise`: signal level from rtl_433.

The default `emit = changes` sends only changes, plus a heartbeat every
`heartbeat_minutes` to show reception is alive. `emit = all` sends every
received frame. Failed HTTP posts are queued in memory (up to 1,000) and retried.

### Transmission schedule

Observed in one building: every minute on days 1–3 of the month, otherwise
roughly every 4 minutes. Missing frames are normal. Compute consumption from
differences between readings, not by summing.

## Supported meters

**Supported:** the **Apator Metra E-RM 30** radio module, common in Czech
apartments for remote reading. Look for the E-RM 30 label on the module.
Decoded fields are the module ID, the reading in litres and a date. Other
fields the module sends according to its manual (reverse flow, magnetic
tamper date, etc.) have not been reverse-engineered yet.

**Not supported:**

- **wireless M-Bus (wM-Bus, OMS) meters and modules**, for example Apator
  wM-Bus modules, Itron, Diehl/Sensus, Kamstrup, Bmeters, Techem and ista.
  Use [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters) with
  rtl_wmbus. You usually need a key (see below).
- **Meters without radio.** Try optical or camera reading, e.g. the
  AI-on-the-edge-device project.
- Radio protocols rtl_433 does not know (`rtl_433 -R help` lists them).
  rtl_433 handles some other water meters too, but this script only
  filters E-RM 30.

## Encryption and keys

**The E-RM 30 does not use wM-Bus.** It uses its own protocol with an
obfuscated payload and a fixed cipher that rtl_433 contributors have
reverse-engineered. The cipher is the same for every module, so **you need
no per-meter key** and have nothing to request.

**wM-Bus meters** usually encrypt with AES-128 (OMS, mode 5) and a per-meter
key held by the water utility, the building manager or the meter reading
company. Without the key you only see the header (manufacturer, ID, type),
not the reading. You can ask for the key, but they may refuse. With the key,
use wmbusmeters.

## Privacy and neighbours

The receiver also picks up your neighbours' meters. Put only your own IDs
into `[meters]`. The script then drops other frames and forwards nothing.
Do not store or publish other people's readings. A consumption profile
shows when someone is at home.

## License

MIT, see [LICENSE](../LICENSE). rtl_433 is GPL-2.0 licensed. This project only
runs it as a separate program and contains none of its code. The test frames
in `tests/frames.txt` are synthetic and made up.
