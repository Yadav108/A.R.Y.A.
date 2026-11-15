"""Phase B: calendar — ARYA's own events, .ics calendars (with weekly lectures), voice and heads-ups.
Uses a throwaway calendar folder and database; nothing is fetched."""
from datetime import datetime, timedelta, timezone

import pytest

from arya.core import proactive
from arya.tools import agenda as A

NOW = datetime(2026, 11, 16, 9, 0)                    # Monday morning

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:dyn-lecture
SUMMARY:Dynamics lecture
LOCATION:H2
DTSTART:20261013T100000
DTEND:20261013T113000
RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20270210T000000Z
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:Reading week
DTSTART;VALUE=DATE:20261123
DTEND;VALUE=DATE:20261128
END:VEVENT
BEGIN:VEVENT
UID:dentist
SUMMARY:Dentist
DTSTART:20261116T091500
DTEND:20261116T100000
END:VEVENT
BEGIN:VEVENT
UID:call
SUMMARY:Call with Berlin
DTSTART:20261118T120000Z
DTEND:20261118T123000Z
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def cal(kb, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "ICS_DIR", tmp_path)
    (tmp_path / "uni.ics").write_text(ICS, encoding="utf-8")
    with A._db() as c:
        c.execute("DELETE FROM events")
    yield tmp_path
    with A._db() as c:
        c.execute("DELETE FROM events")


def test_ics_weekly_lecture_timezones_and_all_day(cal):
    tue = A.day_events(NOW.date() + timedelta(days=1))
    assert [(e.title, e.start.strftime("%H:%M"), e.location) for e in tue] == [("Dynamics lecture", "10:00", "H2")]
    call = [e for e in A.events_between(NOW, NOW + timedelta(days=4)) if e.title == "Call with Berlin"][0]
    assert call.start == datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    week_after = A.day_events(NOW.date() + timedelta(days=8))
    assert week_after[0].title == "Reading week" and week_after[0].all_day                # all day first


@pytest.mark.parametrize("message, reply", [
    ("Add Control lab every Thursday at 2 pm in room B12",
     "Added Control lab every Thursday at 14:00 in room B12."),
    ("add a meeting with Prof Weber to my calendar tomorrow at 11:30",
     "Added Meeting with Prof Weber tomorrow at 11:30."),
    ("put gym in my calendar on Friday at 18:00 to 19:30", "Added Gym on Friday at 18:00."),
    ("add study group to my calendar today at 5", "Added Study group today at 17:00."),
    ("add dinner with Mia to my calendar", "Which day is dinner with Mia?"),
    ("add lunch to my calendar tomorrow", "What time is lunch?"),
])
def test_add_by_voice(cal, message, reply):
    assert A.handle(message, NOW) == reply


def test_weekly_event_repeats_and_can_be_removed(cal):
    A.handle("Add Control lab every Thursday at 2 pm", NOW)
    for weeks in (0, 1, 5):
        day = NOW.date() + timedelta(days=3 + 7 * weeks)
        assert any(e.title == "Control lab" and e.start.hour == 14 for e in A.day_events(day))
    assert A.handle("remove control lab from my calendar", NOW) == "Removed control lab from your calendar."
    assert not any(e.title == "Control lab" for e in A.day_events(NOW.date() + timedelta(days=3)))
    assert A.handle("delete the dynamics lecture from my calendar", NOW).startswith("I only manage events added here")


@pytest.mark.parametrize("message, reply", [
    ("what's on my calendar today?", "Today you have one thing: Dentist at 09:15."),
    ("What do I have tomorrow?", "Tomorrow you have one thing: Dynamics lecture at 10:00 in H2."),
    ("what's on Saturday", "Nothing on your calendar on Saturday."),
    ("what's next", "Next: Dentist today at 09:15."),
    ("when is my next lecture?", "Next: Dynamics lecture tomorrow at 10:00 in H2."),
    ("when is my next dynamics lecture", "Next: Dynamics lecture tomorrow at 10:00 in H2."),
    ("when's my next seminar", "I don't see a seminar coming up on your calendar."),
    ("am I free tomorrow at 10:30?", "No, you have Dynamics lecture at 10:00 in H2."),
    ("am I free tomorrow at 3 pm?", "Yes, nothing is on your calendar at 15:00 tomorrow."),
])
def test_questions(cal, message, reply):
    assert A.handle(message, NOW) == reply


def test_several_events_in_a_day(cal):
    A.handle("add coffee with Sam to my calendar today at 12", NOW)
    assert A.handle("what's on today", NOW) == ("Today you have 2 things: Dentist at 09:15 and "
                                                 "Coffee with Sam at 12:00.")
    assert A.briefing_line(NOW) == "Today: Dentist at 09:15; Coffee with Sam at 12:00."
    assert A.briefing_line(NOW.replace(hour=13)) == "No calendar events left today."


@pytest.mark.parametrize("message", ["what's next step in exercise 1-13", "what's the next song",
                                     "explain the Coriolis acceleration", "add 3 and 4", "what is a schedule",
                                     "set a timer for 10 minutes", "when is my next exam"])
def test_not_calendar(cal, message):
    assert A.handle(message, NOW) is None


def test_heads_up_ten_minutes_before(cal):
    ev = A.heads_up(NOW.replace(hour=9, minute=6))
    assert ev.title == "Dentist"
    assert A.heads_up(NOW.replace(hour=8, minute=50)) is None
    n = proactive.Nudger()
    s = proactive.Sensors(now=NOW.replace(minute=6), idle_s=5, upcoming=(ev.key, "Dentist", ev.start))
    assert n.check(s) == "Heads-up: Dentist starts in 9 minutes."
    s.now = NOW.replace(minute=7)
    assert n.check(s) is None                                                # once per event
