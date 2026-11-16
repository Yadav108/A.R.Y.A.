"""C5: the watchdog that keeps ARYA running, daily backups, starting with Windows.
The child process, the health check, the clock and the registry are faked."""
import sqlite3
from datetime import date, timedelta

import pytest

from arya.core import upkeep as U
from arya.desktop import supervisor as S


class FakeChild:
    def __init__(self, codes):
        self.codes, self.pid, self.killed = list(codes), 1, False

    def poll(self):
        return self.codes.pop(0) if self.codes else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 1


def _run(children, healthy=lambda port: True):
    started, t = [], [0.0]

    def spawn(cmd, cwd=None):
        started.append(cmd)
        return children.pop(0)

    def sleep(s):
        t[0] += s
    code = S.supervise(["--background"], spawn=spawn, check=healthy, sleep=sleep, clock=lambda: t[0], port=8000)
    return code, started


def test_quit_is_not_restarted():
    code, started = _run([FakeChild([None, 0])])
    assert code == 0 and len(started) == 1
    assert started[0][-2:] == ["--child", "--background"]


def test_a_crash_is_restarted():
    code, started = _run([FakeChild([None, 3221225477]), FakeChild([None, None, 0])])     # access violation, then quit
    assert code == 0 and len(started) == 2


def test_a_hang_is_restarted():
    hung = FakeChild([None] * 20)
    calls = {"n": 0}

    def health(port):
        calls["n"] += 1
        return calls["n"] < 2                                 # answers once, then never again
    code, started = _run([hung, FakeChild([None, 0])], healthy=health)
    assert hung.killed and len(started) == 2 and code == 0


def _timed_run(children):
    """Like _run, but also returns the fake time each child was started at."""
    starts, t = [], [0.0]

    def spawn(cmd, cwd=None):
        starts.append(t[0])
        return children.pop(0)

    def sleep(s):
        t[0] += s
    code = S.supervise([], spawn=spawn, check=lambda port: True, sleep=sleep, clock=lambda: t[0], port=8000)
    return code, starts


def test_server_that_died_at_startup_is_restarted_within_seconds():
    """2026-11-16: startup failed for lack of memory; the empty window waited for the 3-minute health check."""
    code, starts = _timed_run([FakeChild([None, None, S.SERVER_STOPPED]), FakeChild([None, 0])])
    assert code == 0 and len(starts) == 2
    # two polls (4 s) + the failed-start pause (30 s), not 3+ minutes of health checks
    assert starts[1] == 2 * S.POLL_S + S.FAILED_START_BACKOFF_S


def test_a_late_crash_restarts_quickly():
    code, starts = _timed_run([FakeChild([None] * 100 + [1]), FakeChild([0])])
    assert starts[1] == 100 * S.POLL_S + S.BACKOFF_S


def test_child_is_told_it_is_supervised(monkeypatch):
    seen = {}
    monkeypatch.setattr(S.subprocess, "Popen", lambda cmd, cwd=None, env=None: seen.update(env=env) or "child")
    assert S._spawn(["x"]) == "child"
    assert seen["env"][S.ENV_FLAG] == "1"


def test_desktop_app_exits_for_the_watchdog_only_when_supervised(monkeypatch):
    from arya.desktop import app
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            timers.append((delay, fn, args))

        def start(self):
            pass
    monkeypatch.setattr(app.threading, "Timer", FakeTimer)
    monkeypatch.delenv(S.ENV_FLAG, raising=False)
    app.DesktopApp._exit_for_watchdog(None)
    assert timers == []                                       # run directly: keep the "couldn't start" window
    monkeypatch.setenv(S.ENV_FLAG, "1")
    app.DesktopApp._exit_for_watchdog(None)
    assert timers == [(3.0, app.os._exit, (S.SERVER_STOPPED,))]


def test_gives_up_after_too_many_crashes():
    code, started = _run([FakeChild([1]) for _ in range(10)])
    assert code == 1 and len(started) == S.MAX_RESTARTS + 1


# ── Backups ────────────────────────────────────────────────────────────────

@pytest.fixture
def store(kb, tmp_path, monkeypatch):
    monkeypatch.setattr(U, "backup_dir", lambda: tmp_path / "backups")
    from arya.memory.database import DB_PATH
    with sqlite3.connect(DB_PATH) as c:
        c.execute("CREATE TABLE IF NOT EXISTS upkeep_probe (x TEXT)")
        c.execute("INSERT INTO upkeep_probe VALUES ('remember me')")
    return tmp_path


def test_backup_is_a_real_copy_and_keeps_seven_days(store):
    start = date(2026, 11, 1)
    for i in range(10):
        U.back_up(start + timedelta(days=i))
    names = [p.name for p in U.backups()]
    assert names == [(start + timedelta(days=i)).isoformat() for i in range(3, 10)]          # the last 7
    c = sqlite3.connect(U.backups()[-1] / "arya.db")
    assert c.execute("SELECT x FROM upkeep_probe").fetchone()[0] == "remember me"
    c.close()
    assert U.backed_up_today(start + timedelta(days=9)) and not U.backed_up_today(start + timedelta(days=20))


def test_backup_voice(store):
    assert U.handle("when was the last backup?").startswith("There's no backup yet")
    assert U.handle("back up now").startswith("Backed up:")
    assert U.handle("when was the last backup?").startswith("The last backup is from")


@pytest.mark.parametrize("message, on", [("start ARYA with Windows", True), ("start automatically when I log in", True),
                                         ("don't start with Windows", False), ("turn off autostart", False)])
def test_autostart_voice(monkeypatch, message, on):
    from arya.desktop import system
    seen = []
    monkeypatch.setattr(system, "set_autostart", lambda enabled: seen.append(enabled))
    assert U.handle(message) is not None and seen == [on]


@pytest.mark.parametrize("message", ["what is a backup generator", "start studying", "start the timer", "back to the topic"])
def test_not_upkeep(message):
    assert U.handle(message) is None
