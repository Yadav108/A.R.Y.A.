"""Phase B: proactive nudges — battery, breaks, due cards; and all the times ARYA must stay quiet.
Sensors are faked: nothing is read from the laptop and nothing is spoken."""
from datetime import datetime, timedelta

import pytest

from arya.core import proactive as P

T0 = datetime(2026, 11, 16, 14, 0)                       # a Monday afternoon


def S(minutes=0.0, **kw):
    kw.setdefault("idle_s", 5)
    return P.Sensors(now=T0 + timedelta(minutes=minutes), **kw)


def test_battery_warns_once_per_level_and_rearms_when_plugged():
    n = P.Nudger()
    assert n.check(S(battery=(35, False))) is None
    assert n.check(S(1, battery=(20, False))) == "Battery is at 20 percent. Time to plug in the charger."
    assert n.check(S(2, battery=(19, False))) is None                              # said once
    assert n.check(S(3, battery=(10, False))).startswith("Battery is down to 10 percent")   # despite the gap rule
    assert n.check(S(4, battery=(9, False))) is None
    n.check(S(5, battery=(30, True)))                                               # plugged in
    assert n.check(S(6, battery=(20, False))).startswith("Battery is at 20 percent")


def test_jumping_straight_below_ten_gives_one_warning():
    n = P.Nudger()
    assert n.check(S(battery=(8, False))).startswith("Battery is down to 8 percent")
    assert n.check(S(1, battery=(7, False))) is None


def test_break_after_ninety_minutes_of_activity():
    n = P.Nudger()
    for m in range(0, 90, 5):
        assert n.check(S(m)) is None
    msg = n.check(S(90))
    assert msg.startswith("You've been at the laptop for an hour and a half without a break")
    assert n.check(S(100)) is None                                                  # once per stretch
    n.check(S(105, idle_s=6 * 60))                                                  # took a break
    for m in range(110, 200, 5):
        assert n.check(S(m)) is None
    assert n.check(S(200)).startswith("You've been at the laptop")


def test_no_break_nudge_during_a_study_session():
    n = P.Nudger()
    n.check(S(0, studying=True))
    assert n.check(S(120, studying=True)) is None


def test_due_cards_once_a_day_after_ten():
    n = P.Nudger()
    early = P.Sensors(now=datetime(2026, 11, 16, 9, 30), idle_s=5, due_cards=6)
    assert n.check(early) is None
    msg = n.check(S(due_cards=6))
    assert msg == "You have 6 practice cards due today. Say \"quiz me\" when you have ten minutes."
    assert n.check(S(60, due_cards=6)) is None
    assert P.Nudger().check(S(due_cards=6, practised_today=True)) is None
    assert P.Nudger().check(S(due_cards=6, studying=True)) is None


@pytest.mark.parametrize("why", ["quiet hours", "away", "windows busy", "muted", "just spoke"])
def test_stays_quiet(why):
    n = P.Nudger()
    s = S(due_cards=3)
    if why == "quiet hours":
        s.now = datetime(2026, 11, 16, 23, 15)
    elif why == "away":
        s.idle_s = 15 * 60
    elif why == "windows busy":
        s.windows_ok = False
    elif why == "muted":
        n.muted_until = s.now.timestamp() + 3600
    else:
        n.last_spoken = s.now.timestamp() - 60
    assert n.check(s) is None


def test_quiet_hours_window():
    n = P.Nudger(quiet_hours=(22, 8))
    assert n._quiet(23) and n._quiet(3) and not n._quiet(8) and not n._quiet(21)
    assert P.Nudger(quiet_hours=(13, 15))._quiet(14) and not P.Nudger(quiet_hours=(13, 15))._quiet(15)


@pytest.fixture
def fresh(kb, monkeypatch):
    monkeypatch.setattr(P, "nudger", P.Nudger())
    yield P.nudger


def test_mute_and_unmute_by_voice(fresh):
    now = datetime(2026, 11, 16, 14, 0)
    assert P.handle("do not disturb me for two hours", now) == ("Okay, I won't disturb you until 16:00. "
                                                               "Timers and reminders still ring.")
    assert fresh.muted_until == now.timestamp() + 7200
    assert P.handle("no more nudges", now).startswith("Okay, no more nudges until you say")
    assert fresh.muted_until == float("inf")
    assert P.handle("turn nudges back on", now).startswith("Okay, I'll speak up again")
    assert fresh.muted_until == 0.0
    P.handle("don't disturb me today", now)
    assert datetime.fromtimestamp(fresh.muted_until).date() == now.date()
    P._load(fresh)                                                                   # survives a restart
    assert fresh.muted_until > now.timestamp()
    P.handle("nudges back on", now)


@pytest.mark.parametrize("message", ["cancel the reminder", "remind me to call mum at 5", "set a timer",
                                     "what is a disturbance input", "stop"])
def test_other_requests_are_not_mute_commands(fresh, message):
    assert P.handle(message) is None


def test_a_sleeping_laptop_is_not_ninety_minutes_of_work():
    n = P.Nudger()
    n.check(S(0))
    assert n.check(S(120)) is None                       # no checks in between: asleep, not working
    assert n.check(S(125)) is None


def test_spoken_durations():
    assert P._spoken(90) == "an hour and a half" and P._spoken(270) == "4 and a half hours"
    assert P._spoken(75) == "an hour and 15 minutes" and P._spoken(120) == "2 hours"
