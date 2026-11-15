"""Follow-ups from the live conversation of 2026-11-15 (all sentences are real transcripts from the log):
the name alone or first ("Area.", "Uh, Aria?", "I'll play …"), commands ARYA ignored as side talk,
"play X by Y" without a site, "open YouTube", "any music", and a quiz answer of "I don't know, …"."""
import asyncio

import pytest

from arya.core import addressee
from arya.study import practice
from arya.tools import browser as B
from arya.tools import windows as W


@pytest.mark.parametrize("heard, rest, named", [
    ("Area", "", True), ("Uh, Aria?", "", True), ("Hey, Ario.", "", True), ("Arya.", "", True),
    ("Aarya, how do you stabilize this PT2 system?", "how do you stabilize this PT2 system?", True),
    ("Area, play Kon Tujhe by Kishore Kumar.", "play Kon Tujhe by Kishore Kumar.", True),
    ("I'll play Kon Tujhe by Kishore Kumar.", "play Kon Tujhe by Kishore Kumar.", True),
    ("The area under the curve is the integral.", "The area under the curve is the integral.", False),
    ("I'll be right back.", "I'll be right back.", False),
    ("Aryan, are you there?", "Aryan, are you there?", False),
])
def test_name_in_followups(heard, rest, named):
    assert addressee.split_name(heard) == (rest, named)


@pytest.mark.parametrize("command", ["Go to YouTube and search", "any music.", "minimized chrome window.",
                                     "play Kon Tujhe by Kishore Kumar.", "put on some music"])
def test_ignored_commands_now_count_as_commands(command):
    assert addressee.quick_verdict(command) is True


@pytest.fixture
def chrome(monkeypatch):
    state = {"opened": [], "keys": [], "media": [], "now": None}

    async def fake_open(url):
        state["opened"].append(url)
        return f"Opened {url}"

    async def fake_video(q):
        return "VIDEOID0000"

    async def now_playing():
        return state["now"]

    async def media_control(action, steps=5):
        state["media"].append(action)
        return "Toggled play/pause."

    import arya.tools.media as media
    import arya.tools.music as music
    import arya.tools.os_tools as os_tools
    monkeypatch.setattr(os_tools, "open_url", fake_open)
    monkeypatch.setattr(B, "first_video", fake_video)
    monkeypatch.setattr(music, "now_playing", now_playing)
    monkeypatch.setattr(media, "media_control", media_control)
    monkeypatch.setattr(B, "MUSIC_SITE", "youtube")
    return state


def _h(m):
    return asyncio.run(B.handle(m))


@pytest.mark.parametrize("message, query", [
    ("Play Kon Tujhe by Kishore Kumar.", "Kon Tujhe by Kishore Kumar"),
    ("play the song Kesariya", "Kesariya"),
    ("play lofi study beats", "lofi study beats"),
])
def test_play_without_a_site_goes_to_youtube(chrome, message, query):
    assert _h(message) == f"Playing {query} on YouTube."
    assert chrome["opened"] == ["https://www.youtube.com/watch?v=VIDEOID0000"]


@pytest.mark.parametrize("message", ["play", "play the step response", "play it again", "pause the music"])
def test_play_that_is_not_a_song(chrome, message):
    assert _h(message) is None


@pytest.mark.parametrize("message, url, reply", [
    ("Open YouTube.", "https://www.youtube.com", "Opening YouTube."),
    ("go to gmail", "https://mail.google.com", "Opening Gmail."),
    ("Go to YouTube and search", "https://www.youtube.com", "YouTube is open. What should I search for?"),
    ("go to YouTube and search for Bode plots", "https://www.youtube.com/results?search_query=Bode+plots",
     "Searching YouTube for Bode plots."),
])
def test_open_sites(chrome, message, url, reply):
    assert _h(message) == reply and chrome["opened"] == [url]


def test_any_music(chrome):
    assert _h("any music.") == "Opening YouTube Music. Tell me a song or an artist and I'll put it on."
    assert chrome["opened"] == ["https://music.youtube.com"]
    chrome["now"] = {"title": "Kon Tujhe", "artist": "Kishore Kumar", "app": "Chrome", "playing": False}
    assert _h("play some music") == "Resuming Kon Tujhe by Kishore Kumar."
    assert chrome["media"] == ["play_pause"]


def test_minimized_past_tense(monkeypatch):
    shown = []
    monkeypatch.setattr(W, "_app_windows", lambda app: [7])
    monkeypatch.setattr(W, "_show", lambda h, mode: shown.append((h, mode)))
    assert asyncio.run(W.handle("minimized chrome window.")) == "Minimized Chrome."
    assert shown == [([7], W.SW_MINIMIZE)]


@pytest.mark.parametrize("answer, skip", [
    ("I don't know, I think that's what we're trying to do.", True), ("I don't know", True),
    ("no idea.", True), ("I'm not sure", True),
    ("I don't know, maybe it is 52 percent", False), ("one hundred times e to the minus", False),
])
def test_quiz_skip(answer, skip):
    assert bool(practice._SKIP.match(answer)) is skip
