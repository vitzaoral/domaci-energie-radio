"""Testy protokolu bez hardwaru: python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bsb_rf  # noqa: E402
import kotel_rf  # noqa: E402

# Vymyšlená hlavička ovladače. Skutečnou si brána převezme z rámců vašeho ovladače.
HEADER = bytes.fromhex("38a1b2c3d400")


def over_the_air(frame: bytes) -> bytes:
    """Rámec -> chipy jako z vysílače -> co přijímač vydá za sync slovem -> dekódovaný rámec."""
    chips = "".join(f"{b:08b}" for b in bsb_rf.encode_chips(frame))
    pos = chips.find(bsb_rf.SYNC_CHIPS[:16])
    decoded, framing_ok, errors = bsb_rf.decode_frame(chips[pos + 16:])
    assert framing_ok or len(decoded) > len(frame), "rámování"
    return decoded


class FrameTest(unittest.TestCase):
    def test_set_temperature_roundtrip(self):
        frame = bsb_rf.build_set_temperature(5, HEADER, 20.5)
        self.assertEqual(frame[7], len(frame))
        self.assertEqual(bsb_rf.crc16(frame), 0)
        info = bsb_rf.interpret(over_the_air(frame))
        self.assertTrue(info["crc_ok"])
        self.assertEqual(info["type"], "SET")
        self.assertEqual(info["role"], "controller")
        self.assertEqual(info["seq"], 5)
        self.assertEqual(info["header_hex"], HEADER.hex())
        self.assertEqual(info["cmd"], "3d2d058e")
        self.assertEqual(info["temp_c"], 20.5)

    def test_dhw_and_query(self):
        info = bsb_rf.interpret(over_the_air(bsb_rf.build_set_dhw(1, HEADER, False)))
        self.assertEqual((info["type"], info["dhw"], info["payload"]), ("SET", "off", "0100"))
        info = bsb_rf.interpret(over_the_air(bsb_rf.build_query(2, HEADER, bsb_rf.CMD_DHW_MODE)))
        self.assertEqual((info["type"], info["cmd"]), ("QUR", "3d310571"))

    def test_corrupted_frame_fails_crc(self):
        frame = bytearray(bsb_rf.build_set_temperature(0, HEADER, 19.5))
        frame[15] ^= 0x01
        self.assertFalse(bsb_rf.interpret(bytes(frame))["crc_ok"])

    def test_boiler_answer(self):
        # odpověď kotle (vymyšlená hlavička 0x30...), ANS na 710 = 19,5 °C
        body = bytes([0x38 | 4]) + bytes.fromhex("30e1f2a3b406") + bytes([18, 7]) \
            + bytes.fromhex(bsb_rf.reply_cmd(bsb_rf.CMD_ROOM_SETPOINT)) + b"\x00" + int(19.5 * 64).to_bytes(2, "big")
        frame = body + bsb_rf.crc16(body).to_bytes(2, "big")
        info = bsb_rf.interpret(frame)
        self.assertEqual((info["role"], info["type"], info["temp_c"]), ("boiler", "ANS", 19.5))
        self.assertEqual(kotel_rf.value_of(info), 19.5)

    def test_header_is_required(self):
        with self.assertRaises(ValueError):
            bsb_rf.build_query(0, b"\x30" + bytes(5), bsb_rf.CMD_DHW_MODE)   # hlavička kotle, ne ovladače

    def test_long_frame_fits_one_transmission(self):
        # časový program: 12 B payload -> přes 64 B chipů (doplňování FIFO), ale pod 255 B
        frame = bsb_rf.build_frame(0, HEADER, bsb_rf.T_SET, bytes.fromhex("3d050a8c"), bytes(13))
        raw = bsb_rf.encode_chips(frame)
        self.assertTrue(64 < len(raw) <= 255)


class ValueTest(unittest.TestCase):
    def test_decode_info(self):
        self.assertEqual(bsb_rf.decode_info("temp", "000f20"), 60.5)
        self.assertEqual(bsb_rf.decode_info("temp", "010000"), None)       # hodnota není k dispozici
        self.assertEqual(bsb_rf.decode_info("pressure", "00000a"), 1.0)
        self.assertEqual(bsb_rf.decode_info("enum", "0119"), 25)
        self.assertEqual(bsb_rf.decode_info("dword", "0000028d9d"), 167325)
        self.assertEqual(bsb_rf.decode_info("datetime", "007a010f030e1e0000"), "2022-01-15T14:30:00")

    def test_setpoint_limits(self):
        self.assertEqual(kotel_rf.check_setpoint(20.5, 14, 25), 20.5)
        for bad in (20.3, 13.5, 25.5):
            with self.assertRaises(ValueError):
                kotel_rf.check_setpoint(bad, 14, 25)
        with self.assertRaises(ValueError):          # pevná pojistka 10–28 °C platí vždy
            kotel_rf.check_setpoint(30, 14, 40)


if __name__ == "__main__":
    unittest.main()
