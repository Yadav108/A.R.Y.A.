"""What the ARYA phone app relies on: typed events that survive a restart, long-polling with catch-up, the key in
an Authorization header, snooze/done buttons, Tailscale addresses, and the sources that push (reminders, timers,
alarms, calendar, briefing, wrap-up, meeting mode)."""
import asyncio
import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from arya.server import phone as P

POST = {"X-ARYA": "1"}
REAL_CLIENT_OK = P.client_ok                     # before the fixture swaps it for the test client


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "SETTINGS_FILE", tmp_path / "phone.json")
    monkeypatch.setattr(P, "TOKEN_FILE", tmp_path / "phone_token")
    monkeypatch.setattr(P, "open_pairing_page", lambda: None)
    P._state.hosts = {"testserver"}
    monkeypatch.setattr(P, "client_ok", lambda host: True)          # TestClient's client is "testclient"


def bearer():
    return {"Authorization": f"Bearer {P.token()}", **POST}


# ── Events ────────────────────────────────────────────────────────────────────

def test_typed_events_are_numbered_kept_on_disk_and_reloaded(monkeypatch):
    P.push_event("reminder", "Reminder", "Reminder: call Ben", ref={"label": "call Ben"})
    P.push("Battery at 15 percent")
    first, second = P.feed_after(0)
    assert first["kind"] == "reminder" and first["title"] == "Reminder" and first["ref"] == {"label": "call Ben"}
    assert second["id"] > first["id"] and second["kind"] == "nudge"
    lines = P.EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["text"] for l in lines] == ["Reminder: call Ben", "Battery at 15 percent"]
    monkeypatch.setattr(P, "_state", P._State())                      # ARYA restarted
    assert [i["id"] for i in P.feed_after(0)] == [first["id"], second["id"]]
    third = P.push("after the restart")
    assert third["id"] > second["id"]                                 # ids keep growing: the app's "after" still works


def test_the_generic_copy_of_a_typed_event_is_dropped():
    P.push_event("reminder", "Reminder", "Reminder: stretch", ref={"label": "stretch"})
    P.note_event("NOTIFY:Reminder: stretch")                          # what announce() also sends
    P.note_event("NOTIFY:Something else")
    assert [i["text"] for i in P.feed_after(0)] == ["Reminder: stretch", "Something else"]


def test_file_is_trimmed_on_load(monkeypatch):
    for i in range(P.FEED_SIZE + 30):
        P.push(f"n{i}")
    monkeypatch.setattr(P, "_state", P._State())
    assert len(P.feed_after(0)) == P.FEED_SIZE
    assert len(P.EVENTS_FILE.read_text(encoding="utf-8").splitlines()) == P.FEED_SIZE


# ── The app's endpoints ───────────────────────────────────────────────────────

def test_bearer_key_works_and_a_wrong_one_does_not():
    c = TestClient(P.app)
    hello = c.get("/api/hello", headers=bearer()).json()
    assert hello["paired"] is True and "reminder" in hello["notify_kinds"] and "nudge" not in hello["notify_kinds"]
    assert c.get("/api/hello", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/action", json={"id": 1, "action": "done"},
                  headers={"Authorization": f"Bearer {P.token()}"}).status_code == 403    # POST needs X-ARYA


def test_wait_returns_at_once_with_what_was_missed():
    a = P.push_event("timer", "Timer done", "Timer done! Tea has finished.", ref={"label": "Tea"})
    P.push("later nudge")
    c = TestClient(P.app)
    r = c.get("/api/wait", params={"after": 0, "timeout": 1}, headers=bearer()).json()
    assert [i["kind"] for i in r["items"]] == ["timer", "nudge"]
    r = c.get("/api/wait", params={"after": r["last"], "timeout": 1}, headers=bearer()).json()
    assert r["items"] == []


def test_wait_is_answered_as_soon_as_something_arrives():
    async def go():
        transport = httpx.ASGITransport(app=P.app, client=("100.101.102.103", 5555))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            task = asyncio.create_task(c.get("/api/wait", params={"after": P.last_id(), "timeout": 20},
                                             headers=bearer()))
            await asyncio.sleep(0.3)
            assert not task.done()
            P.push_event("calendar", "Coming up", "Heads-up: Lab meeting starts in 10 minutes.")
            r = await asyncio.wait_for(task, 3)
            return r.json()
    data = asyncio.run(go())
    assert [i["kind"] for i in data["items"]] == ["calendar"]


def test_snooze_and_done(monkeypatch):
    from arya.tools import productivity
    calls = []

    async def fake_set_reminder(message, at_time=None, in_minutes=None):
        calls.append((message, in_minutes))
        return "ok"
    monkeypatch.setattr(productivity, "set_reminder", fake_set_reminder)
    r = P.push_event("reminder", "Reminder", "Reminder: call Ben", ref={"label": "call Ben"})
    item = P.feed_after(0)[-1]
    c = TestClient(P.app)
    out = c.post("/api/action", json={"id": item["id"], "action": "snooze", "minutes": 15}, headers=bearer()).json()
    assert out["reply"] == "Snoozed for 15 minutes." and calls == [("call Ben", 15.0)]
    brief = P.push("Good morning.", kind="briefing")
    assert c.post("/api/action", json={"id": brief["id"], "action": "snooze"}, headers=bearer()).status_code == 400
    assert c.post("/api/action", json={"id": brief["id"], "action": "done"}, headers=bearer()).json()["ok"]
    assert c.post("/api/action", json={"id": 12345, "action": "done"}, headers=bearer()).status_code == 404


# ── Tailscale ─────────────────────────────────────────────────────────────────

def test_tailscale_addresses_are_allowed_public_ones_are_not():
    ok = REAL_CLIENT_OK
    assert ok("100.101.102.103") and ok("100.64.0.1") and ok("192.168.0.20") and ok("127.0.0.1")
    assert not ok("8.8.8.8") and not ok("100.128.0.1") and not ok("testclient")      # 100.128+ isn't Tailscale


def test_tailscale_ip_found(monkeypatch):
    import socket as S
    fake = types.SimpleNamespace(net_if_addrs=lambda: {
        "Wi-Fi": [types.SimpleNamespace(family=S.AF_INET, address="192.168.0.85")],
        "Tailscale": [types.SimpleNamespace(family=S.AF_INET, address="100.88.1.7")]})
    import sys
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert P.tailscale_ip() == "100.88.1.7"


def test_pairing_qr_prefers_tailscale(monkeypatch):
    monkeypatch.setattr(P, "where_to_listen", lambda: P.Where("192.168.0.85", "", "Home"))
    monkeypatch.setattr(P, "tailscale_ip", lambda: "100.88.1.7")
    page = P.pairing_page()
    assert "Tailscale" in page and "http://192.168.0.85:8001" in page
    assert P.pairing_url("100.88.1.7").startswith("http://100.88.1.7:8001/#k=")


# ── The sources ───────────────────────────────────────────────────────────────

def test_reminders_alarms_and_timers_are_pushed_typed(monkeypatch):
    from arya.tools import productivity as prod
    monkeypatch.setattr(prod, "_notify_callback", None)
    asyncio.run(prod._reminder_worker(0, "call Ben"))
    asyncio.run(prod._reminder_worker(0, "Wake up, it's 07:00. This is your alarm."))
    asyncio.run(prod._timer_worker(0, "Tea"))
    kinds = [(i["kind"], i["title"], i["ref"]["label"]) for i in P.feed_after(0)]
    assert kinds == [("reminder", "Reminder", "call Ben"),
                     ("alarm", "Alarm", "Wake up, it's 07:00. This is your alarm."),
                     ("timer", "Timer done", "Tea")]


def test_calendar_heads_up_and_briefing_reach_the_phone(monkeypatch):
    from arya.core import proactive
    proactive._to_phone("calendar", "Coming up", "Heads-up: Lab meeting starts in 10 minutes.")
    proactive._to_phone("briefing", "Good morning", "Good morning. Sunny, 12 degrees.")
    assert [(i["kind"], i["title"]) for i in P.feed_after(0)] == [("calendar", "Coming up"),
                                                                  ("briefing", "Good morning")]