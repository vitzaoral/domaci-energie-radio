#!/usr/bin/env python3
"""vodomer-radio: odečet vodoměru s rádiovým modulem Apator Metra E-RM 30 přes RTL-SDR.

Spustí rtl_433 (dekodér 277), z jeho JSON výstupu vybere rámce nastavených
vodoměrů a stav pošle na výstupy: stdout (text nebo JSON řádky), HTTP POST
na libovolnou adresu, MQTT. Jen standardní knihovna Pythonu 3.9+.

Reads an Apator Metra E-RM 30 water meter radio module with an RTL-SDR dongle
via rtl_433 and publishes the reading to stdout, HTTP POST or MQTT.
"""
from __future__ import annotations

import argparse
import collections
import configparser
import json
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Iterable, Iterator

__version__ = "1.0.0"
MODEL = "ApatorMetra-ERM30"

DEFAULTS = {
    "radio": {
        "rtl_433": "rtl_433",
        "frequency": "868.9M",
        "sample_rate": "1000k",
        "gain": "0",              # 0 = automatický zisk
        "device": "",            # např. "0" nebo ":serial", prázdné = první dongle
        "extra_args": "",
    },
    "output": {
        "stdout": "text",         # text | json | none
        "emit": "changes",        # changes | all
        "heartbeat_minutes": "60",
    },
    "http": {"url": "", "token": "", "timeout": "10"},
    "mqtt": {
        "host": "", "port": "1883", "username": "", "password": "",
        "topic": "vodomer/{id}", "retain": "true", "client_id": "vodomer-radio",
    },
}


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- konfigurace

def load_config(path: str | None) -> configparser.ConfigParser:
    conf = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    conf.optionxform = str  # ID měřidel nechat tak, jak jsou
    conf.read_dict(DEFAULTS)
    if not conf.has_section("meters"):
        conf.add_section("meters")
    if path:
        with open(path, encoding="utf-8") as f:
            conf.read_file(f)
    return conf


def meters_from(conf: configparser.ConfigParser) -> dict[str, str]:
    """ID rádiového modulu -> název. Prázdné = všechny zachycené (jen pro hledání)."""
    return {normalize_id(k): (v or k) for k, v in conf.items("meters")}


def normalize_id(raw: object) -> str:
    """rtl_433 vypisuje ID na 9 číslic s nulami zleva, uživatel ho může psát bez nich."""
    s = str(raw).strip()
    return s.zfill(9) if s.isdigit() else s


def rtl433_command(conf: configparser.ConfigParser) -> list[str]:
    r = conf["radio"]
    cmd = [r["rtl_433"]]
    if r["device"]:
        cmd += ["-d", r["device"]]
    cmd += [
        "-f", r["frequency"], "-s", r["sample_rate"], "-g", r["gain"],
        "-Y", "minmax",             # FSK demodulátor doporučený autorem dekodéru
        "-R", "277",                # jen Apator Metra E-RM 30
        "-M", "time:iso:tz", "-M", "level",
        "-F", "json",
    ]
    cmd += shlex.split(r["extra_args"])
    return cmd


# --------------------------------------------------------------------------- dekódování

def parse_line(line: str, meters: dict[str, str] | None = None) -> dict | None:
    """Jeden JSON řádek z rtl_433 -> odečet, nebo None (cizí model, cizí ID, neúplné)."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("model") != MODEL:
        return None
    rid = normalize_id(d.get("id", ""))
    if not rid.isdigit():
        return None                  # ID se nepodařilo dešifrovat
    if meters and rid not in meters:
        return None
    try:
        volume_m3 = float(d.get("volume_m3"))
    except (TypeError, ValueError):
        return None                  # "?????.???" = neúplně dešifrovaný stav
    reading = {
        "meter_id": rid,
        "name": (meters or {}).get(rid, rid),
        "volume_m3": round(volume_m3, 3),
        "volume_l": int(round(volume_m3 * 1000)),
        "meter_date": d.get("date"),
        "time": d.get("time") or datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    for key in ("rssi", "snr", "noise"):
        if isinstance(d.get(key), (int, float)):
            reading[key] = round(float(d[key]), 1)
    return reading


class Emitter:
    """Rozhodne, které odečty poslat: všechny, nebo jen změny + pravidelné potvrzení."""

    def __init__(self, mode: str = "changes", heartbeat_minutes: float = 60):
        self.mode = mode
        self.heartbeat = heartbeat_minutes * 60
        self.last: dict[str, tuple[int, float]] = {}

    def should_emit(self, reading: dict, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        prev = self.last.get(reading["meter_id"])
        if self.mode == "all" or prev is None or prev[0] != reading["volume_l"] \
                or (self.heartbeat > 0 and now - prev[1] >= self.heartbeat):
            self.last[reading["meter_id"]] = (reading["volume_l"], now)
            return True
        return False


# --------------------------------------------------------------------------- výstupy

def format_text(r: dict) -> str:
    signal = f"  rssi {r['rssi']} dB" if "rssi" in r else ""
    label = f"{r['name']} ({r['meter_id']})" if r["name"] != r["meter_id"] else r["meter_id"]
    return f"{r['time']}  {label}  {r['volume_m3']:.3f} m3{signal}"


class HttpOutput:
    """POST JSON odečtu na libovolnou adresu. Neodeslané drží ve frontě a zkusí znovu."""

    def __init__(self, url: str, token: str = "", timeout: float = 10, queue_max: int = 1000):
        self.url, self.token, self.timeout = url, token, timeout
        self.queue: collections.deque[dict] = collections.deque(maxlen=queue_max)
        self.retry_at = 0.0

    def send(self, reading: dict | None) -> None:
        if reading is not None:
            self.queue.append(reading)
        if time.monotonic() < self.retry_at:
            return
        while self.queue:
            try:
                self._post(self.queue[0])
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"HTTP: odeslání selhalo ({exc}), ve frontě {len(self.queue)}, další pokus za 60 s")
                self.retry_at = time.monotonic() + 60
                return
            self.queue.popleft()

    def _post(self, reading: dict) -> None:
        headers = {"Content-Type": "application/json", "User-Agent": f"vodomer-radio/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=json.dumps(reading).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


def _mqtt_str(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def _mqtt_len(n: int) -> bytes:
    out = bytearray()
    while True:
        n, digit = divmod(n, 128)
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def mqtt_connect_packet(client_id: str, username: str = "", password: str = "", keepalive: int = 30) -> bytes:
    flags = 0x02 | (0x80 if username else 0) | (0x40 if username and password else 0)
    body = _mqtt_str("MQTT") + bytes([4, flags]) + keepalive.to_bytes(2, "big") + _mqtt_str(client_id)
    if username:
        body += _mqtt_str(username)
        if password:
            body += _mqtt_str(password)
    return bytes([0x10]) + _mqtt_len(len(body)) + body


def mqtt_publish_packet(topic: str, payload: bytes, retain: bool = True) -> bytes:
    body = _mqtt_str(topic) + payload        # QoS 0, bez identifikátoru zprávy
    return bytes([0x30 | (1 if retain else 0)]) + _mqtt_len(len(body)) + body


class MqttOutput:
    """Minimální MQTT 3.1.1 klient: připojit, publikovat (QoS 0), odpojit. Bez závislostí."""

    def __init__(self, host: str, port: int = 1883, username: str = "", password: str = "",
                 topic: str = "vodomer/{id}", retain: bool = True, client_id: str = "vodomer-radio"):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.topic, self.retain, self.client_id = topic, retain, client_id

    def send(self, reading: dict | None) -> None:
        if reading is None:
            return
        topic = self.topic.format(id=reading["meter_id"], name=reading["name"])
        try:
            with socket.create_connection((self.host, self.port), timeout=10) as s:
                s.sendall(mqtt_connect_packet(self.client_id, self.username, self.password))
                ack = s.recv(4)
                if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
                    raise OSError(f"broker odmítl spojení (CONNACK {ack.hex()})")
                s.sendall(mqtt_publish_packet(topic, json.dumps(reading).encode(), self.retain))
                s.sendall(b"\xe0\x00")       # DISCONNECT
        except OSError as exc:
            log(f"MQTT: publikování selhalo ({exc})")


def build_outputs(conf: configparser.ConfigParser) -> list:
    outs: list = []
    if conf["http"]["url"]:
        h = conf["http"]
        outs.append(HttpOutput(h["url"], h["token"], h.getfloat("timeout")))
    if conf["mqtt"]["host"]:
        m = conf["mqtt"]
        outs.append(MqttOutput(m["host"], m.getint("port"), m["username"], m["password"],
                               m["topic"], m.getboolean("retain"), m["client_id"]))
    return outs


# --------------------------------------------------------------------------- běh

def run(lines: Iterable[str], conf: configparser.ConfigParser, out=sys.stdout) -> int:
    meters = meters_from(conf)
    emitter = Emitter(conf["output"]["emit"], conf["output"].getfloat("heartbeat_minutes"))
    outputs = build_outputs(conf)
    style = conf["output"]["stdout"]
    count = 0
    for line in lines:
        reading = parse_line(line, meters)
        if reading is None or not emitter.should_emit(reading):
            for o in outputs:
                if isinstance(o, HttpOutput) and o.queue:
                    o.send(None)    # dopsat frontu i bez nového odečtu
            continue
        count += 1
        if style == "json":
            print(json.dumps(reading, ensure_ascii=False), file=out, flush=True)
        elif style == "text":
            print(format_text(reading), file=out, flush=True)
        for o in outputs:
            o.send(reading)
    return count


def list_meters(lines: Iterable[str], out=sys.stdout) -> None:
    """Režim hledání: vypíše všechna zachycená ID s počtem rámců a úrovní signálu."""
    seen: dict[str, dict] = {}
    try:
        for line in lines:
            r = parse_line(line)
            if r is None:
                continue
            e = seen.setdefault(r["meter_id"], {"n": 0, "rssi": None})
            e["n"] += 1
            e["rssi"] = r.get("rssi", e["rssi"])
            print(f"{r['time']}  zachycen modul {r['meter_id']}  rssi {r.get('rssi', '?')} dB", file=out, flush=True)
    except KeyboardInterrupt:
        pass
    print("\nModul            rámců  poslední rssi", file=out)
    for rid, e in sorted(seen.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{rid:<16} {e['n']:>5}  {e['rssi'] if e['rssi'] is not None else '?'}", file=out)
    print("Svůj modul poznáte podle ID natištěného na rádiovém modulu (ne na vodoměru).", file=out)


def rtl433_lines(cmd: list[str]) -> Iterator[str]:
    log("spouštím: " + " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        log(f"rtl_433 nenalezen ({cmd[0]}), nainstalujte verzi 25.12 nebo novější")
        sys.exit(127)
    assert proc.stdout is not None
    try:
        yield from proc.stdout
    finally:
        proc.terminate()
    rc = proc.wait()
    log(f"rtl_433 skončil (kód {rc})")
    sys.exit(rc or 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Odečet vodoměru Apator Metra E-RM 30 přes RTL-SDR a rtl_433.")
    ap.add_argument("-c", "--config", help="konfigurační soubor INI (viz config.example.ini)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--stdin", action="store_true", help="číst JSON řádky rtl_433 ze vstupu místo spuštění rtl_433")
    src.add_argument("--file", help="číst JSON řádky rtl_433 ze souboru (zkouška nanečisto)")
    ap.add_argument("--list", action="store_true", help="vypsat všechny zachycené moduly E-RM 30 (hledání vlastního ID)")
    ap.add_argument("--json", action="store_true", help="na stdout JSON řádky (přebije konfiguraci)")
    ap.add_argument("--print-command", action="store_true", help="jen vypsat příkaz rtl_433 a skončit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    conf = load_config(args.config)
    if args.json:
        conf["output"]["stdout"] = "json"
    if args.print_command:
        print(" ".join(shlex.quote(c) for c in rtl433_command(conf)))
        return 0

    if args.stdin:
        lines: Iterable[str] = sys.stdin
    elif args.file:
        lines = open(args.file, encoding="utf-8")
    else:
        lines = rtl433_lines(rtl433_command(conf))

    if args.list:
        list_meters(lines)
        return 0
    if not meters_from(conf):
        log("upozornění: v [meters] není žádné ID, vypisuji všechny zachycené vodoměry (i sousedů)")
    try:
        run(lines, conf)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
