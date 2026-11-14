"""Testy bez hardwaru: python3 -m unittest discover -s tests -v"""
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vodomer_radio as vr  # noqa: E402

SAMPLE = os.path.join(HERE, "sample-rtl433.jsonl")


def config(text: str = ""):
    path = os.path.join(HERE, "_tmp.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        return vr.load_config(path)
    finally:
        os.remove(path)


def sample_lines():
    with open(SAMPLE, encoding="utf-8") as f:
        return f.readlines()


class ParseTest(unittest.TestCase):
    def test_reading(self):
        r = vr.parse_line(sample_lines()[0], {"123456789": "byt"})
        self.assertEqual(r["meter_id"], "123456789")
        self.assertEqual(r["name"], "byt")
        self.assertEqual(r["volume_m3"], 254.321)
        self.assertEqual(r["volume_l"], 254321)
        self.assertEqual(r["meter_date"], "2026-11-01")
        self.assertEqual(r["rssi"], -0.4)

    def test_filters(self):
        lines = sample_lines()
        meters = {"123456789": "byt"}
        self.assertIsNone(vr.parse_line(lines[1], meters))   # cizí modul
        self.assertIsNone(vr.parse_line(lines[2], meters))   # jiný model
        self.assertIsNone(vr.parse_line(lines[4]))           # nedešifrováno
        self.assertIsNone(vr.parse_line(lines[5]))           # není JSON

    def test_id_without_leading_zeros(self):
        line = json.dumps({"model": vr.MODEL, "id": "012345678", "volume_m3": "1.000"})
        conf = config("[meters]\n12345678 = byt\n")
        self.assertEqual(vr.parse_line(line, vr.meters_from(conf))["name"], "byt")


class EmitTest(unittest.TestCase):
    def test_changes_and_heartbeat(self):
        e = vr.Emitter("changes", heartbeat_minutes=60)
        r = {"meter_id": "1", "volume_l": 10}
        self.assertTrue(e.should_emit(r, now=0))
        self.assertFalse(e.should_emit(r, now=60))
        self.assertTrue(e.should_emit({"meter_id": "1", "volume_l": 11}, now=120))
        self.assertTrue(e.should_emit({"meter_id": "1", "volume_l": 11}, now=120 + 3600))

    def test_run_stdout(self):
        conf = config("[meters]\n123456789 = byt\n[output]\nstdout = json\n")
        out = io.StringIO()
        self.assertEqual(vr.run(sample_lines(), conf, out=out), 2)   # 254,321 a změna na 254,338
        rows = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([x["volume_l"] for x in rows], [254321, 254338])


class HttpTest(unittest.TestCase):
    def test_post(self):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append((self.headers.get("Authorization"), json.loads(body)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = vr.HttpOutput(f"http://127.0.0.1:{srv.server_port}/odecet", token="TEST")
            out.send(vr.parse_line(sample_lines()[0]))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(got[0][0], "Bearer TEST")
        self.assertEqual(got[0][1]["volume_l"], 254321)
        self.assertFalse(out.queue)


class MqttTest(unittest.TestCase):
    def test_publish(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        received = bytearray()

        def broker():
            conn, _ = srv.accept()
            with conn:
                received.extend(conn.recv(1024))          # CONNECT
                conn.sendall(b"\x20\x02\x00\x00")         # CONNACK OK
                while True:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    received.extend(chunk)

        t = threading.Thread(target=broker, daemon=True)
        t.start()
        out = vr.MqttOutput("127.0.0.1", srv.getsockname()[1], "user", "secret", topic="vodomer/{id}")
        out.send(vr.parse_line(sample_lines()[0]))
        t.join(5)
        srv.close()
        self.assertEqual(received[0], 0x10)
        self.assertIn(b"vodomer/123456789", received)
        self.assertIn(b'"volume_l": 254321', received)
        self.assertTrue(received.endswith(b"\xe0\x00"))


@unittest.skipUnless(shutil.which("rtl_433"), "rtl_433 není nainstalován")
class Rtl433DecoderTest(unittest.TestCase):
    """Celá cesta: syntetický rádiový rámec -> dekodér 277 v rtl_433 -> vodomer_radio.py."""

    def test_end_to_end(self):
        with open(os.path.join(HERE, "frames.txt")) as f:
            frames = [x.strip() for x in f if x.startswith("{")]
        decoded = []
        for fr in frames:
            p = subprocess.run(["rtl_433", "-R", "277", "-F", "json", "-M", "time:iso:tz", "-y", fr],
                               capture_output=True, text=True, timeout=30)
            decoded += [x for x in p.stdout.splitlines() if x.startswith("{")]
        self.assertEqual(len(decoded), 2, decoded)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "vodomer_radio.py"), "--stdin", "--json"],
                           input="\n".join(decoded) + "\n", capture_output=True, text=True, timeout=30)
        rows = {r["meter_id"]: r for r in map(json.loads, p.stdout.splitlines())}
        self.assertEqual(rows["123456789"]["volume_m3"], 254.321)
        self.assertEqual(rows["123456789"]["meter_date"], "2026-11-01")
        self.assertEqual(rows["987654321"]["volume_l"], 1500)


if __name__ == "__main__":
    unittest.main()
