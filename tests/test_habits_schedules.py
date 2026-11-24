"""Habits & routines: routines on a schedule (parsing, next run, quiet / asleep rules) and habits (parsing,
logging, streaks, briefing / wrap-up lines, the evening nudge) — and that their phrases don't steal others'.
The clock is injected; nothing is spoken."""
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import arya.core.llm as llm
from arya.core import habits as H
from arya.core import proactive as P
from arya.core import routines as R

MON = datetime(2026, 11, 23, 10, 0)                 # a Monday
WEEKDAYS, ALL = frozenset(range(5)), frozenset(range(7))


@pytest.fixture
def fresh():
    def wipe():
        with R._db() as c:
            for t in ("routines", "routine_schedules", "routine_skips"):
                c.execute(f"DELETE FROM {t}")
        with H._db() as c:
            c.execute("DELETE FROM habits")
            c.execute("DELETE FROM habit_log")
    wipe()
    yield
    wipe()


# ── Schedules: parsing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, days, at", [
    ("every weekday at 8", WEEKDAYS, "08:00"),
    ("every Sunday at 7 pm", {6}, "19:00"),
    ("every sunday at 7 p.m.", {6}, "19:00"),
    ("on Monday and Thursday at 7:30", {0, 3}, "07:30"),
    ("on mondays, wednesdays and fridays at 18:15", {0, 2, 4}, "18:15"),
    ("every morning at 8", ALL, "08:00"),
    ("every evening at 9", ALL, "21:00"),
    ("every day at 3", ALL, "15:00"),
    ("daily at noon", ALL, "12:00"),
    ("at 8 every weekday", WEEKDAYS, "08:00"),
    ("every weekend at 10 am", {5, 6}, "10:00"),
])
def test_parse_when(text, days, at):
    assert R.parse_when(text) == (frozenset(days), at)


@pytest.mark.parametrize("text", ["the lights every day at 7", "every weekday", "at 8", "every day 8",
                                  "every day at 25", "every weekday at 8 and 9"])
def test_parse_when_rejects(text):
    assert R.parse_when(text) is None


def test_next_time():
    assert R.next_time(WEEKDAYS, "08:00", MON) == datetime(2026, 11, 24, 8, 0)          # Tuesday
    assert R.next_time(WEEKDAYS, "11:00", MON) == datetime(2026, 11, 23, 11, 0)         # later today
    assert R.next_time(WEEKDAYS, "08:00", datetime(2026, 11, 27, 9)) == datetime(2026, 11, 30, 8)   # Fri -> Mon
    assert R.next_time(frozenset({0}), "10:00", MON) == datetime(2026, 11, 30, 10, 0)  # exactly now: next week


# ── Schedules: by voice ──────────────────────────────────────────────────────

def _routines():
    R.handle("When I say start my day, give me the briefing, today's calendar and play focus music.")
    R.handle("when I say weekly review, how was my week, then what's on my list")


def test_schedule_list_and_stop(fresh):
    _routines()
    assert R.handle("Run start my day every weekday at 8", MON) == "Okay. Every weekday at 08:00 I'll run \"start my day\"."
    assert R.handle("Every Sunday at 7 pm do my weekly review.", MON) == \
        "Okay. Every Sunday at 19:00 I'll run \"weekly review\"."
    assert R.handle("what runs automatically?", MON) == ("Automatically: every weekday at 08:00, \"start my day\"; "
                                                        "every Sunday at 19:00, \"weekly review\".")
    assert R.handle("which routines run automatically", MON).startswith("Automatically:")
    assert R.handle("what routines do I have?", MON).startswith("Your routines:")       # still the plain list
    assert R.handle("stop running start my day automatically", MON).startswith(
        "Okay, \"start my day\" won't run automatically")
    assert R.handle("cancel the weekly review schedule", MON).startswith("Okay, \"weekly review\" won't")
    assert R.handle("what runs automatically", MON).startswith("Nothing runs automatically yet.")


def test_schedule_more_phrasings(fresh):
    _routines()
    assert R.handle("ARYA, start my day every weekday at 8.", MON).startswith("Okay. Every weekday at 08:00")
    assert R.handle("on Monday and Thursday at 7:30 pm, run weekly review", MON).startswith(
        "Okay. Every Monday and Thursday at 19:30")
    assert R.handle("run start my day at 7 every day", MON).endswith("so it'll still run.")    # quiet hours note
    R.handle("delete the start my day routine")
    with R._db() as c:                                               # its schedule goes with it
        assert c.execute("SELECT COUNT(*) FROM routine_schedules WHERE trigger = 'start my day'").fetchone()[0] == 0


def test_schedule_unknown_routine(fresh):
    assert R.handle("Run lunch prep every day at noon", MON).startswith("I don't have a routine called \"lunch prep\"")
    assert R.handle("every Sunday at 7 pm do my weekly review", MON).startswith("I don't have a routine called")


@pytest.mark.parametrize("message", ["remind me to take my pills every day at 8", "wake me up every weekday at 7",
                                     "every day at 8 I go for a run", "what's on my calendar on Monday at 9?",
                                     "stop running", "what happens on Monday at 9"])
def test_schedule_leaves_other_requests(fresh, message):
    _routines()
    assert R.handle_schedule(message, MON) is None


# ── Schedules: firing ────────────────────────────────────────────────────────

def _fired():
    runs = []

    async def fire(trigger, late):
        runs.append((trigger, round(late)))
    return runs, fire


def _tick(fire, now, **kw):
    kw.setdefault("quiet_hours", lambda h: h >= 22 or h < 8)
    return asyncio.run(R.tick(fire, now, **kw))


def test_fires_on_time_then_moves_on(fresh):
    _routines()
    R.handle("run start my day every weekday at 8", MON)                  # next: Tuesday 08:00
    runs, fire = _fired()
    assert _tick(fire, datetime(2026, 11, 24, 7, 59)) == []
    assert _tick(fire, datetime(2026, 11, 24, 8, 0, 20)) == [("start my day", "ran")]
    assert runs == [("start my day", 20)]
    assert _tick(fire, datetime(2026, 11, 24, 8, 1)) == []                # once
    with R._db() as c:
        nxt = c.execute("SELECT next_at FROM routine_schedules").fetchone()[0]
    assert datetime.fromtimestamp(nxt) == datetime(2026, 11, 25, 8, 0)


def test_asleep_runs_on_wake_if_less_than_two_hours_late(fresh):
    _routines()
    R.handle("run start my day every weekday at 8", MON)
    runs, fire = _fired()
    assert _tick(fire, datetime(2026, 11, 24, 9, 30)) == [("start my day", "ran")]       # 90 min late
    assert runs == [("start my day", 5400)]
    assert _tick(fire, datetime(2026, 11, 25, 10, 30)) == [("start my day", "skipped")]  # 2.5 h late
    assert R.wrap_up_line(date(2026, 11, 25)) == ("I skipped \"start my day\" at 08:00, because the laptop was asleep "
                                                  "or I wasn't running.")
    assert R.wrap_up_line(date(2026, 11, 24)) == ""
    assert len(runs) == 1


def test_meeting_mode_holds_it_until_it_ends(fresh):
    _routines()
    R.handle("run start my day every weekday at 8", MON)
    runs, fire = _fired()
    t = datetime(2026, 11, 24, 8, 0)
    assert _tick(fire, t, quiet="you were in meeting mode") == [("start my day", "held")]
    assert _tick(fire, t + timedelta(minutes=40), quiet="you were in meeting mode") == [("start my day", "held")]
    assert _tick(fire, t + timedelta(minutes=41), busy=True) == [("start my day", "waiting")]
    assert _tick(fire, t + timedelta(minutes=42)) == [("start my day", "ran")]
    assert runs == [("start my day", 42 * 60)]


def test_held_too_long_is_skipped_with_the_reason(fresh):
    _routines()
    R.handle("run start my day every weekday at 8", MON)
    runs, fire = _fired()
    _tick(fire, datetime(2026, 11, 24, 8, 0), quiet="you were on a call")
    assert _tick(fire, datetime(2026, 11, 24, 10, 1)) == [("start my day", "skipped")]
    assert runs == [] and "because you were on a call" in R.wrap_up_line(date(2026, 11, 24))


def test_no_late_run_inside_quiet_hours(fresh):
    _routines()
    R.handle("every day at 9 pm run weekly review", MON)
    runs, fire = _fired()
    assert _tick(fire, datetime(2026, 11, 23, 22, 30)) == [("weekly review", "skipped")]   # woke at 22:30
    assert "because it was quiet hours by then" in R.wrap_up_line(date(2026, 11, 23))
    R.handle("run start my day every day at 7", MON)                         # chosen inside quiet hours: runs
    assert _tick(fire, datetime(2026, 11, 24, 7, 0, 10)) == [("start my day", "ran")]


def test_deleted_routine_never_fires(fresh):
    _routines()
    R.handle("run start my day every weekday at 8", MON)
    with R._db() as c:
        c.execute("DELETE FROM routines")
    runs, fire = _fired()
    _tick(fire, datetime(2026, 11, 24, 8, 1))
    assert runs == []


# ── Habits: parsing and logging ──────────────────────────────────────────────

@pytest.mark.parametrize("what, name, per_week", [
    ("drink 2 litres of water every day", "drink 2 litres of water", 7),
    ("gym 3 times a week", "gym", 3),
    ("go to the gym twice a week", "gym", 2),
    ("meditate", "meditate", 7),
    ("read 20 pages daily", "read 20 pages", 7),
    ("run on weekdays", "run", 5),
])
def test_parse_habit(what, name, per_week):
    assert H.parse(what) == (name, per_week)


def test_keywords():
    assert H.keywords("drink 2 litres of water") == {"water"}
    assert H.keywords("I meditated") == H.keywords("meditate") == {"meditat"}


TUE = date(2026, 11, 24)


def test_track_log_status_stop(fresh):
    assert H.handle("Track a habit: drink 2 litres of water every day.", TUE) == (
        "Okay, I'm tracking \"drink 2 litres of water\", every day. Tell me when you've done it, like "
        "\"done with water\".")
    assert H.handle("track a new habit: gym 3 times a week", TUE).startswith("Okay, I'm tracking \"gym\", 3 times a week")
    assert H.handle("start tracking meditate every day", TUE).startswith("Okay, I'm tracking \"meditate\"")
    assert H.handle("I went to the gym", TUE) == "Nice, logged \"gym\" for today. That's 1 of 3 this week."
    assert H.handle("I went to the gym yesterday", TUE) == "Nice, logged \"gym\" for yesterday. That's 2 of 3 this week."
    assert H.handle("done with water", TUE) == "Nice, logged \"drink 2 litres of water\" for today."
    assert H.handle("water done", TUE) == "Already logged \"drink 2 litres of water\" for today."
    assert H.handle("I meditated", TUE).startswith("Nice, logged \"meditate\"")
    assert H.handle("how are my habits?", TUE) == ("Drink 2 litres of water: done today; Gym: 2 of 3 this week; "
                                                  "Meditate: done today.")
    assert H.handle("what's my gym streak?", TUE) == "Gym: 2 of 3 this week."
    assert H.handle("stop tracking gym", TUE) == "Okay, I've stopped tracking \"gym\"."
    assert H.handle("delete the yoga habit", TUE) == "I'm not tracking a habit called \"yoga\"."


@pytest.mark.parametrize("message", ["I went to the gym and hurt my knee", "I asked about water pumps",
                                     "should I go to the gym today?", "I went to the shop", "done",
                                     "what's my study streak?", "stop tracking my location",
                                     "I didn't go to the gym", "what are good habits for studying?"])
def test_not_a_habit(fresh, message):
    H.handle("track a habit: gym 3 times a week", TUE)
    H.handle("track a habit: drink water every day", TUE)
    assert H.handle(message, TUE) is None


# ── Habits: streaks ──────────────────────────────────────────────────────────

def _d(*offsets):
    return {TUE - timedelta(days=o) for o in offsets}


def test_daily_streak():
    assert H.streak(7, _d(0, 1, 2), TUE) == 3
    assert H.streak(7, _d(1, 2), TUE) == 2                  # today not done yet: the streak still counts
    assert H.streak(7, _d(0, 2, 3), TUE) == 1               # a gap
    assert H.streak(7, _d(2, 3), TUE) == 0
    assert H.streak(7, set(), TUE) == 0


def test_weekly_streak():
    # TUE is Tuesday 24 Nov; weeks start Mon 23, 16, 9 Nov
    last_two_weeks = _d(2, 4, 6, 9, 10, 12)                 # 3 in the week of the 16th, 3 in the week of the 9th
    assert H.streak(3, last_two_weeks, TUE) == 2            # this week not met yet: counted from last week
    assert H.streak(3, last_two_weeks | _d(0, 1), TUE) == 2  # 2 of 3 this week: still 2
    assert H.streak(3, last_two_weeks | _d(0, 1) | {TUE - timedelta(days=1)}, TUE) == 2
    assert H.streak(2, last_two_weeks | _d(0, 1), TUE) == 3  # 2 a week: this week counts now
    assert H.streak(3, _d(2, 4), TUE) == 0
    assert H.this_week("x", TUE, _d(0, 1, 2)) == 2          # Sunday the 22nd is last week


def test_briefing_and_wrap_up_lines(fresh):
    assert H.briefing_line(TUE) == "" and H.wrap_up_line(TUE) == ""
    H.add("drink water", 7, TUE)
    H.add("gym", 3, TUE)
    for o in (1, 2, 3):
        H.log("water", TUE - timedelta(days=o))
    H.log("gym", TUE)
    assert H.briefing_line(TUE) == "Habits: drink water, 3 days in a row; gym, 2 more this week"
    assert H.wrap_up_line(TUE) == "Habits done today: gym (1 of 3 this week). Still to do today: drink water."
    assert H.open_today(TUE) == ["drink water"]


# ── Habits: the evening nudge ────────────────────────────────────────────────

def _s(hour, idle=5.0, **kw):
    kw.setdefault("nudges", frozenset({"habits"}))
    return P.Sensors(now=datetime(2026, 11, 24, hour, 0), idle_s=idle, habits_open=("drink water",), **kw)


def test_habit_nudge_once_an_evening():
    n = P.Nudger(quiet_hours=(22, 8))
    assert n.habits(_s(18)) is None                          # not before 19:00
    text, away = n.habits(_s(19))
    assert text.startswith("Quick check on your habits: drink water") and away is False
    assert n.habits(_s(20)) is None                          # once a day


def test_habit_nudge_goes_to_the_phone_when_away():
    n = P.Nudger(quiet_hours=(22, 8))
    assert n.habits(_s(19, idle=3600))[1] is True


def test_habit_nudge_rules():
    assert P.Nudger(quiet_hours=(22, 8)).habits(_s(22)) is None                         # quiet hours
    assert P.Nudger(quiet_hours=(22, 8)).habits(_s(19, nudges=frozenset())) is None     # the mode doesn't want it
    muted = P.Nudger(quiet_hours=(22, 8), muted_until=float("inf"))
    assert muted.habits(_s(19)) is None
    n = P.Nudger(quiet_hours=(22, 8))
    assert n.habits(P.Sensors(now=datetime(2026, 11, 24, 19), idle_s=5, nudges=frozenset({"habits"}))) is None


# ── Routing ──────────────────────────────────────────────────────────────────

def _say(m):
    async def turn():
        return "".join([s async for s in llm.stream_with_tools(m)])
    return asyncio.run(turn())


def test_routed_through_the_pipeline(fresh, kb, monkeypatch, capsys):
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    _routines()
    for message, route in [("Run start my day every weekday at 8", "routines"),
                           ("what runs automatically?", "routines"),
                           ("stop running start my day automatically", "routines"),
                           ("track a habit: drink 2 litres of water every day", "habits"),
                           ("done with water", "habits"),
                           ("how are my habits?", "habits")]:
        capsys.readouterr()
        _say(message)
        assert f"Instant answer: {route}" in capsys.readouterr().out, message


def test_no_existing_route_is_stolen(fresh):
    """Every phrase in the routing benchmark for another handler is left alone (with routines and habits set up)."""
    _routines()
    R.handle("run start my day every weekday at 8", MON)
    for spec in ("drink water every day", "gym 3 times a week", "read 20 pages every day", "run every day"):
        H.add(*H.parse(spec))
    routes = yaml.safe_load((Path(llm.__file__).parents[1] / "eval" / "routes.yaml").read_text(encoding="utf-8"))
    stolen = []
    for item in routes["routes"]:
        if item["route"] in ("routines", "habits"):
            continue
        if R.handle_schedule(item["say"], MON) is not None or H.handle(item["say"], TUE) is not None:
            stolen.append(item["say"])
    assert stolen == []
