"""Window control and the false-claim guard ("Window minimized to the taskbar." when nothing ran).
Windows are faked: nothing on screen moves."""
import asyncio

import pytest

import arya.core.llm as llm
from arya.core import addressee, policy
from arya.tools import windows as W


@pytest.fixture
def screen(monkeypatch):
    state = {"apps": {"chrome": [11, 12], "word": [21]}, "front": 99, "shown": [], "desktop": 0}
    monkeypatch.setattr(W, "_app_windows", lambda app: state["apps"].get(app.lower(), []))
    monkeypatch.setattr(W, "_foreground", lambda: state["front"])
    monkeypatch.setattr(W, "_show", lambda hwnds, mode: state["shown"].append((list(hwnds), mode)))
    monkeypatch.setattr(W, "_show_desktop", lambda: state.__setitem__("desktop", state["desktop"] + 1))
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    return state


@pytest.mark.parametrize("message, reply, shown", [
    ("minimize the Chrome window.", "Minimized Chrome.", ([11, 12], W.SW_MINIMIZE)),
    ("maximise Word", "Maximized Word.", ([21], W.SW_MAXIMIZE)),
    ("restore the chrome browser", "Restored Chrome.", ([11, 12], W.SW_RESTORE)),
    ("minimize this window", "Minimized the window.", ([99], W.SW_MINIMIZE)),
])
def test_window_commands(screen, message, reply, shown):
    assert asyncio.run(W.handle(message)) == reply
    assert screen["shown"] == [shown]


def test_missing_app_and_desktop(screen):
    assert asyncio.run(W.handle("minimize spotify")) == "I can't find an open spotify window."
    assert asyncio.run(W.handle("show me the desktop")) == "Showing the desktop." and screen["desktop"] == 1
    screen["front"] = None
    assert asyncio.run(W.handle("minimize this window")).startswith("Which window?")


@pytest.mark.parametrize("message", ["how do I maximize the profit of a small company",
                                     "explain how to minimize the cost function", "restore"])
def test_not_window_commands(screen, message):
    assert asyncio.run(W.handle(message)) is None


def test_the_exact_live_failure_now_works(screen):
    async def turn():
        return "".join([s async for s in llm.stream_with_tools("minimize the Chrome window.")])
    assert asyncio.run(turn()) == "Minimized Chrome."


@pytest.mark.parametrize("claim", ["Window minimized to the taskbar.", "The tab is now closed.",
                                   "Chrome window maximized.", "I've minimized it for you.",
                                   "Your music is paused.", "Volume muted."])
def test_terse_false_claims_are_blocked(claim):
    policy.begin_turn("x")
    assert llm._guard_claim(claim) == llm._NOT_DONE


@pytest.mark.parametrize("fine", ["Minimizing a cost function means finding where the gradient is zero.",
                                  "A closed loop system feeds the output back.", "Closed the tab.",
                                  "Paused: Weightless by Marconi Union, in Spotify.",
                                  "Should I close the window?"])
def test_ordinary_sentences_pass(fine):
    policy.begin_turn("x")
    assert llm._guard_claim(fine) == fine


def test_claims_pass_when_the_action_ran():
    policy.begin_turn("x")
    policy.note_result("window_control")
    assert llm._guard_claim("Window minimized.") == "Window minimized."


@pytest.mark.parametrize("command", ["minimize the Chrome window.", "reopen the tab", "google bode plots",
                                     "tidy my downloads", "summarise my emails", "new tab"])
def test_new_commands_are_for_arya(command):
    assert addressee.quick_verdict(command) is True
