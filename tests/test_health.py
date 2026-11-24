"""Self-healing laptop: health report, sleep detection, disk warning, restart count, Wake-on-LAN target."""
import socket
from collections import namedtuple
from datetime import date, datetime

import pytest

from arya.core import health as H

Addr = namedtuple("Addr", "family address netmask")


def _snap(**kw):
    s = {"uptime_s": 3 * 3600, "model": True, "ram_gb": 2.0, "disk_gb": 20.0, "restarts_24h": 0}
    s.update(kw)
    return s


def test_all_good_report():
    text = H.report(_snap())
    assert text.startswith("All good") and "3 hours" in text and "20.0 GB" in text


@pytest.mark.parametrize("kw, words", [
    ({"model": False}, "model isn't answering"),
    ({"disk_gb": 0.9}, "disk is nearly full"),
    ({"ram_gb": 0.3}, "memory is tight"),
    ({"restarts_24h": 2}, "restart myself 2 times"),
])
def test_problems_come_first(kw, words):
    text = H.report(_snap(**kw))
    assert text.startswith("Mostly fine") and words in text


@pytest.mark.parametrize("text", ["health check", "Run diagnostics", "system check please", "ARYA status",
                                  "how's the laptop holding up?", "diagnose yourself", "self-check"])
def test_asks(text):
    assert H.matches(text)


@pytest.mark.parametrize("text", ["how are you", "check my health insurance", "what's the status of my order",
                                  "how is the weather"])
def test_not_asks(text):
    assert not H.matches(text)


def test_sleep_detected_only_for_a_real_gap():
    assert H.slept_for(1000, 1000 + H.CHECK_EVERY_S + 5) == 0
    assert H.slept_for(1000, 1000 + 3600) == 3600 - H.CHECK_EVERY_S


def test_disk_warning_once_a_day(monkeypatch):
    monkeypatch.setattr(H, "_warned_disk", None)
    d = date(2026, 11, 24)
    assert H.disk_warning(5.0, d) is None
    assert "1.0 GB" in H.disk_warning(1.0, d)
    assert H.disk_warning(1.0, d) is None
    assert H.disk_warning(1.0, date(2026, 11, 25))


def test_restart_count_from_supervisor_log(tmp_path):
    log = tmp_path / "supervisor.log"
    log.write_text("[2026-11-23 10:00:00] ARYA exited with code 1 — restarting in 5 s\n"
                   "[2026-11-24 10:00:00] started ARYA (pid 1)\n"
                   "[2026-11-24 11:00:00] ARYA stopped answering for 3 minutes — restarting in 5 s\n"
                   "[2026-11-24 12:00:00] ARYA quit normally\n", encoding="utf-8")
    assert H.restarts_since(datetime(2026, 11, 24), log) == 1
    assert H.restarts_since(datetime(2026, 11, 1), log) == 2


ADDRS = {
    "VirtualBox Host-Only Network": [Addr(-1, "0A-00-27-00-00-17", None),
                                     Addr(socket.AF_INET, "192.168.56.1", "255.255.255.0")],
    "Wi-Fi": [Addr(-1, "80-38-FB-8A-6C-D6", None), Addr(socket.AF_INET, "192.168.0.85", "255.255.255.0")],
    "Tailscale": [Addr(socket.AF_INET, "100.74.82.21", "255.192.0.0")],
}


def test_wake_target_is_the_home_wifi_adapter():
    assert H.wake_target(["100.74.82.21", "192.168.0.85"], ADDRS) == {"mac": "80:38:FB:8A:6C:D6",
                                                                       "broadcast": "192.168.0.255"}


def test_wake_target_falls_back_to_the_routed_adapter(monkeypatch):
    monkeypatch.setattr(H, "_routed_ip", lambda: "192.168.0.85")
    assert H.wake_target(["100.74.82.21"], ADDRS)["mac"] == "80:38:FB:8A:6C:D6"
    monkeypatch.setattr(H, "_routed_ip", lambda: "")
    assert H.wake_target(["100.74.82.21"], ADDRS) is None
