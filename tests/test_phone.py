"""C5: phone access over home Wi-Fi. The network, the browser and ARYA's answers are faked."""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from arya.core import policy
from arya.server import phone as P

REAL_CLIENT_OK = P.client_ok


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "SETTINGS_FILE", tmp_path / "phone.json")
    monkeypatch.setattr(P, "TOKEN_FILE", tmp_path / "phone_token")
    monkeypatch.setattr(P, "_state", P._State())
    monkeypatch.setattr(P, "open_pairing_page", lambda: None)
    monkeypatch.setattr(P, "where_to_listen", lambda: P.Where("192.168.0.42", "", "Home"))
    P._state.hosts = {"testserver"}
    monkeypatch.setattr(P, "client_ok", lambda host: True)      # TestClient's client is "testclient"


async def _answer(text):
    return {"reply": f"You said {text}", "confirm": False}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(P, "_turn", _answer)
    return TestClient(P.app)


POST = {"X-ARYA": "1"}


def _pair(c):
    return c.post("/api/pair", json={"key": P.token()}, headers=POST)


# ── Pairing and the key ──────────────────────────────────────────────────────

def test_unpaired_phone_gets_nothing(client):
    assert client.get("/").status_code == 200                   # the page itself, with no data in it
    assert client.get("/api/hello").status_code == 401
    assert client.get("/api/feed").status_code == 401
    assert client.post("/api/say", json={"text": "hi"}, headers=POST).status_code == 401


def test_pairing_sets_an_httponly_cookie_and_then_it_works(client):
    r = _pair(client)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/hello").json()["paired"] is True
    assert client.post("/api/say", json={"text": "hi"}, headers=POST).json()["reply"] == "You said hi"


def test_wrong_key_is_refused_and_tries_are_limited(client):
    for _ in range(P.PAIR_FAILS_ALLOWED):
        assert client.post("/api/pair", json={"key": "x" * 43}, headers=POST).status_code == 401
    assert _pair(client).status_code == 429                    # even the right key has to wait now


def test_new_phone_link_locks_out_paired_phones(client):
    _pair(client)
    assert P.handle("new phone link") .startswith("Done")
    assert client.get("/api/hello").status_code == 401


def test_posts_need_the_page_header_and_same_origin(client):
    _pair(client)
    assert client.post("/api/say", json={"text": "hi"}).status_code == 403
    assert client.post("/api/say", json={"text": "hi"},
                       headers={**POST, "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/say", json={"text": "hi"},
                       headers={**POST, "Origin": "http://testserver"}).status_code == 200


def test_foreign_host_and_public_clients_are_refused(client, monkeypatch):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    monkeypatch.setattr(P, "client_ok", REAL_CLIENT_OK)
    assert P.client_ok("192.168.0.7") and P.client_ok("10.0.0.3") and P.client_ok("127.0.0.1")
    assert not P.client_ok("8.8.8.8") and not P.client_ok("testclient") and not P.client_ok(None)


def test_long_and_empty_messages(client):
    _pair(client)
    assert client.post("/api/say", json={"text": "  "}, headers=POST).status_code == 400
    assert client.post("/api/say", json={"text": "a" * (P.MAX_TEXT + 1)}, headers=POST).status_code == 400


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


# ── Nudges ───────────────────────────────────────────────────────────────────

def test_spoken_nudges_reach_the_phone(client):
    _pair(client)
    P.note_event("ARYA_DONE")                                  # not a nudge
    P.note_event("NOTIFY:Timer done! Tea has finished.")
    P.note_event("NOTIFY:Reminder: call Anna")
    items = client.get("/api/feed?after=0").json()["items"]
    assert [i["text"] for i in items] == ["Timer done! Tea has finished.", "Reminder: call Anna"]
    assert client.get(f"/api/feed?after={items[0]['id']}").json()["items"][0]["text"] == "Reminder: call Anna"


# ── Voice ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["connect my phone", "Turn on phone access", "pair my phone please",
                                  "show me the phone QR code"])
def test_turn_on(text):
    assert "Phone access is on" in P.handle(text)
    assert P.enabled()


@pytest.mark.parametrize("text", ["turn off phone access", "disconnect my phone", "disable phone access"])
def test_turn_off(text):
    P.set_enabled(True)
    assert "off" in P.handle(text)
    assert not P.enabled()


def test_status_question_does_not_switch_it():
    assert "off" in P.handle("is phone access on?")
    assert not P.enabled()
    P.set_enabled(True)
    assert "on" in P.handle("is phone access off?") and P.enabled()


def test_public_wifi_is_explained(monkeypatch):
    monkeypatch.setattr(P, "where_to_listen", lambda: P.Where(None, "public", "Cafe"))
    reply = P.handle("connect my phone")
    assert "public network" in reply and "Private" in reply


def test_not_for_phone_access():
    for text in ["call my phone", "where is my phone", "what's on my phone bill", "turn on the lights"]:
        assert P.handle(text) is None


def test_replies_pass_the_false_claim_guard(monkeypatch):
    """From the log: "I've opened a QR code" was replaced by "I haven't actually done that"."""
    from arya.core import llm

    policy.begin_turn("connect my phone")                       # nothing 'acted' this turn
    replies = [P.handle("connect my phone"), P.handle("new phone link"), P.handle("is phone access on?"),
               P.handle("turn off phone access"), P.handle("new phone link")]
    monkeypatch.setattr(P, "where_to_listen", lambda: P.Where(None, "public", "Cafe"))
    replies += [P.handle("connect my phone"), P.handle("is phone access on?")]
    for reply in replies:
        for sentence in re.split(r"(?<=[.!?])\s+", reply):
            assert llm._guard_claim(sentence) == sentence, sentence


def test_from_the_phone_itself():
    P.set_enabled(True)
    assert "laptop" in P.handle("new phone link", remote=True)
    assert "already connected" in P.handle("connect my phone", remote=True)


# ── What the phone may not do ────────────────────────────────────────────────

def test_phone_requests_never_touch_files_clipboard_or_commands():
    from arya.core import llm

    async def go():
        flag = policy.remote_turn.set(True)
        try:
            return [await llm._execute_tool(t, {"path": "x", "command": "dir", "text": "x"}, confirmed=True)
                    for t in ("delete_file", "run_command", "get_clipboard", "read_text_file")]
        finally:
            policy.remote_turn.reset(flag)

    assert all(r.startswith("Not allowed from the phone") for r in asyncio.run(go()))
    assert not policy.blocked_remotely("delete_file")          # at the laptop the normal rules apply


def test_network_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: calls.append(a) or
                        type("R", (), {"stdout": "Public|Vodafone\n"})())
    assert P.network_profile("192.168.0.42") == ("Public", "Vodafone")
    with pytest.raises(ValueError):
        P.network_profile("1.2.3.4'; rm -rf")                     # never reaches the command line
    assert len(calls) == 1
