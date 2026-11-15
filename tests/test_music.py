"""Phase B: music — "what's playing?", focus music, and pausing the music while you talk to ARYA.
Windows' media controls are faked; nothing really plays or pauses."""
import asyncio
import types

import pytest

from arya.tools import music as M


class FakeSession:
    def __init__(self, app="Spotify.exe", title="Weightless", artist="Marconi Union", status=M.PLAYING):
        self.source_app_user_model_id, self.title, self.artist, self.status = app, title, artist, status
        self.calls = []

    def get_playback_info(self):
        return types.SimpleNamespace(playback_status=self.status)

    async def try_get_media_properties_async(self):
        return types.SimpleNamespace(title=self.title, artist=self.artist)

    async def try_pause_async(self):
        self.calls.append("pause")
        self.status = M.PAUSED
        return True

    async def try_play_async(self):
        self.calls.append("play")
        self.status = M.PLAYING
        return True


@pytest.fixture
def media(monkeypatch):
    holder = {"session": FakeSession()}

    async def manager():
        return types.SimpleNamespace(get_current_session=lambda: holder["session"],
                                     get_sessions=lambda: [holder["session"]] if holder["session"] else [])

    monkeypatch.setattr(M, "_manager", manager)
    monkeypatch.setenv("ARYA_PAUSE_MUSIC", "1")
    M.forget()
    yield holder
    M.forget()


def test_whats_playing(media):
    assert asyncio.run(M.handle("what's playing?")) == "Playing: Weightless by Marconi Union, in Spotify."
    media["session"].status = M.PAUSED
    assert asyncio.run(M.handle("what song is this")) == "Paused: Weightless by Marconi Union, in Spotify."
    media["session"] = FakeSession(app="MSEdge", title="Lofi beats to study to", artist="")
    assert asyncio.run(M.handle("what am I listening to")) == "Playing: Lofi beats to study to, in Edge."
    media["session"] = None
    assert asyncio.run(M.handle("what's playing")) == "Nothing is playing right now."


def test_paused_while_you_talk_and_resumed_after(media):
    async def go():
        s = media["session"]
        await M.duck()
        assert s.calls == ["pause"]
        M.resume_later(delay=0.01)
        await asyncio.sleep(0.05)
        return s
    s = asyncio.run(go())
    assert s.calls == ["pause", "play"] and s.status == M.PLAYING


def test_not_resumed_if_you_paused_it_yourself(media):
    async def go():
        await M.duck()
        M.forget()                                  # "ARYA, pause the music" -> media_control
        M.resume_later(delay=0.01)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert media["session"].calls == ["pause"]


def test_paused_music_is_left_alone(media):
    media["session"].status = M.PAUSED
    async def go():
        await M.duck()
        M.resume_later(delay=0.01)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert media["session"].calls == []             # it wasn't playing: don't start it


def test_false_wake_watchdog_brings_music_back(media):
    async def go():
        await M.duck(busy=lambda: False, watchdog_s=0.02)
        await asyncio.sleep(0.08)
    asyncio.run(go())
    assert media["session"].calls == ["pause", "play"]


def test_watchdog_waits_for_a_running_answer(media):
    async def go():
        await M.duck(busy=lambda: True, watchdog_s=0.02)
        await asyncio.sleep(0.08)
    asyncio.run(go())
    assert media["session"].calls == ["pause"]        # the turn's end resumes it, not the watchdog


def test_switched_off(media, monkeypatch):
    monkeypatch.setenv("ARYA_PAUSE_MUSIC", "0")
    asyncio.run(M.duck())
    assert media["session"].calls == []


def test_focus_music(media, monkeypatch):
    monkeypatch.delenv("ARYA_FOCUS_MUSIC_URL", raising=False)
    assert asyncio.run(M.handle("play some focus music")) == M.FOCUS_HELP
    opened = []

    async def fake_open(url):
        opened.append(url)
        return f"Opened {url}"
    import arya.tools.os_tools as os_tools
    monkeypatch.setattr(os_tools, "open_url", fake_open)
    monkeypatch.setenv("ARYA_FOCUS_MUSIC_URL", "https://example.com/lofi")
    assert asyncio.run(M.handle("play lofi")) == "Opening your focus music."
    assert opened == ["https://example.com/lofi"]


@pytest.mark.parametrize("message", ["play", "pause the music", "next song", "what is a playing field",
                                     "explain the Coriolis acceleration", "play the step response"])
def test_not_music_questions(media, message):
    assert asyncio.run(M.handle(message)) is None
