"""Browser: Chrome tabs, searches and YouTube. The window, keys, network and browser are faked."""
import asyncio

import pytest

import arya.core.llm as llm
from arya.tools import browser as B


@pytest.fixture
def chrome(monkeypatch):
    state = {"hwnd": 4242, "front": True, "keys": [], "opened": [], "video": "3NycM9lYdRI", "apps": []}
    monkeypatch.setattr(B, "_chrome_window", lambda: state["hwnd"])
    monkeypatch.setattr(B, "_focus", lambda hwnd: state["front"])
    monkeypatch.setattr(B, "_press", lambda *keys: state["keys"].append(keys))

    async def fake_open(url):
        state["opened"].append(url)
        return f"Opened {url} in Chrome"

    async def fake_video(q):
        return state["video"]

    async def fake_app(name):
        state["apps"].append(name)
        return f"Opened {name}"

    import arya.tools.os_tools as os_tools
    monkeypatch.setattr(os_tools, "open_url", fake_open)
    monkeypatch.setattr(os_tools, "open_app", fake_app)
    monkeypatch.setattr(B, "first_video", fake_video)
    monkeypatch.setattr(B, "MUSIC_SITE", "youtube")          # not whatever .env says (ARYA_MUSIC_SITE)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    return state


def _h(m):
    return asyncio.run(B.handle(m))


C, S, T = B.VK_CONTROL, B.VK_SHIFT, B.VK_TAB


@pytest.mark.parametrize("message, keys, reply", [
    ("open a new tab", (C, ord("T")), "New tab open."),
    ("ARYA, new tab please", (C, ord("T")), "New tab open."),
    ("close this tab", (C, ord("W")), "Closed the tab."),
    ("close the tab", (C, ord("W")), "Closed the tab."),
    ("reopen the tab I closed", (C, S, ord("T")), "Reopened the last closed tab."),
    ("reopen the last closed tab", (C, S, ord("T")), "Reopened the last closed tab."),
    ("next tab", (C, T), "Next tab."),
    ("switch to the previous tab", (C, S, T), "Previous tab."),
    ("go to tab 3", (C, ord("3")), "Switched to tab 3."),
    ("switch to the second tab", (C, ord("2")), "Switched to tab 2."),
    ("go to the last tab", (C, ord("9")), "Switched to the last tab."),
])
def test_tabs(chrome, message, keys, reply):
    assert _h(message) == reply
    assert chrome["keys"] == [keys]


def test_keys_only_go_to_chrome(chrome):
    chrome["front"] = False
    assert _h("close this tab") == "I couldn't bring Chrome to the front, so I didn't press anything."
    assert chrome["keys"] == []
    chrome["hwnd"] = None
    assert _h("next tab") == "Chrome isn't open."
    assert _h("open a new tab") == "Opened Chrome with a new tab." and chrome["apps"] == ["chrome"]
    assert chrome["keys"] == []


@pytest.mark.parametrize("message, url, reply", [
    ("search Google for Bode plot examples", "https://www.google.com/search?q=Bode+plot+examples",
     "Searching Google for Bode plot examples."),
    ("google Coriolis force", "https://www.google.com/search?q=Coriolis+force", "Searching Google for Coriolis force."),
    ("search YouTube for PID tuning tutorial", "https://www.youtube.com/results?search_query=PID+tuning+tutorial",
     "Searching YouTube for PID tuning tutorial."),
    ("look up train times to Aachen on the web", "https://www.google.com/search?q=train+times+to+Aachen",
     "Searching Google for train times to Aachen."),
    ("search the internet for laptop RAM prices", "https://www.google.com/search?q=laptop+RAM+prices",
     "Searching Google for laptop RAM prices."),
    ("find lofi study playlists on YouTube Music", "https://music.youtube.com/search?q=lofi+study+playlists",
     "Searching YouTube Music for lofi study playlists."),
])
def test_search(chrome, message, url, reply):
    assert _h(message) == reply
    assert chrome["opened"] == [url]


def test_play_on_youtube_music(chrome):
    assert _h("play Coldplay Yellow on YouTube Music") == "Playing Coldplay Yellow on YouTube Music."
    assert chrome["opened"] == ["https://music.youtube.com/watch?v=3NycM9lYdRI"]
    assert _h("play lofi beats on youtube") == "Playing lofi beats on YouTube."
    assert chrome["opened"][-1] == "https://www.youtube.com/watch?v=3NycM9lYdRI"


def test_play_falls_back_to_results(chrome):
    chrome["video"] = None
    assert _h("play Coldplay on YouTube Music") == "Here are the YouTube Music results for Coldplay; pick one to play."
    assert chrome["opened"] == ["https://music.youtube.com/search?q=Coldplay"]


@pytest.mark.parametrize("message, query", [
    ("Play Sati Gatha.", "Sati Gatha"),                                    # from-log 2026-11-16
    ("play sati katha", "sati katha"),                                     # from-log (Whisper's spelling)
    ("play romantic song", "romantic"),                                    # from-log
    ("ARYA, play Kesariya please", "Kesariya"),
    ("can you play Tum Hi Ho", "Tum Hi Ho"),
])
def test_play_a_song_by_name(chrome, message, query):
    assert _h(message) == f"Playing {query} on YouTube."
    assert chrome["opened"] == ["https://www.youtube.com/watch?v=3NycM9lYdRI"]


def test_open_youtube_music_and_play(chrome):
    """From the phone, 2026-11-16: both parts became a play/pause key press and nothing started."""
    assert _h("Arya open youtube music and play romantic song") == "Playing romantic on YouTube Music."
    assert chrome["opened"] == ["https://music.youtube.com/watch?v=3NycM9lYdRI"]     # one tab, playing


@pytest.mark.parametrize("message", [
    "play it", "play it again", "play the next song", "play the previous track", "play music", "play the video",
    "play a game", "play chess with me", "play the lecture recording", "play the simulation",
])
def test_play_that_is_not_a_song(chrome, message):
    assert _h(message) is None
    assert chrome["opened"] == []


def test_two_part_requests_use_browser_commands(chrome, monkeypatch):
    calls = []

    async def no_model(*a, **k):
        calls.append(a)
        raise AssertionError("the model should not pick tools for browser parts")

    async def compose(*a, **k):
        return type("C", (), {"content": "Done.", "tool_calls": []})()

    monkeypatch.setattr(llm, "_complete", no_model)
    parts = llm.split_multi_part("close this tab and play Sati Gatha")
    assert parts == ["close this tab", "play Sati Gatha"]
    monkeypatch.setattr(llm, "_complete", compose)
    asyncio.run(llm._multi_part_pipeline("close this tab and play Sati Gatha", parts, []))
    assert chrome["keys"] == [(C, ord("W"))]
    assert chrome["opened"] == ["https://www.youtube.com/watch?v=3NycM9lYdRI"]


@pytest.mark.parametrize("message", [
    "search for Bode plots",                       # the course library's job
    "search my notes for torque", "find the slides I downloaded yesterday", "play", "pause the music",
    "open chrome", "close chrome", "what is a tabular method", "explain the Coriolis acceleration",
    "look up exercise 1-13", "play the step response",
])
def test_not_browser(chrome, message):
    assert _h(message) is None
    assert chrome["keys"] == [] and chrome["opened"] == []


def test_through_the_pipeline(chrome):
    async def turn():
        return "".join([s async for s in llm.stream_with_tools("close this tab")])
    assert asyncio.run(turn()) == "Closed the tab."
