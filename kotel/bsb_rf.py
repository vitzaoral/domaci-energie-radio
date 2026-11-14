"""Protokol rádiového spojení Siemens QAA78 <-> AVS71 (BSB po rádiu, 868,3 MHz).

Čistý Python bez hardwaru: skládání a rozklad rámců, Manchester + rámování
nibblů, CRC-16/XMODEM, dekódování hodnot. Ovladač rádia je v cc1101.py.

Radio protocol of the Siemens QAA78 room unit and AVS71 receiver (BSB over
radio). Pure Python, no hardware: frame building/parsing, Manchester and
nibble framing, CRC-16/XMODEM and value decoding.

Rámec (bajty po dekódování):
    [0x38|seq] [adresní hlavička 6 B] LEN TYP CMD(4) PAYLOAD CRC16
- seq: 3bitové pořadové číslo, ovladač ho zvyšuje s každou zprávou.
- hlavička: 1. bajt 0x38 = ovladač (QAA78), 0x30 = kotel (AVS71). Zbytek
  hlavičky se u různých instalací může lišit, proto se nikde nepíše natvrdo:
  vysílač ji převezme z posledního zachyceného rámce vlastního ovladače.
- LEN: délka celého rámce včetně CRC, TYP: 3 SET, 4 ACK, 6 QUR (dotaz), 7 ANS.
- CMD: ID parametru z BSB-LAN s prohozenými prvními dvěma bajty
  (BSB-LAN 0x2D3D058E -> na drátě 3d2d058e), odpověď kotle je má prohozené zpět.
"""
from __future__ import annotations

import binascii

# Synchronizační vzor chipů. Prvních 16 chipů (0x26C9) hledá CC1101 hardwarově.
SYNC_CHIPS = "00100110110010011011"
SYNC19 = "0010011011001001101"
TAIL = "10110011001"
SYNC_WORD = (0x26, 0xC9)

ROLE_CONTROLLER = 0x38
ROLE_BOILER = 0x30
TYPES = {3: "SET", 4: "ACK", 6: "QUR", 7: "ANS"}
T_SET, T_ACK, T_QUR, T_ANS = 3, 4, 6, 7

# Ověřené parametry (Brötje / Siemens LMS, číslování podle BSB-LAN), drátová podoba.
CMD_ROOM_SETPOINT = bytes.fromhex("3d2d058e")   # 710 denní (komfortní) teplota okruhu 1
CMD_DHW_MODE = bytes.fromhex("3d310571")        # 1600 příprava teplé vody vyp/zap
CMD_HEATING_MODE = bytes.fromhex("3d2d0574")    # 700 režim okruhu 1: 0 ochranný, 1 auto, 2 útlum, 3 komfort
CMD_HK2_MODE = bytes.fromhex("3d2e0574")        # 1000 režim okruhu 2
CMD_HK2_COMFORT = bytes.fromhex("3d2e058e")     # 1010 denní teplota okruhu 2
CMD_CONTROLLER_ROOM_TEMP = "3c2c0228"           # ovladač posílá kotli prostorovou teplotu

# Hodnoty k pravidelnému čtení: (klíč, drátové CMD, druh). Ověřeno na kotli
# Brötje s přijímačem AVS71. U jiných kotlů s regulací Siemens LMS se mohou lišit,
# ID hledejte v BSB-LAN (BSB_LAN_defs.h).
INFO_PARAMS = [
    ("room_setpoint_c", "3d2d058e", "temp"),       # 710
    ("dhw_mode", "3d310571", "enum"),              # 1600
    ("heating_mode", "3d2d0574", "enum"),          # 700
    ("burner_status", "3d050f66", "enum"),         # 8009
    ("boiler_status", "3d0507a9", "enum"),         # 8005
    ("heating_status", "3d0507a3", "enum"),        # 8000
    ("dhw_status", "3d0507a1", "enum"),            # 8003
    ("boiler_temp_c", "3d0d0519", "temp"),         # 8310
    ("return_temp_c", "3d11051a", "temp"),         # 8314
    ("outside_temp_c", "3d050521", "temp"),        # 8700
    ("room_temp_c", "3d2d051e", "temp"),           # 8740
    ("dhw_temp_c", "3d31052f", "temp"),            # 8830
    ("dhw_setpoint_c", "3d3106b9", "temp"),        # 1610
    ("water_pressure_bar", "3d0519f0", "pressure"),  # 8327
    ("burner_modulation_pct", "3d050834", "percent"),  # 8326
    ("burner_hours", "3d0d093b", "hours"),         # 8330
    ("burner_starts", "3d0508a5", "dword"),        # 8331
    ("last_error_at", "3d0506d3", "datetime"),     # 6800
    ("last_error_code", "3d050814", "word"),       # 6801
]
BURNER_ON_CODES = {4, 18}   # 8009 (Brötje): „v provozu“


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0)


def reply_cmd(cmd: bytes) -> str:
    """Odpověď kotle má první dva bajty CMD prohozené."""
    return (cmd[1:2] + cmd[0:1] + cmd[2:]).hex()


def build_frame(seq: int, header: bytes, msg_type: int, cmd: bytes, payload: bytes = b"") -> bytes:
    if len(header) != 6 or header[0] != ROLE_CONTROLLER:
        raise ValueError("hlavička musí mít 6 bajtů a začínat 0x38 (převezměte ji z rámce ovladače)")
    if len(cmd) != 4:
        raise ValueError("CMD má 4 bajty")
    body = bytes([0x38 | (seq & 7)]) + header + bytes([len(payload) + 15, msg_type]) + cmd + payload
    return body + crc16(body).to_bytes(2, "big")


def build_query(seq: int, header: bytes, cmd: bytes) -> bytes:
    return build_frame(seq, header, T_QUR, cmd)


def build_set_temperature(seq: int, header: bytes, temp_c: float, cmd: bytes = CMD_ROOM_SETPOINT) -> bytes:
    return build_frame(seq, header, T_SET, cmd, b"\x01" + int(round(temp_c * 64)).to_bytes(2, "big"))


def build_set_enum(seq: int, header: bytes, cmd: bytes, value: int) -> bytes:
    return build_frame(seq, header, T_SET, cmd, b"\x01" + bytes([value & 0xFF]))


def build_set_dhw(seq: int, header: bytes, on: bool) -> bytes:
    return build_set_enum(seq, header, CMD_DHW_MODE, 1 if on else 0)


def encode_chips(frame: bytes, preamble_chips: int = 40) -> bytes:
    """Rámec -> tok chipů pro vysílač (preambule, sync, Manchester, ocas), zarovnaný na bajty."""
    syms: list[int] = []
    for b in frame:
        bits = [(b >> j) & 1 for j in range(8)]
        syms += [1] + [1 - x for x in bits[:4]] + [1] + [1 - x for x in bits[4:]]
    chips = "01" * (preamble_chips // 2) + SYNC19 + "".join("10" if d else "01" for d in syms) + TAIL
    chips += "0" * (-len(chips) % 8)
    return bytes(int(chips[i:i + 8], 2) for i in range(0, len(chips), 8))


def decode_frame(chips: str) -> tuple[bytes, bool, int]:
    """chips: řetězec '0'/'1' hned za 16 chipy hardwarového sync slova.

    Vrací (bajty, rámování v pořádku, počet chyb Manchesteru)."""
    s = chips[3:]   # zbytek sync vzoru (3 chipy)
    pairs = [(s[i], s[i + 1]) for i in range(0, len(s) - 1, 2)]
    dec = [int(a) for a, _ in pairs]
    nbytes = max(0, (len(dec) - 5) // 10)
    out, framing_ok, manch_err = bytearray(), True, 0
    for k in range(nbytes):
        grp = dec[k * 10:(k + 1) * 10]
        if grp[0] != 1 or grp[5] != 1:
            framing_ok = False
        bits = [1 - grp[i] for i in (1, 2, 3, 4, 6, 7, 8, 9)]
        out.append(sum(b << j for j, b in enumerate(bits)))
        manch_err += sum(1 for a, b in pairs[k * 10:(k + 1) * 10] if a == b)
    return bytes(out), framing_ok, manch_err


def expected_chip_bytes(frame_prefix: bytes) -> int | None:
    """Kolik bajtů z FIFO je potřeba na celý rámec, když už známe bajt LEN."""
    if len(frame_prefix) > 7 and 8 <= frame_prefix[7] <= 80:
        return (3 + frame_prefix[7] * 20 + 10) // 8 + 2
    return None


def interpret(frame: bytes) -> dict:
    info: dict = {"frame_hex": frame.hex(), "crc_ok": False}
    if len(frame) <= 7 or not 8 <= frame[7] <= len(frame):
        return info
    f = frame[:frame[7]]
    info["crc_ok"] = crc16(f) == 0
    if not info["crc_ok"] or len(f) <= 13:
        return info
    info["role"] = {ROLE_CONTROLLER: "controller", ROLE_BOILER: "boiler"}.get(f[1], f"{f[1]:02x}")
    info["seq"] = f[0] & 7
    info["header_hex"] = f[1:7].hex()
    info["type"] = TYPES.get(f[8], str(f[8]))
    info["cmd"] = f[9:13].hex()
    info["payload"] = f[13:-2].hex()
    if f[9:13] in (CMD_ROOM_SETPOINT, bytes.fromhex(reply_cmd(CMD_ROOM_SETPOINT))) and len(f) == 18:
        info["temp_c"] = int.from_bytes(f[14:16], "big", signed=True) / 64
    if f[9:13] in (CMD_DHW_MODE, bytes.fromhex(reply_cmd(CMD_DHW_MODE))) and f[8] in (T_SET, T_ANS):
        info["dhw"] = {"0100": "off", "0101": "on", "0000": "off", "0001": "on"}.get(f[13:-2].hex(), "?")
    return info


def decode_info(kind: str, payload_hex: str | None):
    """Hodnota z odpovědi ANS. První bajt je příznak, 0x01 = hodnota není k dispozici."""
    if not payload_hex or len(payload_hex) < 4:
        return None
    flag = int(payload_hex[:2], 16)
    body = payload_hex[2:]
    if kind == "enum":
        return int(body[:2], 16)
    if flag != 0:
        return None
    if kind == "temp":
        return int.from_bytes(bytes.fromhex(body[:4]), "big", signed=True) / 64
    if kind == "pressure":
        return int(body[:4], 16) / 10
    if kind == "percent":
        return int(body[:2], 16)
    if kind == "word":
        return int(body[:4], 16)
    if kind == "dword":
        return int(body[:8], 16)
    if kind == "hours":
        return round(int(body[:8], 16) / 3600, 1)
    if kind == "datetime" and len(body) >= 16:
        b = bytes.fromhex(body[:16])   # rok-1900, měsíc, den, den v týdnu, h, min, s, příznaky
        return f"{1900 + b[0]:04d}-{b[1]:02d}-{b[2]:02d}T{b[4]:02d}:{b[5]:02d}:{b[6]:02d}"
    return None
