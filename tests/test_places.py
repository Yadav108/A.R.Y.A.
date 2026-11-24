"""Continuity: place reminders (Wi-Fi based), naming places, the phone's endpoints, routing."""
import json
from pathlib import Path

import pytest

from arya.core import places as P

CASES = json.loads((Path(__file__).parents[1] / "android/app/src/test/resources/place_cases.json").read_text())


@pytest.fixture
def db(kb, monkeypatch):
    monkeypatch.setattr(P, "_changed", lambda: None)
    with P._db() as c:                                 # the test database is shared by the whole session
        c.execute("DELETE FROM places")
        c.execute("DELETE FROM place_reminders")
    return kb


@pytest.mark.parametrize("move", CASES["moves"], ids=lambda m: f"{m['prev']}->{m['new']}")
def test_firing_rule_same_as_phone(move):
    fired = P.due(move["prev"], move["new"], CASES["reminders"])
    assert [r["id"] for r in fired] == move["fires"]


@pytest.mark.parametrize("text, want", [
    ("remind me to take the probe when I leave home", ("take the probe", "home", "leave")),
    ("When I get to the lab, remind me to ask Max about the detector",
     ("ask Max about the detector", "lab", "arrive")),
    ("remind me to water the plants when I get home", ("water the plants", "home", "arrive")),
    ("Remind me to call mom when I arrive at the office.", ("call mom", "office", "arrive")),
    ("remind me to buy milk when I leave work", ("buy milk", "work", "leave")),
    ("remind me to X when I come back home", ("X", "home", "arrive")),
])
def test_place_reminders(text, want):
    assert P.parse_reminder(text) == want


@pytest.mark.parametrize("text", [
    "remind me to call mom at 5", "remind me when I'm back at the laptop", "remind me to stretch when I am done",
    "remind me to pay rent when I get paid", "remind me to X when I am at the laptop", "remind me in 10 minutes",
])
def test_not_place_reminders(text):
    assert P.parse_reminder(text) is None


@pytest.mark.parametrize("text, label", [
    ("this is the lab", "lab"), ("This is home.", "home"), ("call this place the gym", "gym"),
    ("this is my office", "office"), ("okay this is work", "work"), ("remember this wifi as Grandma's", "grandma's"),
])
def test_naming(text, label):
    assert P.parse_name(text) == label


@pytest.mark.parametrize("text", ["this is Anna", "this is great", "this is the lab report", "this is it",
                                  "this is a test"])
def test_not_naming(text):
    assert P.parse_name(text) is None


def test_reminder_flow(db):
    reply = P.handle("remind me to take the probe when I leave home")
    assert "when you leave home" in reply and "this is home" in reply       # home's Wi-Fi not known yet
    P.name_place("home", '"Vodafone-531C"')
    assert P.places() == {"home": "Vodafone-531C"}
    assert "I'll remind you" in P.handle("when I get to the lab remind me to check the cryostat")
    items = P.pending()
    assert [(i["place"], i["event"]) for i in items] == [("home", "leave"), ("lab", "arrive")]
    listed = P.handle("what are my place reminders?")
    assert "home" in listed and "take the probe" in listed
    P.report("lab", near_laptop=False, fired=[items[0]["id"]])
    assert [i["text"] for i in P.pending()] == ["check the cryostat"]
    assert "lab" in P.where()
    assert P.handle("cancel the lab reminders").startswith("Cancelled 1")
    assert P.pending() == []


def test_one_name_per_network(db):
    P.name_place("lab", "eduroam")
    P.name_place("institute", "eduroam")
    assert P.places() == {"institute": "eduroam"}


def test_naming_on_the_phone_page_points_to_the_app(db):
    assert "Talk screen" in P.handle("this is the lab", remote=True)


def test_naming_at_the_laptop_uses_its_wifi(db, monkeypatch):
    monkeypatch.setattr(P, "laptop_ssid", lambda: "Vodafone-531C")
    assert P.handle("this is home") == "Got it — this Wi-Fi is home."
    assert P.places() == {"home": "Vodafone-531C"}


def test_phone_action_for_naming():
    from arya.server import phone_actions
    a = phone_actions.parse("this is the lab")
    assert a["action"] == "name_place" and a["label"] == "lab"
    assert phone_actions.parse("this is great") is None or phone_actions.parse("this is great")["action"] != "name_place"


def test_unknown_wifi_name_refused(db):
    with pytest.raises(ValueError):
        P.name_place("lab", "<unknown ssid>")
