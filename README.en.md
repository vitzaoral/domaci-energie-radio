# domaci-energie-radio

[Česká verze](README.md) · Repository: https://github.com/vitzaoral/domaci-energie-radio

Water meter, gas boiler and radiators over radio, no cloud, on a single
Raspberry Pi. Three independent parts, each usable on its own:

| Part | What it does | Radio | Hardware |
|---|---|---|---|
| [`vodomer/`](vodomer/) | reads water meters with the **Apator Metra E-RM 30** module | receive 868.9 MHz | RTL-SDR dongle |
| [`kotel/`](kotel/) | reads and controls boilers with the **Siemens QAA78** wireless room unit / **AVS71** receiver | receive and transmit 868.3 MHz | CC1101 module on SPI |
| [`radiatory/`](radiatory/) | drives **Sonoff TRVZB** valves from a room sensor via zigbee2mqtt | Zigbee 2.4 GHz | Sonoff ZBDongle-E |

All parts hand data over in simple ways: stdout, HTTP or MQTT. They plug
into Home Assistant, Node-RED or your own system. Python only. The water
meter and boiler parts need just the standard library (the boiler also
needs `spidev` and GPIO), the radiator part needs `paho-mqtt`.

> [!CAUTION]
> **Boiler and valves: safety and warranty.** `kotel/` transmits commands
> to the boiler using a reverse-engineered protocol the manufacturer does
> not support. `radiatory/` changes valve settings. A mistake can switch
> the heating off, let the home cool down, overheat a room or set scalding
> hot water. It may affect the boiler's **warranty and service contract**.
> Transmissions must follow your national 868 MHz SRD rules. Use at your own
> risk, no liability for any damage. Always start read-only. The water meter
> part only receives and never transmits.

## Requirements

- A Raspberry Pi (everything runs on a Pi Zero 2 W) with Raspberry Pi OS.
- On a Pi Zero with a single USB port, a **powered OTG hub**. An RTL-SDR
  and a Zigbee dongle on a hub powered from the Pi crashed the Pi.
- Put the RTL-SDR and the Zigbee dongle on USB extension cables, apart from
  each other and from the hub's power supply. Interference degraded water
  meter reception.

## Quick check without hardware

```bash
git clone https://github.com/vitzaoral/domaci-energie-radio.git && cd domaci-energie-radio
for d in vodomer kotel radiatory; do (cd $d && python3 -m unittest discover -s tests); done
```

The water meter test also runs the real rtl_433 decoder (25.12 or newer, if
installed) on a made-up radio frame.

## Privacy

- The water meter receiver also hears your neighbours' meters. Filter on
  your own IDs and do not store other people's data.
- Water, heating and temperature profiles reveal when someone is at home.
  The gateways therefore listen locally (`127.0.0.1`) with a token or
  password, and nothing should be reachable from the internet.
- Never publish your Zigbee network key (z2m `configuration.yaml`), tokens
  or your meter IDs.

## License

MIT, see [LICENSE](LICENSE). The project runs rtl_433 (GPL-2.0) and
zigbee2mqtt (GPL-3.0) as separate programs and contains none of their code.
The boiler protocol description builds on the public BSB-LAN project and
the author's own measurements.
