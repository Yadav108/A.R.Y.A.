"""Laptop remote control and meeting / do-not-disturb. Locking, sleeping, the registry and windows are faked:
nothing here locks or suspends the real laptop."""
import asyncio
from datetime import datetime

import pytest

from arya.core import quiet as Q
from arya.tools import remote as R


@pytest.fixture(autouse=True)
def safe(monkeypatch):
    actions = []
    monkeypatch.setattr(R, "_lock", lambda: actions.append("lock"))
    monkeypatch.setattr(R, "_suspend", lambda: actions.append("sleep"))
    Q.end()
    R.cancel_sleep()
    yield actions
    Q.end()
    R.cancel_sleep()


# ── Remote control ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["lock the laptop", "Lock my computer please", "lock the screen", "ARYA, lock the PC"])
def test_lock(text, safe):
    assert asyncio.run(R.handle(text)).startswith("Locked")
    assert safe == ["lock"]


def test_sleep_later_and_cancel(safe):
    async def go():
        reply = await R.handle("put the laptop to sleep in 10 minutes")
        assert "10 minutes" in reply and R._sleep["task"] is not None
        assert await R.handle("cancel sleep") == "Okay, the laptop stays awake."
        assert await R.handle("stay awake") == "There's no sleep planned."
    asyncio.run(go())
    assert safe == []


def test_sleep_now_warns_then_sleeps(safe, monkeypatch):
    said = []

    async def say(text):
        said.append(text)
    R.set_announcer(say)
    monkeypatch.setattr(R, "NOW_GRACE_S", 0.05)

    async def go():
        reply = await R.handle("put the laptop to sleep")
        assert "seconds" in reply
        await asyncio.sleep(0.3)
    try:
        asyncio.run(go())
    finally:
        R.set_announcer(None)
    assert said and "cancel sleep" in said[0] and safe == ["sleep"]


def test_whats_running(monkeypatch):
    import psutil
    wins = [(1, 10, "Chrome_WidgetWin_1", "Gmail - Google Chrome"), (2, 10, "Chrome_WidgetWin_1", "YouTube - Google Chrome"),
            (3, 20, "CabinetWClass", "Downloads - File Explorer"), (4, 30, "x", "Spotify Premium"),
            (5, 40, "ApplicationFrameWindow", "WhatsApp"), (6, 50, "Progman", "Program Manager")]
    import arya.tools.windows as W
    monkeypatch.setattr(W, "_visible_windows", lambda: wins)
    names = {10: "chrome.exe", 20: "explorer.exe", 30: "Spotify.exe", 40: "ApplicationFrameHost.exe", 50: "explorer.exe"}
    monkeypatch.setattr(psutil, "Process", lambda pid: type("P", (), {"name": lambda self: names[pid]})())
    assert R.running_apps() == ["Chrome (2 windows)", "File Explorer", "Spotify", "WhatsApp"]


def test_memory(monkeypatch):
    monkeypatch.setattr(R, "free_ram_gb", lambda: 1.8)
    monkeypatch.setattr(R, "memory_hogs", lambda top=3: "Chrome (1.4 GB)")
    assert asyncio.run(R.handle("how much memory is free?")) == \
        "1.8 gigabytes of memory are free. The biggest apps: Chrome (1.4 GB)."


@pytest.mark.parametrize("text", ["lock in the answer", "what's running in the background of this video",
                                  "the laptop sleeps badly", "how do I put Linux to sleep", "sleep well",
                                  "go to sleep", "block the window"])
def test_not_remote(text):
    assert not R.matches(text)


@pytest.mark.parametrize("text", ["what's running?", "which apps are open", "what's open on my laptop",
                                  "what is running on the computer right now"])
def test_running_requests(text):
    assert R._RUNNING.match(text)


# ── Meeting / do-not-disturb ─────────────────────────────────────────────────

NOW = datetime(2026, 11, 16, 14, 0)


@pytest.mark.parametrize("text, until", [
    ("I'm in a meeting", "15:00"), ("I'm in a meeting for 30 minutes", "14:30"),
    ("we're in a call until 3:30", "15:30"), ("do not disturb for two hours", "16:00"),
    ("meeting mode", "15:00"), ("stay quiet until 16:15", "16:15"),
])
def test_meeting_mode(text, until):
    reply = Q.handle(text, now=NOW)
    assert f"until {until}" in reply and Q.active(NOW.timestamp() + 60)


def test_back():
    Q.handle("I'm in a meeting", now=NOW)
    assert Q.handle("I'm back", now=NOW) == "Okay, I'm talking again."
    assert not Q.active(NOW.timestamp())
    assert Q.handle("you can talk again", now=NOW) is None         # not in meeting mode: the nudge settings' turn


@pytest.mark.parametrize("text", ["I have a meeting tomorrow", "add the meeting to my calendar", "be quiet",
                                  "what's a quiet mode in an amplifier", "the meeting was long"])
def test_not_meeting(text):
    assert not Q.matches(text)


# ── In a call, by Windows' microphone record ─────────────────────────────────

@pytest.mark.parametrize("users, titles, app", [
    (["c:#program files#zoom#bin#zoom.exe"], [], "Zoom"),
    (["mstteams_8wekyb3d8bbwe"], [], "Teams"),
    (["5319275a.whatsappdesktop_cv1g1gvanyjgm"], [], "WhatsApp"),
    (["c:#program files#google#chrome#application#chrome.exe"], ["Meet - abc-defg-hij - Google Chrome"],
     "a Meet call in your browser"),
    (["c:#program files#google#chrome#application#chrome.exe"], ["YouTube - Google Chrome"], None),   # mic, no meeting
    (["c:#python314#pythonw.exe"], [], None),                                                           # ARYA itself
    ([], [], None),
])
def test_call_detection(users, titles, app):
    assert Q.call_app(users, titles) == app


def test_auto_quiet_during_a_call_and_back(monkeypatch):
    calls = iter(["Zoom", "Zoom", None])
    monkeypatch.setattr(Q, "call_app", lambda: next(calls))
    monkeypatch.setattr(Q, "CHECK_EVERY_S", 0)
    told = []

    async def go():
        task = asyncio.create_task(Q.run(told.append))
        await asyncio.sleep(0.05)
        task.cancel()
    try:
        asyncio.run(go())
    except StopIteration:
        pass
    assert told[0].startswith("🔕 Looks like you're in a call (Zoom)") and told[-1] == "🔔 Call over: I'm talking again."
    assert not Q.active()


def test_meeting_mode_off_during_a_call_is_respected(monkeypatch):
    Q.start(3600, "call: Zoom", auto=True)
    assert Q.handle("meeting mode off") == "Okay, I'm talking again."
    assert Q._state["declined_call"] == "Zoom"


# ── Silent while quiet ───────────────────────────────────────────────────────

def test_answers_are_shown_not_spoken(monkeypatch):
    import arya.server.websocket as ws
    sent, synthesized = [], []

    async def send(p):
        sent.append(p)

    async def synth(s):
        synthesized.append(s)
        return b""
    monkeypatch.setattr(ws, "_send_all", send)
    monkeypatch.setattr(ws, "_synthesize", synth)

    async def sentences():
        yield "Your meeting is at three."
    Q.start(600, "meeting")
    asyncio.run(ws._speak_stream(sentences()))
    assert "Your meeting is at three." in sent and synthesized == []


def test_announcements_go_to_the_phone_not_the_speakers(monkeypatch):
    import arya.server.websocket as ws
    sent, played = [], []

    async def send(p):
        sent.append(p)

    async def play(audio):
        played.append(audio)
    monkeypatch.setattr(ws, "_send_all", send)
    monkeypatch.setattr(ws.speaker, "play", play)
    Q.start(600, "meeting")
    asyncio.run(ws.announce("Timer done! Tea has finished."))
    assert sent == ["NOTIFY:Timer done! Tea has finished."] and played == []
