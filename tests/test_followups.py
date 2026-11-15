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


# ── 2026-11-16: ARYA asked for a song, "classic music." went to the model ("Playing some classic music for you.")

def test_the_song_ARYA_asked_for(chrome):
    assert _h("play any music.").endswith("Tell me a song or an artist and I'll put it on.")
    assert B.awaiting_song()
    assert _h("classic music.") == "Playing classic music on YouTube."
    assert chrome["opened"][-1] == "https://www.youtube.com/watch?v=VIDEOID0000"
    assert not B.awaiting_song()                                   # one answer only
    assert _h("classic music.") is None                            # later, it's not a song request any more


@pytest.mark.parametrize("reply", ["what's the weather like?", "no, never mind", "set a timer for 5 minutes",
                                   "pause", "explain the Coriolis acceleration"])
def test_a_new_request_is_not_taken_as_the_song(chrome, reply):
    _h("any music")
    assert _h(reply) is None and not B.awaiting_song()


def test_song_wait_expires(chrome, monkeypatch):
    _h("any music")
    monkeypatch.setattr(B, "_awaiting_song_until", 0.0)
    assert _h("Kishore Kumar") is None


@pytest.mark.parametrize("claim", ["Playing some classic music for you.", "Now playing your favourite song.",
                                   "I'm playing it now.", "I've put on some jazz."])
def test_playing_claims_need_a_real_action(claim):
    import arya.core.llm as llm
    from arya.core import policy
    policy.begin_turn("x")
    assert llm._guard_claim(claim) == llm._NOT_DONE


def test_real_playing_replies_pass():
    import arya.core.llm as llm
    from arya.core import policy
    policy.begin_turn("x")
    assert llm._guard_claim("Playing: Weightless by Marconi Union, in Spotify.").startswith("Playing:")
    policy.note_result("open_url")
    assert llm._guard_claim("Playing classic music on YouTube.") == "Playing classic music on YouTube."


# ── "increase the volume by 30 percent." then "Increase it to more 30 percent." (went to the model, 2.6 s)

@pytest.fixture
def after_volume_up(monkeypatch):
    import arya.core.llm as llm
    monkeypatch.setattr(llm, "_last_volume", {"action": "", "until": 0.0})
    llm.note_media({"action": "volume_up", "steps": 15})
    return llm


@pytest.mark.parametrize("said, expected", [
    ("Increase it to more 30 percent.", {"action": "volume_up", "steps": 15}),
    ("louder again", {"action": "volume_up", "steps": 5}),
    ("more", {"action": "volume_up", "steps": 5}),
    ("a bit more", {"action": "volume_up", "steps": 3}),
    ("again", {"action": "volume_up", "steps": 5}),
    ("a bit less", {"action": "volume_down", "steps": 3}),
    ("lower it by 10 percent", {"action": "volume_down", "steps": 5}),
    ("tell me more", None), ("more about the Coriolis force", None), ("what is it", None),
])
def test_volume_follow_ups(after_volume_up, said, expected):
    assert after_volume_up.contextual_media_command(said) == expected


def test_volume_follow_ups_only_right_after_a_volume_change(after_volume_up, monkeypatch):
    llm = after_volume_up
    monkeypatch.setattr(llm, "_last_volume", {"action": "volume_up", "until": 0.0})      # 90 s passed
    assert llm.contextual_media_command("more") is None
    assert llm.contextual_media_command("louder again") is None


def test_volume_follow_up_counts_as_for_arya(after_volume_up):
    assert addressee.quick_verdict("Increase it to more 30 percent.") is True
