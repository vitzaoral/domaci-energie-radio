#!/usr/bin/env python3
"""hlavice-cidlo: hlavice Sonoff TRVZB řídí podle pokojového čidla, ne podle teploty u radiátoru.

Běží vedle zigbee2mqtt a přes MQTT:
1. posílá hlavici teplotu z pokojového čidla (`external_temperature_input`)
   a přepne ji na `temperature_sensor_select = remote_temperature`, dokud je
   měření čidla čerstvé; když čidlo mlčí, vrátí hlavici na vlastní čidlo,
2. drží menší hysterezi (`temperature_accuracy`, výchozí -0,2 °C místo -1),
3. volitelně obchází chybu firmwaru 1.3.0, který hysterezi ignoruje a otevírá
   až 1 °C pod cílem: krátký „probouzecí“ pulz nižší teploty, hned se vrátí skutečná,
4. zeptá se hlavice na teplotu, když dlouho mlčí.

Drives Sonoff TRVZB valves from a room sensor via zigbee2mqtt (MQTT).
Requires paho-mqtt (apt install python3-paho-mqtt, or pip install paho-mqtt).
"""
from __future__ import annotations

import argparse
import configparser
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

SELECT_REMOTE = "remote_temperature"
SELECT_LOCAL = "local_temperature"

DEFAULTS = {
    "mqtt": {"host": "127.0.0.1", "port": "1883", "username": "", "password": "", "base_topic": "zigbee2mqtt",
             "client_id": "hlavice-cidlo"},
    "nastaveni": {
        "room_fresh_minutes": "75",     # čidlo Aqara/Xiaomi posílá nejpozději po 60 min
        "refresh_minutes": "20",        # i beze změny, ať hlavice ví, že čidlo žije
        "min_delta": "0.1",
        "accuracy": "-0.2",             # prázdné = hysterezi neměnit
        "wake_pulse": "true",
        "wake_below": "1.2",
        "wake_retry_minutes": "10",
        "wake_wait_seconds": "15",
        "ping_minutes": "20",
        "retry_minutes": "5",
        "interval_seconds": "60",
    },
}


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class Settings:
    def __init__(self, s: configparser.SectionProxy):
        self.room_fresh = timedelta(minutes=s.getfloat("room_fresh_minutes"))
        self.refresh = timedelta(minutes=s.getfloat("refresh_minutes"))
        self.min_delta = s.getfloat("min_delta")
        self.accuracy = number(s.get("accuracy") or None)
        self.wake_pulse = s.getboolean("wake_pulse")
        self.wake_below = s.getfloat("wake_below")
        self.wake_retry = timedelta(minutes=s.getfloat("wake_retry_minutes"))
        self.wake_wait = s.getfloat("wake_wait_seconds")
        self.ping = timedelta(minutes=s.getfloat("ping_minutes"))
        self.retry = timedelta(minutes=s.getfloat("retry_minutes"))
        self.interval = s.getfloat("interval_seconds")


# --------------------------------------------------------------------------- rozhodování (čisté funkce)

def plan_external(trv: dict, room: dict | None, entry: dict, now: datetime, cfg: Settings) -> dict:
    """Co poslat hlavici. Mění `entry` (paměť pro danou hlavici), vrací payload pro .../set."""
    temp = room.get("temperature_c") if room else None
    measured = room.get("measured_at") if room else None
    fresh = temp is not None and measured is not None and now - measured <= cfg.room_fresh and -20 < temp < 60
    entry["room_c"] = temp if fresh else None
    payload: dict = {}
    if fresh:
        want = SELECT_REMOTE
        value = round(temp, 1)
        sent_c, sent_at = entry.get("sent_c"), entry.get("sent_at")
        in_head = number(trv.get("external_temperature_input"))
        eps = cfg.min_delta - 1e-9
        if (sent_c is None or abs(sent_c - value) >= eps or sent_at is None or now - sent_at >= cfg.refresh
                or (in_head is not None and abs(in_head - value) >= eps)):
            payload["external_temperature_input"] = value
            entry["sent_c"], entry["sent_at"] = value, now
    else:
        want = SELECT_LOCAL
    if trv.get("temperature_sensor_select") != want:
        last = entry.get("select_sent_at")
        if entry.get("want") != want or last is None or now - last >= cfg.retry:
            payload["temperature_sensor_select"] = want
            entry["select_sent_at"] = now
    entry["want"] = want
    if cfg.accuracy is not None and "temperature_accuracy" in trv:   # starší firmware klíč nemá
        reported = number(trv.get("temperature_accuracy"))
        if reported is None or abs(reported - cfg.accuracy) >= 0.01:
            last = entry.get("accuracy_sent_at")
            if last is None or now - last >= cfg.retry:
                payload["temperature_accuracy"] = cfg.accuracy
                entry["accuracy_sent_at"] = now
    return payload


def plan_wake(trv: dict, entry: dict, now: datetime, cfg: Settings) -> float | None:
    """Teplota probouzecího pulzu, nebo None.

    Firmware 1.3.0 otevírá ventil až pod cíl − 1 °C bez ohledu na nastavenou
    hysterezi. Když hlavice stojí (idle) a místnost je už na prahu cíl + accuracy
    nebo níž (ale ne pod cíl − 1, tam otevře sama), pošle se na chvíli teplota
    cíl − wake_below. Hlavice začne topit a dál topí do cíle jako normálně."""
    room = number(entry.get("room_c"))
    target = number(trv.get("occupied_heating_setpoint"))
    if (not cfg.wake_pulse or cfg.accuracy is None or room is None or target is None
            or entry.get("want") != SELECT_REMOTE or trv.get("temperature_sensor_select") != SELECT_REMOTE
            or trv.get("system_mode") != "heat" or trv.get("running_state") != "idle"):
        return None
    room = round(room, 1)
    if room > target + cfg.accuracy + 1e-9 or room <= target - 1 + 1e-9:
        return None
    last = entry.get("wake_at")
    if last is not None and now - last < cfg.wake_retry:
        return None
    return round(max(5.0, target - cfg.wake_below), 1)


def wants_ping(trv: dict, entry: dict, now: datetime, cfg: Settings) -> bool:
    seen = parse_ts(trv.get("last_seen"))
    if seen is None or now - seen < cfg.ping:
        return False
    last = entry.get("ping_at")
    return last is None or now - last >= cfg.ping


# --------------------------------------------------------------------------- MQTT

class Bridge:
    def __init__(self, conf: configparser.ConfigParser, pairs: dict[str, str]):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            sys.exit("chybí paho-mqtt: sudo apt install python3-paho-mqtt (nebo pip install paho-mqtt)")
        m = conf["mqtt"]
        self.base = m["base_topic"].rstrip("/")
        self.pairs = pairs
        self.cfg = Settings(conf["nastaveni"])
        self.data: dict[str, dict] = {}
        self.measured: dict[str, datetime] = {}
        self.entries: dict[str, dict] = {trv: {} for trv in pairs}
        self.heat_events: dict[str, threading.Event] = {trv: threading.Event() for trv in pairs}
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=m["client_id"])
        except AttributeError:   # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=m["client_id"])
        if m["username"]:
            self.client.username_pw_set(m["username"], m["password"] or None)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.connect(m["host"], m.getint("port"), keepalive=60)

    def on_connect(self, client, userdata, flags, reason, properties=None):
        names = set(self.pairs) | set(self.pairs.values())
        for name in names:
            client.subscribe(f"{self.base}/{name}")
        log(f"připojeno k MQTT, sleduji {len(names)} zařízení")

    def on_message(self, client, userdata, msg):
        name = msg.topic[len(self.base) + 1:]
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        self.data[name] = {**self.data.get(name, {}), **data}
        seen = parse_ts(data.get("last_seen"))
        if seen is None and not msg.retain:
            seen = datetime.now(timezone.utc)
        if seen is not None and "temperature" in data:
            self.measured[name] = seen
        if name in self.heat_events and not msg.retain and data.get("running_state") == "heat":
            self.heat_events[name].set()

    def publish(self, name: str, payload: dict, suffix: str = "set"):
        self.client.publish(f"{self.base}/{name}/{suffix}", json.dumps(payload))

    def room(self, sensor: str) -> dict | None:
        d = self.data.get(sensor)
        if not d:
            return None
        return {"temperature_c": number(d.get("temperature")), "measured_at": self.measured.get(sensor)}

    def wake(self, trv: str, pulse: float, real: float, entry: dict, now: datetime):
        entry["wake_at"] = now
        ev = self.heat_events[trv]
        ev.clear()
        try:
            self.publish(trv, {"external_temperature_input": pulse})
            heating = ev.wait(self.cfg.wake_wait)
        finally:
            self.publish(trv, {"external_temperature_input": real})
            entry["sent_c"], entry["sent_at"] = real, now
        log(f"{trv}: probouzecí pulz {pulse} °C, {'topí' if heating else 'nezačala topit'}")

    def tick(self):
        now = datetime.now(timezone.utc)
        for trv, sensor in self.pairs.items():
            data = self.data.get(trv)
            if not data:
                continue
            entry = self.entries[trv]
            payload = plan_external(data, self.room(sensor), entry, now, self.cfg)
            if payload:
                self.publish(trv, payload)
                log(f"{trv}: {payload}")
            pulse = plan_wake(data, entry, now, self.cfg)
            if pulse is not None:
                self.wake(trv, pulse, round(entry["room_c"], 1), entry, now)
            if wants_ping(data, entry, now, self.cfg):
                entry["ping_at"] = now
                self.publish(trv, {"local_temperature": ""}, "get")

    def run(self):
        self.client.loop_start()
        time.sleep(3)   # retained stavy
        try:
            while True:
                self.tick()
                time.sleep(self.cfg.interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.client.loop_stop()


def load_config(path: str) -> tuple[configparser.ConfigParser, dict[str, str]]:
    conf = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    conf.optionxform = str
    conf.read_dict(DEFAULTS)
    with open(path, encoding="utf-8") as f:
        conf.read_file(f)
    pairs = dict(conf.items("hlavice")) if conf.has_section("hlavice") else {}
    return conf, pairs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hlavice Sonoff TRVZB podle pokojového čidla (zigbee2mqtt).")
    ap.add_argument("-c", "--config", required=True)
    a = ap.parse_args(argv)
    conf, pairs = load_config(a.config)
    if not pairs:
        sys.exit("v [hlavice] není žádná dvojice hlavice = čidlo")
    Bridge(conf, pairs).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
