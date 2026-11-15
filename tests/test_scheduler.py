"""M3: timers and reminders survive a restart."""
import asyncio
import time

import pytest

from arya.tools import productivity as P


@pytest.fixture
def clean(monkeypatch):
    said = []

    async def notify(text):
        said.append(text)

    monkeypatch.setattr(P, "_notify_callback", notify)
    with P._db() as c:
        c.execute("DELETE FROM scheduled")
    P._SCHEDULED.clear()
    yield said
    for item in list(P._SCHEDULED):
        item.task.cancel()
    P._SCHEDULED.clear()
    with P._db() as c:
        c.execute("DELETE FROM scheduled")


def _rows():
    with P._db() as c:
        return [(r["kind"], r["label"]) for r in c.execute("SELECT * FROM scheduled ORDER BY id")]


def test_timer_and_reminder_are_saved_and_removed_when_done(clean):
    async def main():
        await P.set_timer(60, "Tea")
        await P.set_reminder("submit the lab report", in_minutes=30)
        assert _rows() == [("timer", "Tea"), ("reminder", "submit the lab report")]
        assert "Cancelled timer 'Tea'" in await P.cancel_timer("tea")
        await asyncio.sleep(0)
        assert _rows() == [("reminder", "submit the lab report")]
        await P.set_timer(0.05, "Quick")
        await asyncio.sleep(0.2)                                     # finishes -> announced and removed
        assert ("timer", "Quick") not in _rows()
    asyncio.run(main())
    assert any("Quick has finished" in s for s in clean)


def test_restart_rearms_pending_and_announces_missed(clean):
    now = time.time()
    with P._db() as c:
        c.execute("INSERT INTO scheduled (kind, label, ends_at, created_at) VALUES (?,?,?,?)",
                  ("reminder", "call the lab supervisor", now + 0.1, now))
        c.execute("INSERT INTO scheduled (kind, label, ends_at, created_at) VALUES (?,?,?,?)",
                  ("reminder", "take the washing out", now - 600, now - 3600))           # due while ARYA was off
        c.execute("INSERT INTO scheduled (kind, label, ends_at, created_at) VALUES (?,?,?,?)",
                  ("timer", "ancient", now - 3 * 86400, now - 3 * 86400))                 # too old to mention

    async def main():
        summary = await P.restore_scheduled()
        assert summary == "Scheduler: restored 1, missed 1"
        assert [i.label for i in P._SCHEDULED] == ["call the lab supervisor"]
        await asyncio.sleep(0.3)
    asyncio.run(main())
    assert any(s.startswith("While I was off, your reminder was due at") and "take the washing out" in s
               for s in clean)
    assert any(s == "Reminder: call the lab supervisor" for s in clean)
    assert not any("ancient" in s for s in clean)
    assert _rows() == []


def test_pomodoro_is_not_persisted(clean):
    async def main():
        await P.pomodoro(1, "Study")
        assert _rows() == []
    asyncio.run(main())
