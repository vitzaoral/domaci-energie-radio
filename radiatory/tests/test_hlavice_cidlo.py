"""Testy rozhodování bez MQTT: python3 -m unittest discover -s tests -v"""
import configparser
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hlavice_cidlo as hc  # noqa: E402

NOW = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)


def cfg(**over):
    c = configparser.ConfigParser()
    c.read_dict(hc.DEFAULTS)
    for k, v in over.items():
        c["nastaveni"][k] = str(v)
    return hc.Settings(c["nastaveni"])


def trv(**over):
    d = {"occupied_heating_setpoint": 20.0, "system_mode": "heat", "running_state": "idle",
         "temperature_sensor_select": "remote_temperature", "temperature_accuracy": -0.2,
         "external_temperature_input": 19.8, "last_seen": NOW.isoformat()}
    d.update(over)
    return d


def room(t=19.8, age_min=10):
    return {"temperature_c": t, "measured_at": NOW - timedelta(minutes=age_min)}


class ExternalTest(unittest.TestCase):
    def test_sends_room_temperature_and_switches_to_remote(self):
        entry = {}
        p = hc.plan_external(trv(temperature_sensor_select="local_temperature", external_temperature_input=None),
                             room(19.84), entry, NOW, cfg())
        self.assertEqual(p, {"external_temperature_input": 19.8, "temperature_sensor_select": "remote_temperature"})

    def test_no_resend_when_unchanged_then_refresh(self):
        entry = {}
        hc.plan_external(trv(), room(), entry, NOW, cfg())
        self.assertEqual(hc.plan_external(trv(), room(), entry, NOW + timedelta(minutes=5), cfg()), {})
        later = NOW + timedelta(minutes=21)
        self.assertEqual(hc.plan_external(trv(), room(age_min=-11), entry, later, cfg()),
                         {"external_temperature_input": 19.8})

    def test_stale_sensor_falls_back_to_local(self):
        entry = {}
        p = hc.plan_external(trv(), room(age_min=80), entry, NOW, cfg())
        self.assertEqual(p, {"temperature_sensor_select": "local_temperature"})
        self.assertIsNone(entry["room_c"])

    def test_accuracy_kept(self):
        p = hc.plan_external(trv(temperature_accuracy=-1), room(), {}, NOW, cfg())
        self.assertEqual(p.get("temperature_accuracy"), -0.2)
        self.assertNotIn("temperature_accuracy", hc.plan_external(trv(temperature_accuracy=-1), room(), {}, NOW,
                                                                  cfg(accuracy="")))


class WakeTest(unittest.TestCase):
    def setUp(self):
        self.entry = {}
        hc.plan_external(trv(), room(19.8), self.entry, NOW, cfg())

    def test_pulse_when_idle_below_threshold(self):
        self.assertEqual(hc.plan_wake(trv(), self.entry, NOW, cfg()), 18.8)

    def test_no_pulse(self):
        self.assertIsNone(hc.plan_wake(trv(running_state="heat"), self.entry, NOW, cfg()))
        self.assertIsNone(hc.plan_wake(trv(), self.entry, NOW, cfg(wake_pulse="false")))
        warm = {}
        hc.plan_external(trv(), room(19.9), warm, NOW, cfg())
        self.assertIsNone(hc.plan_wake(trv(), warm, NOW, cfg()))       # nad prahem cíl − 0,2
        cold = {}
        hc.plan_external(trv(), room(19.0), cold, NOW, cfg())
        self.assertIsNone(hc.plan_wake(trv(), cold, NOW, cfg()))       # pod cíl − 1 otevře sama

    def test_retry_limit(self):
        self.entry["wake_at"] = NOW - timedelta(minutes=5)
        self.assertIsNone(hc.plan_wake(trv(), self.entry, NOW, cfg()))


class PingTest(unittest.TestCase):
    def test_ping_after_silence(self):
        quiet = trv(last_seen=(NOW - timedelta(minutes=25)).isoformat())
        entry = {}
        self.assertTrue(hc.wants_ping(quiet, entry, NOW, cfg()))
        entry["ping_at"] = NOW
        self.assertFalse(hc.wants_ping(quiet, entry, NOW + timedelta(minutes=5), cfg()))
        self.assertFalse(hc.wants_ping(trv(), {}, NOW, cfg()))


if __name__ == "__main__":
    unittest.main()
