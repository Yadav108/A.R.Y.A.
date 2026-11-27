"""What the phone gets for when the laptop is out of reach, and what it hands back (arya/server/phone.py)."""
import pytest
from fastapi.testclient import TestClient

from arya.server import phone as P

AUTH = {"X-ARYA": "1"}


@pytest.fixture(autouse=True)
def paired(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "TOKEN_FILE", tmp_path / "phone_token")
    P._state.hosts = {"testserver"}
    monkeypatch.setattr(P, "client_ok", lambda host: True)
    monkeypatch.setattr(P, "_inbox_seen", set())
    AUTH["Authorization"] = f"Bearer {P.token()}"


def test_config_has_city_coordinates_and_the_cloud(monkeypatch):
    monkeypatch.setenv("DEFAULT_CITY", "Schweinfurt")
    monkeypatch.setenv("DEFAULT_LAT", "50.05")
    monkeypatch.setenv("DEFAULT_LON", "10.23")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    cfg = TestClient(P.app).get("/api/offline-config", headers=AUTH).json()
    assert cfg == {"city": "Schweinfurt", "lat": 50.05, "lon": 10.23, "groq_key": "gsk_test",
                   "cloud_model": P.PHONE_CLOUD_MODEL}


def test_config_without_cloud(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(P, "PHONE_CLOUD", False)
    assert "groq_key" not in TestClient(P.app).get("/api/offline-config", headers=AUTH).json()


def test_key_never_logged(monkeypatch):
    from arya.core import audit
    logged = []
    monkeypatch.setattr(audit, "log_event", lambda kind, **kw: logged.append((kind, kw)))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret_value")
    TestClient(P.app).get("/api/offline-config", headers=AUTH)
    assert "gsk_secret_value" not in repr(logged)


def test_unpaired_gets_nothing():
    c = TestClient(P.app)
    assert c.get("/api/offline-config", headers={"X-ARYA": "1"}).status_code == 401
    assert c.post("/api/inbox", json={"items": []}, headers={"X-ARYA": "1"}).status_code == 401


def test_inbox_files_each_note_once(monkeypatch):
    from arya.core import capture
    filed = []

    async def fake_file(text, source="typed", mode=None):
        filed.append((text, source))
        return "your notes"
    monkeypatch.setattr(capture, "file", fake_file)
    items = [{"id": 1, "text": "remember to buy milk"}, {"id": 2, "text": "the parking spot is B12"},
             {"id": 3, "text": "   "}, "junk"]
    c = TestClient(P.app)
    assert c.post("/api/inbox", json={"items": items}, headers=AUTH).json() == {"filed": 2}
    assert c.post("/api/inbox", json={"items": items[:2]}, headers=AUTH).json() == {"filed": 0}     # sent again
    assert filed == [("remember to buy milk", "phone-offline"), ("the parking spot is B12", "phone-offline")]


# ── The offline brain (android/.../PhoneBrain.kt): managed from the laptop too ──────────────────────────────────

@pytest.mark.parametrize("text, do", [
    ("download the offline brain", "download"), ("Hey ARYA, set up the phone AI", "download"),
    ("delete the phone model", "delete"), ("is the offline brain ready?", "status"),
    ("how is the offline brain download going", "status"),
])
def test_phone_brain_action(text, do):
    from arya.server import phone_actions
    a = phone_actions.parse(text)
    assert a["action"] == "phone_brain" and a["do"] == do


@pytest.mark.parametrize("text", ["download the lecture", "what is a phone model", "delete the offline notes"])
def test_not_phone_brain(text):
    from arya.server import phone_actions
    a = phone_actions.parse(text)
    assert a is None or a["action"] != "phone_brain"


def test_typed_brain_command_goes_to_the_app(monkeypatch):
    called = []

    async def turn(text):
        called.append(text)
        return {"reply": "chat", "confirm": False}
    monkeypatch.setattr(P, "_turn", turn)
    r = TestClient(P.app).post("/api/say", headers=AUTH, json={"text": "Download offline brain"}).json()
    assert r["action"] == {"action": "phone_brain", "do": "download"}
    assert called == []                                    # not answered by the chat model
    r = TestClient(P.app).post("/api/say", headers=AUTH, json={"text": "what is a phone model"}).json()
    assert "action" not in r and called == ["what is a phone model"]


def test_phone_reports_its_brain(monkeypatch, capsys):
    monkeypatch.setattr(P, "_last_brain", [""])
    c = TestClient(P.app)
    c.get("/api/offline-config", headers=AUTH, params={"brain": "DOWNLOADING 42% (waiting for Wi-Fi)<script>"})
    c.get("/api/offline-config", headers=AUTH, params={"brain": "DOWNLOADING 42% (waiting for Wi-Fi)<script>"})
    out = capsys.readouterr().out
    assert out.count("Phone: offline brain DOWNLOADING 42% (waiting for Wi-Fi)script") == 1   # logged once, cleaned
