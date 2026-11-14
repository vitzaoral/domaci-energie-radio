# Kotel přes rádio (kotel-rf)

[English version](README.en.md)

> [!CAUTION]
> **Zápis do kotle je na vlastní nebezpečí.** Brána vysílá na kotel povely
> jako originální ovladač. Protokol je zpětně rozluštěný, výrobce ho
> nedokumentuje ani nepodporuje. Chybný povel může kotel vypnout, nechat
> byt vychladnout (a vodu v potrubí zamrznout), nastavit horkou vodu
> s nebezpečím opaření nebo změnit servisní parametry. Zásah do topení může
> mít vliv na **záruku a servisní smlouvu** kotle. Vysílání v pásmu 868 MHz
> musí dodržet povolený výkon a střídu podle všeobecného oprávnění ČTÚ
> (v EU obdobně). Kotel nesmí řídit nic, u čeho nevíte, co přesně dělá.
> Autor neručí za žádné škody. Pokud si nejste jisti, používejte jen čtení
> (`rx`, `query`, `info`).

Raspberry Pi s rádiovým modulem **TI CC1101** mluví s rádiovým přijímačem
kotle **Siemens AVS71** stejně jako bezdrátový pokojový ovladač **Siemens
QAA78**. Originální ovladač funguje dál, brána se jen připojí jako další
účastník. Kabeláž kotle se nemění.

Co umí:

- **pasivní poslech** komunikace ovladače a kotle (nic nevysílá),
- **čtení hodnot** jako obrazovka Info ovladače: teploty kotle, zpátečky,
  venku, v místnosti a teplé vody, tlak, stav hořáku, modulace, hodiny
  a starty hořáku, poslední porucha,
- **zápis**: denní teplota okruhu 1, příprava teplé vody zapnout/vypnout,
  režim okruhu 1 (ochranný, automatika, útlum, komfort); obecný zápis
  libovolného parametru je ve výchozím stavu vypnutý,
- **démon s HTTP API** (jen místně, s tokenem) pro Home Assistant, Node-RED
  nebo vlastní systém.

Každý zápis má podobu dotaz → SET → potvrzení ACK → nový dotaz. Brána tak
vrací hodnotu, kterou kotel po změně sám nahlásil, ne jen to, co se poslalo.

## Pro koho to je

Ověřeno na plynovém kondenzačním kotli **Brötje** s regulací Siemens LMS,
rádiovým přijímačem **AVS71.393** a ovladačem **QAA78.610**. Přijímač AVS71 a ovladač
QAA78 dodává Siemens i k dalším kotlům se svou regulací LMS. Tam to může
fungovat, ověřené to ale není a parametry se mohou lišit. Vždy začněte pasivním
poslechem a čtením.

Neumí:

- kotle s drátovým ovladačem (BSB/OpenTherm po kabelu). Pro BSB po drátu je
  projekt [BSB-LAN](https://github.com/fredlcore/BSB-LAN), pro OpenTherm
  OpenTherm Gateway,
- jiné rádiové ovladače (jiné generace Siemens, Bosch, Vaillant, Viessmann a další),
- časové programy z UI. Zápis programů funguje přes obecný zápis, je ale
  na úrovni pokusu.

## Hardware

| Díl | Poznámka |
|---|---|
| Raspberry Pi (stačí Zero 2 W) | Raspberry Pi OS, zapnuté SPI |
| Modul CC1101 **na 868 MHz** | verzi na 433 MHz nekupujte, liší se vstupní obvody |
| anténa 868 MHz | pružinová stačí, mimo kovové díly |
| 8 vodičů | pájené k modulu, dutinky na lištu Pi |

Zapojení (fyzické piny 40pinové lišty Pi). **Modul je jen na 3,3 V,
nikdy ho nepřipojujte na 5 V (piny 2 a 4).**

| CC1101 | Pi pin | Signál |
|---|---|---|
| VCC | 1 | 3,3 V |
| GND | 6 | GND |
| MOSI | 19 | GPIO10 / SPI0 MOSI |
| SCLK | 23 | GPIO11 / SPI0 SCLK |
| MISO | 21 | GPIO9 / SPI0 MISO |
| CSn | 24 | GPIO8 / SPI0 CE0 |
| GDO0 | 22 | GPIO25 |
| GDO2 | 18 | GPIO24 (nepoužito) |

## Rozjetí

```bash
# SPI a knihovny
echo "dtparam=spi=on" | sudo tee -a /boot/firmware/config.txt && sudo reboot
sudo apt install python3-spidev python3-rpi-lgpio

sudo mkdir -p /opt/kotel-rf && sudo cp bsb_rf.py cc1101.py kotel_rf.py /opt/kotel-rf/
cd /opt/kotel-rf
sudo python3 kotel_rf.py check          # PARTNUM 0x00, VERSION 0x14 (nebo 0x04)
sudo python3 kotel_rf.py rx             # pasivně; změňte teplotu na ovladači a sledujte rámce
sudo python3 kotel_rf.py query temp     # první vysílání: jen dotaz
sudo python3 kotel_rf.py info
```

V `rx` musíte vidět rámce s `"crc_ok": true`, `"role": "controller"`
(ovladač) i `"role": "boiler"` (kotel). Bez rámců ovladače brána nic
nevyšle: z nich převezme adresní hlavičku a pořadové číslo. Hlavička se
v kódu nikde nepíše natvrdo.

Teprve po úspěšném čtení zkuste zápis. Mějte u toho ovladač a zkontrolujte
na něm změnu:

```bash
sudo python3 kotel_rf.py set-temp 20.5 --yes     # pak zpět na původní hodnotu
sudo python3 kotel_rf.py set-dhw on --yes
```

Démon:

```bash
sudo cp kotel-rf.env.example /etc/kotel-rf.env && sudo chmod 600 /etc/kotel-rf.env
sudo nano /etc/kotel-rf.env               # KOTEL_RF_TOKEN=$(openssl rand -hex 24)
sudo cp systemd/kotel-rf.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kotel-rf
```

Rádio smí držet jen jeden proces. Před ručními příkazy démona zastavte
(`systemctl stop kotel-rf`).

## HTTP API

Hlavička `Authorization: Bearer <token>`, JSON. Démon poslouchá jen na
`127.0.0.1`. Do sítě ho pouštějte jen vědomě (například přes VPN), nikdy
do internetu.

| Volání | Co dělá |
|---|---|
| `GET /state` | poslední známý stav, nic nevysílá |
| `POST /query {"what": "temp" \| "dhw" \| "<8 hex>"}` | jeden dotaz |
| `POST /info` | znovu přečte hodnoty `INFO_PARAMS` |
| `POST /setpoint {"temp_c": 20.5}` | denní teplota v mezích `KOTEL_RF_MIN_C` až `KOTEL_RF_MAX_C` po 0,5, pevná pojistka 10–28 °C |
| `POST /dhw {"on": true}` | příprava teplé vody |
| `POST /heating {"mode": 0..3}` | režim okruhu 1 |
| `POST /set {"cmd": "<8 hex>", "payload": "<hex>"}` | obecný zápis, jen s `KOTEL_RF_ALLOW_RAW_SET=1` |

## Protokol v kostce

- 2-FSK kolem 868,3 MHz, odchylka asi ±55 kHz, Manchester. Ovladač vysílá
  19 532 chipů/s, kotel 19 200. CC1101 přijímá na 19 390 a vysílá rychlostí
  ovladače.
- Preambule `0101…`, synchronizační vzor chipů `0010 0110 1100 1001 1011`
  (prvních 16 = 0x26C9 jako sync slovo CC1101), Manchester a rámování nibblů
  skládá Python ze surových chipů. Každý bajt = 10 symbolů (1, čtyři
  invertované bity LSB napřed, 1, čtyři bity).
- Rámec `[0x38|seq] [hlavička 6 B] LEN TYP CMD(4) PAYLOAD CRC16`,
  CRC-16/XMODEM. TYP 3 SET, 4 ACK, 6 dotaz, 7 odpověď.
- CMD je ID parametru z BSB-LAN s prohozenými prvními dvěma bajty
  (BSB-LAN `0x2D3D058E` → na drátě `3d2d058e`).
- Hodnoty: první bajt příznak (0x01 = nedostupné), teploty int16/64 °C,
  výčty 1 bajt, tlak uint16/10 bar.
- U SET hodnot, které jdou vypnout („---“, např. čas legionelové funkce),
  musí být příznak `06`. S `01` kotel hodnotu uloží, ale nechá ji vypnutou.
- Rámce delší než 64 B chipů (časové programy) se nevejdou do TX FIFO
  CC1101 najednou, `transmit()` proto FIFO během vysílání doplňuje.

Zdroje: [BSB-LAN](https://github.com/fredlcore/BSB-LAN) (ID parametrů,
škálování, kontrolní součet), [TI CC1101](https://www.ti.com/product/CC1101).

## Testy bez hardwaru

```bash
python3 -m unittest discover -s tests -v
```

Testy ověří skládání rámců, Manchester, CRC a dekódování hodnot na
vymyšlených rámcích. Ovladač CC1101 a vysílání autor ověřil v provozu
na původní instalaci. Tato vyčištěná verze je na hardwaru zatím neověřená.
