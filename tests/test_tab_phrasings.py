"""Chrome tabs by voice: the phrasings from the 2026-11-21 check, and typing only into the tab-search popup."""
import asyncio

import pytest

from arya.core import addressee
from arya.tools import browser as B


@pytest.fixture
def keys(monkeypatch):
    pressed = []
    monkeypatch.setattr(B, "_chrome_window", lambda: 7)
    monkeypatch.setattr(B, "_focus", lambda hwnd: True)
    monkeypatch.setattr(B, "_press", lambda *k: pressed.append(k))
    return pressed


@pytest.mark.parametrize("text,shortcut", [
    ("switch tabs", (B.VK_CONTROL, B.VK_TAB)),
    ("change the tab", (B.VK_CONTROL, B.VK_TAB)),
    ("tab to the right", (B.VK_CONTROL, B.VK_TAB)),
    ("switch to the other tab", (B.VK_CONTROL, B.VK_TAB)),
    ("go forward a tab", (B.VK_CONTROL, B.VK_TAB)),
    ("go back a tab", (B.VK_CONTROL, B.VK_SHIFT, B.VK_TAB)),
    ("tab to the left", (B.VK_CONTROL, B.VK_SHIFT, B.VK_TAB)),
    ("second tab", (B.VK_CONTROL, ord("2"))),
    ("the third tab", (B.VK_CONTROL, ord("3"))),
    ("tab 4", (B.VK_CONTROL, ord("4"))),
    ("last tab", (B.VK_CONTROL, ord("9"))),
    ("next tab", (B.VK_CONTROL, B.VK_TAB)),
    ("go to tab 3", (B.VK_CONTROL, ord("3"))),
])
def test_tab_positions(keys, text, shortcut):
    assert asyncio.run(B.handle(text))
    assert keys == [shortcut]


def test_tab_used_before_is_explained_not_guessed(keys):
    reply = asyncio.run(B.handle("go back to the last tab I was on"))
    assert "no shortcut" in reply and keys == []


@pytest.mark.parametrize("text,name", [
    ("open the Gmail tab", "Gmail"),
    ("go to the tab with the PDF", "PDF"),
    ("switch to the tab showing my lecture notes", "lecture notes"),
    ("switch to the Claude tab", "Claude"),
])
def test_tab_names(text, name):
    assert B.named_tab(text) == name


@pytest.mark.parametrize("text", ["open a new tab", "open the new tab", "open the last closed tab", "open the tab",
                                  "is this the right tab?", "second hand shop"])
def test_not_tab_names(text):
    assert B.named_tab(text) is None
    assert not B._NEXT.search(text) or text == "open the tab"


@pytest.mark.parametrize("text", ["switch tab", "second tab", "next Chrome tab", "go back to the previous tab",
                                  "go to the tab with the PDF", "switch to the other tab"])
def test_tab_commands_are_for_arya(text):
    assert addressee.quick_verdict(text) is True


# ── Typing only into the tab-search popup ────────────────────────────────────

def _fake_screen(monkeypatch, foreground):
    typed = []
    monkeypatch.setattr(B, "_type", lambda text: typed.append(text))
    monkeypatch.setattr(B, "_is_chrome", lambda hwnd: True)
    monkeypatch.setattr(B, "TAB_SEARCH_WAIT_S", 0.05)
    monkeypatch.setattr(B, "_foreground", foreground)
    import win32gui
    monkeypatch.setattr(win32gui, "GetWindowText", lambda h: "Inbox - Gmail - Google Chrome")
    return typed


def test_popup_never_opens_nothing_is_typed(monkeypatch, keys):
    typed = _fake_screen(monkeypatch, lambda: 7)           # Chrome's main window stays in front
    reply = B._switch_to_named_tab("gmail")
    assert "didn't type anything" in reply
    assert typed == [] and keys == [(B.VK_CONTROL, B.VK_SHIFT, ord("A"))]      # no Enter either


def test_popup_open_types_and_picks(monkeypatch, keys):
    state = {"fg": 99}                                     # the popup is in front ...

    def press(*k):
        keys.append(k)
        if k == (0x0D,):
            state["fg"] = 7                                # ... until Enter closes it
    monkeypatch.setattr(B, "_press", press)
    typed = _fake_screen(monkeypatch, lambda: state["fg"])
    assert B._switch_to_named_tab("gmail") == "Switched to “Inbox - Gmail”."
    assert typed == ["gmail"] and keys[-1] == (0x0D,)


def test_no_match_closes_the_popup(monkeypatch, keys):
    typed = _fake_screen(monkeypatch, lambda: 99)          # Enter doesn't close it: nothing matched
    monkeypatch.setattr(B, "_wait_for", lambda test, s: 99 if test(99) else None)
    reply = B._switch_to_named_tab("taal")
    assert "couldn't find a tab matching taal" in reply
    assert keys[-2:] == [(0x0D,), (0x1B,)] and typed == ["taal"]


def test_popup_closed_while_typing_no_enter(monkeypatch, keys):
    seq = iter([99] + [7] * 50)
    typed = _fake_screen(monkeypatch, lambda: next(seq))
    reply = B._switch_to_named_tab("gmail")
    assert "didn't press Enter" in reply and (0x0D,) not in keys and typed == ["gmail"]
