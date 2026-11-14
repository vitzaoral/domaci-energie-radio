#!/usr/bin/env python3
"""kotel-rf: čtení a řízení plynového kotle přes rádio místo pokojového ovladače Siemens QAA78.

Raspberry Pi + CC1101 (868 MHz) mluví s rádiovým přijímačem kotle Siemens AVS71
stejně jako bezdrátový ovladač QAA78. Ovladač dál funguje, brána se jen připojí.

Příkazy:
  check                         ověří zapojení CC1101 (nic nevysílá)
  rx [--log SOUBOR]             pasivní poslech, JSON řádky (nic nevysílá)
  query temp|dhw|<8 hex>        jeden dotaz na kotel (krátké vysílání)
  info                          přečte hodnoty z INFO_PARAMS (krátká vysílání)
  set-temp <°C> --yes           změní denní teplotu okruhu 1
  set-dhw on|off --yes          zapne/vypne přípravu teplé vody
  heating-mode 0-3 --yes        režim okruhu 1: 0 ochranný, 1 auto, 2 útlum, 3 komfort
  serve                         démon s HTTP API (viz README)

Každý zápis = dotaz -> SET -> ACK -> dotaz; výsledek je hodnota, kterou kotel
po změně sám nahlásil.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bsb_rf

HARD_MIN_C, HARD_MAX_C = 10.0, 28.0   # pojistka, kterou nejde přenastavit


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class Gateway:
    """Rádiové transakce. Volající drží self.lock."""

    def __init__(self, log_path: str | None = None, on_frame=None):
        from cc1101 import CC1101, Receiver   # hardware jen tady, protokol jde testovat bez něj
        self.radio = CC1101()
        self.radio.reset_and_configure()
        self.log = open(log_path, "a") if log_path else None
        self.rxr = Receiver(self.radio, self.log, on_frame)
        self.lock = threading.Lock()
        self.radio.rx()

    def close(self):
        self.radio.close()
        if self.log:
            self.log.close()

    def wait_for_controller(self, timeout: float = 150) -> bool:
        """Vysílat se smí až po zachycení rámce vlastního ovladače (hlavička + pořadové číslo)."""
        t_end = time.time() + timeout
        while self.rxr.controller_header is None and time.time() < t_end:
            self.rxr.receive_one(5)
        if self.rxr.controller_header is not None:
            self.rxr.receive_one(1.0)   # nechat projít odpověď kotle
            return True
        return False

    def next_seq(self) -> int:
        self.rxr.seq = ((self.rxr.seq if self.rxr.seq is not None else int(time.time())) + 1) & 7
        return self.rxr.seq

    @property
    def header(self) -> bytes:
        if self.rxr.controller_header is None:
            raise RuntimeError("zatím nezachycen žádný rámec ovladače QAA78, nic se neodešle")
        return self.rxr.controller_header

    def send(self, frame: bytes, expect: str, want_cmd: str, attempts: int = 2):
        from cc1101 import transmit
        for _ in range(attempts):
            t_tx = transmit(self.radio, frame)
            rec = self.rxr.receive_one(1.0)
            if rec and rec.get("crc_ok") and rec.get("type") == expect and rec.get("cmd") == want_cmd:
                return rec, round((rec["t"] - t_tx) * 1000)
            time.sleep(0.3)
        return None, None

    def query(self, cmd: bytes):
        return self.send(bsb_rf.build_query(self.next_seq(), self.header, cmd), "ANS", bsb_rf.reply_cmd(cmd))

    def set(self, cmd: bytes, build) -> dict:
        before, _ = self.query(cmd)
        ack, ms = self.send(build(self.next_seq()), "ACK", bsb_rf.reply_cmd(cmd))
        time.sleep(0.4)
        after, _ = self.query(cmd)
        return {"ok": bool(ack) and after is not None, "ack": bool(ack), "ack_ms": ms,
                "before": value_of(before), "after": value_of(after)}

    def read_info(self) -> dict:
        out = {}
        for key, hexcmd, kind in bsb_rf.INFO_PARAMS:
            rec, _ = self.query(bytes.fromhex(hexcmd))
            if rec:
                out[key] = {"value": bsb_rf.decode_info(kind, rec.get("payload")), "raw": rec.get("payload"),
                            "at": rec["t"]}
            time.sleep(0.15)
        return out


def value_of(rec):
    if not rec:
        return None
    if "temp_c" in rec:
        return rec["temp_c"]
    if rec.get("dhw") in ("on", "off"):
        return rec["dhw"] == "on"
    return rec.get("payload")


def check_setpoint(temp_c, lo: float, hi: float) -> float:
    t = float(temp_c)
    lo, hi = max(lo, HARD_MIN_C), min(hi, HARD_MAX_C)
    if not lo <= t <= hi or (t * 2) % 1:
        raise ValueError(f"teplota musí být {lo:g}–{hi:g} °C po 0,5")
    return t


def set_temp(gw: Gateway, t: float) -> dict:
    return gw.set(bsb_rf.CMD_ROOM_SETPOINT, lambda s: bsb_rf.build_set_temperature(s, gw.header, t))


def set_dhw(gw: Gateway, on: bool) -> dict:
    return gw.set(bsb_rf.CMD_DHW_MODE, lambda s: bsb_rf.build_set_dhw(s, gw.header, on))


def set_heating_mode(gw: Gateway, mode: int) -> dict:
    if mode not in (0, 1, 2, 3):
        raise ValueError("režim musí být 0–3")
    r = gw.set(bsb_rf.CMD_HEATING_MODE, lambda s: bsb_rf.build_set_enum(s, gw.header, bsb_rf.CMD_HEATING_MODE, mode))
    for k in ("before", "after"):
        if isinstance(r.get(k), str):
            r[k] = bsb_rf.decode_info("enum", r[k])
    return r


# --------------------------------------------------------------------------- démon

class Daemon:
    def __init__(self):
        self.token = os.environ.get("KOTEL_RF_TOKEN", "")
        self.min_c = env_float("KOTEL_RF_MIN_C", 14)
        self.max_c = env_float("KOTEL_RF_MAX_C", 25)
        self.allow_raw = os.environ.get("KOTEL_RF_ALLOW_RAW_SET", "0") == "1"
        self.info_interval = int(env_float("KOTEL_RF_INFO_INTERVAL", 300))
        log_dir = os.environ.get("KOTEL_RF_LOG_DIR", "")
        self.log_dir = log_dir
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        self.state = {"room_setpoint_c": None, "dhw_on": None, "controller_room_temp_c": None,
                      "last_controller_frame_at": None, "last_boiler_frame_at": None, "last_command": None,
                      "info": {}, "info_at": None, "burner_on": None, "started_at": time.time()}
        self._pending = None
        self.stop = threading.Event()
        self.gw = Gateway(on_frame=self.absorb)

    def absorb(self, rec: dict):
        if self.log_dir:
            with open(os.path.join(self.log_dir, time.strftime("%Y-%m-%d") + ".jsonl"), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec.get("crc_ok"):
            return
        now, s = rec["t"], self.state
        if rec.get("role") == "controller":
            s["last_controller_frame_at"] = now
            if rec.get("cmd") == bsb_rf.CMD_CONTROLLER_ROOM_TEMP and len(rec.get("payload", "")) >= 4:
                s["controller_room_temp_c"] = int(rec["payload"][:4], 16) / 64
            self._pending = rec if rec.get("type") == "SET" else None
        elif rec.get("role") == "boiler":
            s["last_boiler_frame_at"] = now
            if rec.get("type") == "ANS" and "temp_c" in rec and rec["cmd"] == bsb_rf.reply_cmd(bsb_rf.CMD_ROOM_SETPOINT):
                s["room_setpoint_c"] = rec["temp_c"]
            elif rec.get("type") == "ANS" and rec.get("dhw") in ("on", "off"):
                s["dhw_on"] = rec["dhw"] == "on"
            elif rec.get("type") == "ACK" and self._pending:   # změna z originálního ovladače
                p, self._pending = self._pending, None
                if "temp_c" in p:
                    s["room_setpoint_c"] = p["temp_c"]
                elif p.get("dhw") in ("on", "off"):
                    s["dhw_on"] = p["dhw"] == "on"

    def rx_thread(self):
        while not self.stop.is_set():
            with self.gw.lock:
                self.gw.rxr.receive_one(0.25)
            time.sleep(0.01)   # Lock není férový, bez mezery by API čekalo

    def info_thread(self):
        self.stop.wait(20)
        while not self.stop.is_set():
            try:
                self.refresh_info()
            except Exception as exc:   # noqa: BLE001  čtení musí běžet dál
                print("čtení hodnot selhalo:", repr(exc), flush=True)
            self.stop.wait(self.info_interval)

    def refresh_info(self):
        with self.gw.lock:
            if self.gw.rxr.controller_header is None:
                return
            info = {**self.state["info"], **self.gw.read_info()}
        burner = (info.get("burner_status") or {}).get("value")
        self.state.update(info=info, info_at=time.time(),
                          burner_on=(burner in bsb_rf.BURNER_ON_CODES) if burner is not None else None)

    def command(self, kind: str, fn, value) -> tuple[int, dict]:
        with self.gw.lock:
            if self.gw.rxr.controller_header is None:
                return 409, {"error": "zatím nezachycen rámec ovladače QAA78, nic se neodešle"}
            r = fn(self.gw, value)
        self.state["last_command"] = {"kind": kind, "value": value, "at": time.time(), **r}
        return 200, {**r, "state": self.state}

    def handle(self, path: str, body: dict) -> tuple[int, dict]:
        if path == "/setpoint":
            return self.command("setpoint", set_temp, check_setpoint(body.get("temp_c"), self.min_c, self.max_c))
        if path == "/dhw":
            return self.command("dhw", set_dhw, bool(body.get("on")))
        if path == "/heating":
            return self.command("heating", set_heating_mode, int(body.get("mode", -1)))
        if path == "/query":
            what = str(body.get("what", ""))
            cmd = {"temp": bsb_rf.CMD_ROOM_SETPOINT, "dhw": bsb_rf.CMD_DHW_MODE}.get(what) or bytes.fromhex(what)
            if len(cmd) != 4:
                raise ValueError("what: temp, dhw nebo 8 hex číslic")
            with self.gw.lock:
                rec, ms = self.gw.query(cmd)
            return 200, {"ok": rec is not None, "value": value_of(rec), "payload": rec and rec.get("payload"),
                         "reply_ms": ms}
        if path == "/set":
            if not self.allow_raw:
                return 403, {"error": "obecný zápis je vypnutý (KOTEL_RF_ALLOW_RAW_SET=1)"}
            cmd, payload = bytes.fromhex(str(body.get("cmd", ""))), bytes.fromhex(str(body.get("payload", "")))
            if len(cmd) != 4 or not 1 <= len(payload) <= 16:
                raise ValueError("cmd 8 hex číslic, payload 1–16 bajtů hex")
            return self.command("set", lambda gw, _v: gw.set(
                cmd, lambda s: bsb_rf.build_frame(s, gw.header, bsb_rf.T_SET, cmd, payload)), f"{cmd.hex()}:{payload.hex()}")
        if path == "/info":
            self.refresh_info()
            return 200, {"state": self.state}
        return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    daemon: Daemon

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        import hmac
        got = self.headers.get("Authorization", "")
        return bool(self.daemon.token) and hmac.compare_digest(got, f"Bearer {self.daemon.token}")

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/state":
            return self._send(200, {"state": self.daemon.state, "now": time.time()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            code, obj = self.daemon.handle(self.path, body)
        except (ValueError, TypeError) as exc:
            code, obj = 400, {"error": str(exc)}
        except Exception as exc:   # noqa: BLE001  démon musí běžet dál
            code, obj = 500, {"error": repr(exc)}
        self._send(code, obj)

    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), self.address_string(), fmt % args, flush=True)


def serve():
    d = Daemon()
    if not d.token:
        raise SystemExit("chybí KOTEL_RF_TOKEN")
    Handler.daemon = d
    threading.Thread(target=d.rx_thread, daemon=True).start()
    if d.info_interval > 0:
        threading.Thread(target=d.info_thread, daemon=True).start()
    bind = os.environ.get("KOTEL_RF_BIND", "127.0.0.1")
    port = int(os.environ.get("KOTEL_RF_PORT", "8765"))
    srv = ThreadingHTTPServer((bind, port), Handler)

    def shutdown(*_):
        d.stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"kotel-rf poslouchá na {bind}:{port}", flush=True)
    srv.serve_forever()
    with d.gw.lock:
        d.gw.close()


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kotel přes rádio (Siemens QAA78/AVS71, CC1101).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("rx"); p.add_argument("--log")
    p = sub.add_parser("query"); p.add_argument("what")
    sub.add_parser("info")
    p = sub.add_parser("set-temp"); p.add_argument("temp", type=float); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("set-dhw"); p.add_argument("state", choices=["on", "off"]); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("heating-mode"); p.add_argument("mode", type=int, choices=[0, 1, 2, 3]); p.add_argument("--yes", action="store_true")
    sub.add_parser("serve")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        serve(); return 0
    if a.cmd == "check":
        from cc1101 import CC1101
        r = CC1101()
        part, ver = r.identify()
        r.close()
        print(f"PARTNUM 0x{part:02x}, VERSION 0x{ver:02x}")
        ok = part == 0x00 and ver in (0x04, 0x14)
        print("CC1101 v pořádku" if ok else "CC1101 neodpovídá: zkontrolujte napájení 3,3 V, MISO/MOSI/SCLK/CSn")
        return 0 if ok else 1
    if a.cmd in ("set-temp", "set-dhw", "heating-mode") and not a.yes:
        print("Zápis do kotle: přečtěte si upozornění v README a spusťte znovu s --yes.", file=sys.stderr)
        return 2
    if a.cmd == "set-temp":
        check_setpoint(a.temp, HARD_MIN_C, HARD_MAX_C)

    printer = (lambda rec: print(json.dumps(rec, ensure_ascii=False), flush=True))
    gw = Gateway(getattr(a, "log", None), on_frame=printer)
    try:
        if a.cmd == "rx":
            stop = []
            signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
            print(f"poslouchám, šum {gw.radio.rssi_dbm()} dBm (Ctrl+C konec)", file=sys.stderr)
            try:
                while not stop:
                    gw.rxr.receive_one(1)
            except KeyboardInterrupt:
                pass
            return 0
        print("čekám na rámec ovladače QAA78 (bez něj se nic neodešle)...", file=sys.stderr)
        if not gw.wait_for_controller():
            print("rámec ovladače nezachycen, nic neodesláno", file=sys.stderr)
            return 1
        with gw.lock:
            if a.cmd == "query":
                cmd = {"temp": bsb_rf.CMD_ROOM_SETPOINT, "dhw": bsb_rf.CMD_DHW_MODE}.get(a.what) or bytes.fromhex(a.what)
                rec, ms = gw.query(cmd)
                result = {"ok": rec is not None, "value": value_of(rec), "reply_ms": ms}
            elif a.cmd == "info":
                result = gw.read_info()
            elif a.cmd == "set-temp":
                result = set_temp(gw, a.temp)
            elif a.cmd == "set-dhw":
                result = set_dhw(gw, a.state == "on")
            else:
                result = set_heating_mode(gw, a.mode)
        print(json.dumps({"result": result}, ensure_ascii=False, indent=1))
        return 0 if result else 1
    finally:
        gw.close()


if __name__ == "__main__":
    sys.exit(main())
