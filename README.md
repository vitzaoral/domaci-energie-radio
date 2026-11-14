# domaci-energie-radio

[English version](README.en.md) · Repozitář: https://github.com/vitzaoral/domaci-energie-radio

Vodoměr, plynový kotel a radiátory přes rádio, bez cloudu, na jednom
Raspberry Pi. Tři samostatné části, každou jde použít zvlášť:

| Část | Co dělá | Rádio | Hardware |
|---|---|---|---|
| [`vodomer/`](vodomer/) | čte stav vodoměru s modulem **Apator Metra E-RM 30** | příjem 868,9 MHz | RTL-SDR dongle |
| [`kotel/`](kotel/) | čte a řídí kotel s rádiovým ovladačem **Siemens QAA78** / přijímačem **AVS71** | příjem i vysílání 868,3 MHz | modul CC1101 na SPI |
| [`radiatory/`](radiatory/) | hlavice **Sonoff TRVZB** řídí podle pokojového čidla, přes zigbee2mqtt | Zigbee 2,4 GHz | Sonoff ZBDongle-E |

Všechny části posílají data dál jednoduchou cestou: stdout, HTTP nebo MQTT.
Dají se tak napojit na Home Assistant, Node-RED nebo vlastní systém.
Kód je v Pythonu, vodoměr a kotel potřebují jen standardní knihovnu
(kotel navíc `spidev` a GPIO), hlavice `paho-mqtt`.

> [!CAUTION]
> **Kotel a hlavice: bezpečnost a záruka.** Část `kotel/` vysílá povely
> do kotle podle zpětně rozluštěného protokolu, který výrobce nepodporuje.
> Část `radiatory/` mění nastavení hlavic. Chyba může vypnout topení,
> nechat byt vychladnout, přetopit místnost nebo nastavit horkou vodu
> s nebezpečím opaření. Zásah může mít vliv na **záruku a servisní
> smlouvu** kotle. Vysílání musí dodržet pravidla pro pásmo 868 MHz
> (v ČR všeobecné oprávnění ČTÚ). Používáte to na vlastní nebezpečí, autor
> neručí za žádné škody. Začněte vždy jen čtením. Vodoměr je čistě pasivní
> příjem a nic nevysílá.

## Co je potřeba

- Raspberry Pi (všechno běží i na Pi Zero 2 W) s Raspberry Pi OS.
- Na Pi Zero s jediným USB portem **OTG hub s vlastním napájením**.
  RTL-SDR a Zigbee dongle z jednoho hubu napájeného z Pi shazovaly Pi.
- RTL-SDR a Zigbee dongle dejte na USB prodlužovačky dál od sebe a od
  zdroje hubu. Rušení zhoršilo příjem vodoměru.

## Rychlá zkouška bez hardwaru

```bash
git clone https://github.com/vitzaoral/domaci-energie-radio.git && cd domaci-energie-radio
for d in vodomer kotel radiatory; do (cd $d && python3 -m unittest discover -s tests); done
```

Test vodoměru navíc pustí skutečný dekodér rtl_433, pokud je nainstalovaný
(verze 25.12 a novější), na vymyšleném rádiovém rámci.

## Soukromí

- Přijímač vodoměru zachytí i vodoměry sousedů. Filtrujte jen svoje ID
  a cizí data neukládejte.
- Z průběhu spotřeby vody, topení a teplot se dá poznat, kdy je někdo doma.
  Brány proto poslouchají jen místně (`127.0.0.1`) s tokenem nebo heslem
  a nic nemá být dostupné z internetu.
- Nezveřejňujte síťový klíč Zigbee (`configuration.yaml` z2m), tokeny ani
  ID svých měřidel.

## Licence

MIT, viz [LICENSE](LICENSE). Projekt používá rtl_433 (GPL-2.0) a zigbee2mqtt
(GPL-3.0) jako samostatné programy a jejich kód neobsahuje. Popis protokolu
kotle vychází z veřejného projektu BSB-LAN a vlastního měření.
