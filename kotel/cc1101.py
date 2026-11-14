"""Ovladač modulu TI CC1101 (868 MHz) na SPI Raspberry Pi pro spojení QAA78/AVS71.

Surový režim chipů: 2-FSK, asi 19,4 kchip/s (ovladač QAA78 vysílá 19 532,
kotel AVS71 19 200), bez hardwarového Manchesteru, sync 0x26C9, nekonečná
délka paketu. Manchester, rámování a CRC řeší bsb_rf.py.

CC1101 driver for Raspberry Pi SPI. Needs python3-spidev and RPi.GPIO
(on current Raspberry Pi OS: python3-rpi-lgpio).
"""
from __future__ import annotations

import json
import sys
import time

import bsb_rf

XTAL = 26_000_000
FREQ_HZ = 868_295_000
GDO0 = 25          # GPIO25 (fyzický pin 22) = přerušení „sync nalezen“
TX_RATE_REG = 0x8B  # DRATE_M=139 -> 19 536 chip/s, jako ovladač QAA78
RX_RATE_REG = 0x87  # DRATE_M=135 -> 19 390 chip/s, mezi ovladačem a kotlem
TX_FIFO_SIZE = 64

REGS = {
    0x00: 0x2E,  # IOCFG2 high-Z
    0x02: 0x06,  # IOCFG0: sync nalezen .. konec paketu
    0x03: 0x47,  # FIFOTHR
    0x04: bsb_rf.SYNC_WORD[0], 0x05: bsb_rf.SYNC_WORD[1],
    0x06: 0xFF,  # PKTLEN
    0x07: 0x00,  # PKTCTRL1: bez stavových bajtů a adres
    0x08: 0x02,  # PKTCTRL0: bez whiteningu, bez CRC, nekonečná délka
    0x0B: 0x06,  # FSCTRL1 IF
    0x0C: 0x00,
    0x10: 0x79,  # MDMCFG4: RX BW 232 kHz, DRATE_E=9
    0x11: RX_RATE_REG,
    0x12: 0x06,  # MDMCFG2: 2-FSK, bez Manchesteru, sync 16/16 + carrier sense
    0x13: 0x22,  # MDMCFG1
    0x14: 0xF8,
    0x15: 0x51,  # DEVIATN ~57 kHz
    0x17: 0x30,  # MCSM1: RX->IDLE, TX->IDLE
    0x18: 0x18,  # MCSM0: kalibrace IDLE->RX/TX
    0x19: 0x16, 0x1A: 0x6C,
    0x1B: 0x43, 0x1C: 0x40, 0x1D: 0x91,
    0x21: 0x56, 0x22: 0x10,
    0x23: 0xE9, 0x24: 0x2A, 0x25: 0x00, 0x26: 0x1F,
    0x29: 0x59, 0x2C: 0x81, 0x2D: 0x35, 0x2E: 0x09,
}
_f = round(FREQ_HZ * 2**16 / XTAL)
REGS[0x0D], REGS[0x0E], REGS[0x0F] = (_f >> 16) & 0xFF, (_f >> 8) & 0xFF, _f & 0xFF


def _gpio():
    import RPi.GPIO as GPIO   # noqa: N814  (python3-rpi-lgpio)
    return GPIO


class CC1101:
    def __init__(self, bus: int = 0, device: int = 0):
        import spidev
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = 2_000_000
        self.spi.mode = 0
        self.GPIO = _gpio()
        self.GPIO.setmode(self.GPIO.BCM)
        self.GPIO.setup(GDO0, self.GPIO.IN)

    def strobe(self, c): return self.spi.xfer2([c])[0]
    def write(self, a, v): self.spi.xfer2([a, v])
    def read(self, a): return self.spi.xfer2([0x80 | a, 0])[1]
    def status(self, a): return self.spi.xfer2([0xC0 | a, 0])[1]
    def read_fifo(self, n): return bytes(self.spi.xfer2([0xFF] + [0] * n)[1:])
    def gdo0(self): return self.GPIO.input(GDO0)

    def identify(self) -> tuple[int, int]:
        """PARTNUM a VERSION. Správně 0x00 a 0x14 (starší čipy 0x04); 0x00/0xFF = chyba zapojení."""
        self.strobe(0x30); time.sleep(0.01)
        return self.status(0x30), self.status(0x31)

    def reset_and_configure(self):
        self.strobe(0x30); time.sleep(0.01)
        for a, v in REGS.items():
            self.write(a, v)
        bad = {a: self.read(a) for a in REGS if self.read(a) != REGS[a]}
        if bad:
            raise RuntimeError(f"registry nesedí po zápisu (zapojení SPI?): {bad}")

    def rssi_dbm(self):
        r = self.status(0x34)
        return (r - 256 if r >= 128 else r) / 2 - 74

    def idle_flush(self):
        self.strobe(0x36)
        t = time.time()
        while self.status(0x35) & 0x1F != 1 and time.time() - t < 0.5:
            pass
        self.strobe(0x3A)

    def rx(self):
        self.idle_flush(); self.strobe(0x34)

    def close(self):
        try:
            self.idle_flush(); self.strobe(0x36)
        finally:
            self.GPIO.cleanup()


class Receiver:
    """Příjem rámců. Pamatuje si poslední hlavičku a pořadové číslo vlastního ovladače."""

    def __init__(self, radio: CC1101, log=None, on_frame=None):
        self.radio, self.log, self.on_frame = radio, log, on_frame
        self.controller_header: bytes | None = None
        self.seq: int | None = None

    def receive_one(self, timeout: float) -> dict | None:
        radio = self.radio
        t_end = time.time() + timeout
        while time.time() < t_end:
            if not radio.gdo0():
                time.sleep(0.001); continue
            t0 = time.time(); buf = bytearray(); need = None; rssi = radio.rssi_dbm(); last = t0
            while time.time() - last < 0.15 and (need is None or len(buf) < need):
                n = radio.status(0x3B) & 0x7F
                if n > 1:
                    buf += radio.read_fifo(n - 1); last = time.time()
                    if need is None and len(buf) >= 23:
                        frame, _, _ = bsb_rf.decode_frame("".join(f"{b:08b}" for b in buf))
                        need = bsb_rf.expected_chip_bytes(frame) or 24
                time.sleep(0.0005)
            radio.idle_flush()
            chips = "".join(f"{b:08b}" for b in buf)
            frame, framing_ok, manch_err = bsb_rf.decode_frame(chips)
            info = bsb_rf.interpret(frame)
            if not info.get("crc_ok"):          # zkusit najít sync dál v bufferu
                pos = chips.find(bsb_rf.SYNC_CHIPS[:16], 8)
                while pos >= 0 and not info.get("crc_ok"):
                    f2, fr2, me2 = bsb_rf.decode_frame(chips[pos + 16:])
                    i2 = bsb_rf.interpret(f2)
                    if i2.get("crc_ok"):
                        frame, framing_ok, manch_err, info = f2, fr2, me2, i2
                    pos = chips.find(bsb_rf.SYNC_CHIPS[:16], pos + 1)
            rec = {"t": round(t0, 3), "clock": time.strftime("%H:%M:%S", time.localtime(t0)),
                   "rssi_dbm": rssi, "framing_ok": framing_ok, "manchester_errors": manch_err, **info}
            if info.get("crc_ok") and info.get("role") == "controller":
                self.controller_header = bytes.fromhex(info["header_hex"])
                self.seq = info["seq"]
            self.emit(rec)
            radio.rx()
            return rec
        return None

    def emit(self, rec: dict):
        line = json.dumps(rec, ensure_ascii=False)
        if self.log:
            self.log.write(line + "\n"); self.log.flush()
        if self.on_frame:
            self.on_frame(rec)


def _txbytes(radio: CC1101) -> int:
    """Bajty čekající v TX FIFO; číst, dokud se dvě čtení neshodnou (errata CC1101)."""
    a = radio.status(0x3A) & 0x7F
    while True:
        b = radio.status(0x3A) & 0x7F
        if a == b:
            return a
        a = b


def transmit(radio: CC1101, frame: bytes) -> float:
    """Odvysílá jeden rámec rychlostí ovladače QAA78 a vrátí rádio do příjmu. Vrací čas vysílání."""
    raw = bsb_rf.encode_chips(frame)
    if len(raw) > 255:
        raise ValueError(f"rámec je na vysílání s pevnou délkou moc dlouhý: {len(raw)} B chipů")
    radio.idle_flush(); radio.strobe(0x3B)                    # SFTX
    radio.write(0x11, TX_RATE_REG); radio.write(0x12, 0x00)  # rychlost QAA78, bez HW preambule a sync
    radio.write(0x08, 0x00); radio.write(0x06, len(raw))      # pevná délka = celý tok chipů
    radio.spi.xfer2([0x7E, 0xC0])                             # PATABLE asi +10 dBm
    head, rest = raw[:TX_FIFO_SIZE], raw[TX_FIFO_SIZE:]
    radio.spi.xfer2([0x7F] + list(head))
    t_tx = time.time(); radio.strobe(0x35)                    # STX
    while rest and time.time() - t_tx < 1:                    # dlouhé rámce: doplňovat FIFO za běhu
        free = TX_FIFO_SIZE - _txbytes(radio) - 1
        if free >= 8:
            chunk, rest = rest[:free], rest[free:]
            radio.spi.xfer2([0x7F] + list(chunk))
        else:
            time.sleep(0.0005)
    while radio.status(0x35) & 0x1F not in (1, 22):
        if time.time() - t_tx > 1:
            break
        time.sleep(0.001)
    marc = radio.status(0x35) & 0x1F
    if marc == 22 or rest:
        print(f"transmit: podtečení TX FIFO (marc={marc}, neodesláno {len(rest)} z {len(raw)} B)", file=sys.stderr)
    radio.write(0x11, RX_RATE_REG); radio.write(0x12, 0x06); radio.write(0x08, 0x02)
    radio.rx()
    return t_tx
