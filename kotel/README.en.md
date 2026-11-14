# Boiler over radio (kotel-rf)

[Česká verze](README.md)

> [!CAUTION]
> **Writing to the boiler is at your own risk.** The gateway sends commands
> to the boiler just like the original room unit. The protocol is
> reverse-engineered and neither documented nor supported by the
> manufacturer. A wrong command can switch the boiler off, let the home
> cool down (and pipes freeze), set scalding hot water or change service
> parameters. Tampering with the heating may affect the boiler's
> **warranty and service contract**. Transmitting on 868 MHz must respect
> the permitted power and duty cycle of your national regulator (in the EU
> the SRD rules). Do not automate anything whose effect you do not fully
> understand. No liability for any damage. If unsure, use read-only
> commands only (`rx`, `query`, `info`).

A Raspberry Pi with a **TI CC1101** radio module talks to the boiler's
**Siemens AVS71** radio receiver exactly like the **Siemens QAA78**
wireless room unit. The original room unit keeps working, the gateway is
just another participant. No boiler wiring changes.

Features:

- **passive listening** to room unit and boiler traffic (no transmission),
- **reading values** like the room unit's Info screen: boiler, return,
  outside, room and hot water temperatures, pressure, burner state,
  modulation, burner hours and starts, last fault,
- **writing**: comfort setpoint of circuit 1, hot water on/off, circuit 1
  operating mode (protection, auto, reduced, comfort). A generic raw write
  exists but is disabled by default,
- **daemon with an HTTP API** (localhost only, bearer token) for Home
  Assistant, Node-RED or your own system.

Every write is query → SET → ACK → query, so the gateway returns the value
the boiler reports after the change, not just what was sent.

## Who is it for

Verified on a **Brötje** gas condensing boiler with Siemens LMS controls,
an **AVS71.393** radio receiver and a **QAA78.610** room unit. Siemens supplies the
AVS71 receiver and QAA78 room unit with other boilers using its LMS
controls too. It may work there, but that is unverified and parameters
may differ. Always start with passive
listening and reading.

Not supported:

- boilers with a wired room unit (BSB/OpenTherm over cable). For wired BSB
  see [BSB-LAN](https://github.com/fredlcore/BSB-LAN), for OpenTherm the
  OpenTherm Gateway,
- other radio room units (other generations, Bosch, Vaillant, Viessmann),
- time programs from a UI. Writing programs works through the raw write
  but is experimental.

## Hardware

| Part | Note |
|---|---|
| Raspberry Pi (a Zero 2 W is enough) | Raspberry Pi OS, SPI enabled |
| CC1101 module **for 868 MHz** | do not buy the 433 MHz version, the RF front end differs |
| 868 MHz antenna | a spring antenna is fine, keep it away from metal |
| 8 wires | soldered to the module, female DuPont on the Pi header |

Wiring (physical pins of the 40-pin header). **The module is 3.3 V only,
never connect it to 5 V (pins 2 and 4).**

| CC1101 | Pi pin | Signal |
|---|---|---|
| VCC | 1 | 3.3 V |
| GND | 6 | GND |
| MOSI | 19 | GPIO10 / SPI0 MOSI |
| SCLK | 23 | GPIO11 / SPI0 SCLK |
| MISO | 21 | GPIO9 / SPI0 MISO |
| CSn | 24 | GPIO8 / SPI0 CE0 |
| GDO0 | 22 | GPIO25 |
| GDO2 | 18 | GPIO24 (unused) |

## Getting started

```bash
echo "dtparam=spi=on" | sudo tee -a /boot/firmware/config.txt && sudo reboot
sudo apt install python3-spidev python3-rpi-lgpio

sudo mkdir -p /opt/kotel-rf && sudo cp bsb_rf.py cc1101.py kotel_rf.py /opt/kotel-rf/
cd /opt/kotel-rf
sudo python3 kotel_rf.py check          # PARTNUM 0x00, VERSION 0x14 (or 0x04)
sudo python3 kotel_rf.py rx             # passive; change the setpoint on the room unit and watch
sudo python3 kotel_rf.py query temp     # first transmission: a query only
sudo python3 kotel_rf.py info
```

`rx` must show frames with `"crc_ok": true` from both `"role": "controller"`
(room unit) and `"role": "boiler"`. Without room unit frames the gateway
sends nothing: it learns the address header and sequence number from them.
The header is never hard-coded.

Only after reading works, try a write with the room unit in sight:

```bash
sudo python3 kotel_rf.py set-temp 20.5 --yes     # then back to the original value
sudo python3 kotel_rf.py set-dhw on --yes
```

Daemon:

```bash
sudo cp kotel-rf.env.example /etc/kotel-rf.env && sudo chmod 600 /etc/kotel-rf.env
sudo nano /etc/kotel-rf.env               # KOTEL_RF_TOKEN=$(openssl rand -hex 24)
sudo cp systemd/kotel-rf.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kotel-rf
```

Only one process may own the radio. Stop the daemon before manual commands
(`systemctl stop kotel-rf`).

## HTTP API

Header `Authorization: Bearer <token>`, JSON. Listens on `127.0.0.1` only.
Expose it deliberately (e.g. over a VPN), never to the internet.

| Call | Does |
|---|---|
| `GET /state` | last known state, no transmission |
| `POST /query {"what": "temp" \| "dhw" \| "<8 hex>"}` | one query |
| `POST /info` | re-reads `INFO_PARAMS` |
| `POST /setpoint {"temp_c": 20.5}` | comfort setpoint within `KOTEL_RF_MIN_C`–`KOTEL_RF_MAX_C` in 0.5 steps, hard limit 10–28 °C |
| `POST /dhw {"on": true}` | hot water |
| `POST /heating {"mode": 0..3}` | circuit 1 mode |
| `POST /set {"cmd": "<8 hex>", "payload": "<hex>"}` | raw write, only with `KOTEL_RF_ALLOW_RAW_SET=1` |

## Protocol in short

- 2-FSK around 868.3 MHz, deviation about ±55 kHz, Manchester. The room
  unit sends 19,532 chips/s, the boiler 19,200. CC1101 receives at 19,390
  and transmits at the room unit rate.
- Preamble `0101…`, chip sync pattern `0010 0110 1100 1001 1011` (the first
  16 = 0x26C9 as CC1101 sync word). Manchester and nibble framing are done
  in Python from raw chips. Each byte = 10 symbols (1, four inverted bits
  LSB first, 1, four bits).
- Frame `[0x38|seq] [header 6 B] LEN TYPE CMD(4) PAYLOAD CRC16`,
  CRC-16/XMODEM. TYPE 3 SET, 4 ACK, 6 query, 7 answer.
- CMD is the BSB-LAN parameter ID with the first two bytes swapped
  (BSB-LAN `0x2D3D058E` → on air `3d2d058e`).
- Values: first byte is a flag (0x01 = not available), temperatures
  int16/64 °C, enums 1 byte, pressure uint16/10 bar.
- SET for values that can be "---" (e.g. the legionella time) needs flag
  `06`. With `01` the boiler stores the value but keeps it disabled.
- Frames over 64 chip bytes (time programs) do not fit the CC1101 TX FIFO
  at once, so `transmit()` refills the FIFO while sending.

Sources: [BSB-LAN](https://github.com/fredlcore/BSB-LAN) (parameter IDs,
scaling, checksum), [TI CC1101](https://www.ti.com/product/CC1101).

## Tests without hardware

```bash
python3 -m unittest discover -s tests -v
```

Tests cover frame building, Manchester, CRC and value decoding on made-up
frames. The CC1101 driver and transmission were verified in service on the
author's original setup. This cleaned-up version has not been run on
hardware yet.
