"""
Music (Phase B), through Windows' own media controls — works with whatever plays: YouTube in the
browser, Spotify, the Media Player app.

  "What's playing?" / "What song is this?"  -> title, artist and app
  "Play focus music"                        -> opens ARYA_FOCUS_MUSIC_URL (your playlist or stream)
  While you talk to ARYA                    -> music pauses the moment "Hey ARYA" is heard (so the
                                               microphone hears you, not the song) and resumes a few
                                               seconds after the answer. ARYA_PAUSE_MUSIC=0 turns it off.

If you pause or stop the music yourself ("ARYA, pause the music"), ARYA doesn't resume it.
"""
from __future__ import annotations

import asyncio
import os
import re

PLAYING, PAUSED = 4, 5                 # GlobalSystemMediaTransportControlsSessionPlaybackStatus
RESUME_AFTER_S = 3.0
WATCHDOG_S = 20.0                      # "Hey ARYA" but no request: music back after this

_paused_by_arya: str | None = None     # app id of the session ARYA paused
_resume_task: asyncio.Task | None = None


async def _manager():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    return await M.request_async()


def _app_name(app_id: str) -> str:
    a = app_id.lower()
    for key, name in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
                      ("zune", "Media Player"), ("mediaplayer", "Media Player"), ("vlc", "VLC"),
                      ("brave", "Brave"), ("opera", "Opera")):
        if key in a:
            return name
    return re.sub(r"[!_.].*$", "", app_id) or "an app"


async def now_playing() -> dict | None:
    """{'title', 'artist', 'app', 'playing'} for the current media session, or None."""
    try:
        session = (await _manager()).get_current_session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        status = int(session.get_playback_info().playback_status)
        return {"title": props.title or "", "artist": props.artist or "",
                "app": _app_name(session.source_app_user_model_id or ""), "playing": status == PLAYING}
    except Exception as exc:
        print(f"Music: media controls unavailable ({type(exc).__name__})")
        return None


# ── Pause while ARYA listens ─────────────────────────────────────────────────

def enabled() -> bool:
    return os.getenv("ARYA_PAUSE_MUSIC", "1") != "0"


async def duck(busy=lambda: False, watchdog_s: float = WATCHDOG_S) -> None:
    """'Hey ARYA' heard: pause whatever is playing (and remember that ARYA did it).
    If no answer follows (a false wake, a cough), the watchdog brings the music back."""
    global _paused_by_arya, _resume_task
    if _resume_task and not _resume_task.done():
        _resume_task.cancel()                          # a new request before the music came back
    if not enabled():
        return
    if not _paused_by_arya:                            # (already paused by an earlier "Hey ARYA": keep it)
        try:
            session = (await _manager()).get_current_session()
            if session is not None and int(session.get_playback_info().playback_status) == PLAYING:
                if await session.try_pause_async():
                    _paused_by_arya = session.source_app_user_model_id or "?"
                    print(f"Music: paused {_app_name(_paused_by_arya)} while you talk")
        except Exception as exc:
            print(f"Music: couldn't pause ({type(exc).__name__})")
            return

    async def watchdog():
        await asyncio.sleep(watchdog_s)
        if _paused_by_arya and not busy():             # no turn running: nothing will resume it
            await _resume_now()

    if _paused_by_arya:
        _resume_task = asyncio.create_task(watchdog())


def forget() -> None:
    """You controlled the music yourself — don't resume it behind your back."""
    global _paused_by_arya
    _paused_by_arya = None
    if _resume_task and not _resume_task.done():
        _resume_task.cancel()


async def _resume_now() -> None:
    global _paused_by_arya
    app, _paused_by_arya = _paused_by_arya, None
    if not app:
        return
    try:
        for session in (await _manager()).get_sessions():
            if (session.source_app_user_model_id or "?") == app:
                if int(session.get_playback_info().playback_status) == PAUSED:
                    await session.try_play_async()
                    print(f"Music: resumed {_app_name(app)}")
                return
    except Exception as exc:
        print(f"Music: couldn't resume ({type(exc).__name__})")


def resume_later(delay: float = RESUME_AFTER_S) -> None:
    """After ARYA's answer: bring the music back in a moment (cancelled by a new 'Hey ARYA')."""
    global _resume_task
    if not _paused_by_arya:
        return
    if _resume_task and not _resume_task.done():
        _resume_task.cancel()                          # replaces the watchdog

    async def later():
        await asyncio.sleep(delay)
        await _resume_now()

    _resume_task = asyncio.create_task(later())


# ── Voice ─────────────────────────────────────────────────────────────────────

_WHAT = re.compile(r"\bwhat(?:'s| is)\s+(?:this\s+)?(?:playing|song|track)\b|\bwhat song is (?:this|playing)\b|"
                   r"\bwho (?:sings|is singing|is this)\b|\bname of (?:this|the) song\b|\bwhat am i listening to\b",
                   re.IGNORECASE)
_FOCUS = re.compile(r"\bplay\s+(?:some\s+|my\s+)?(?:focus|study|studying|concentration|lo-?fi|background)\s+"
                    r"(?:music|playlist|beats|sounds)\b|\bplay\s+(?:some\s+|my\s+)?lo-?fi\b", re.IGNORECASE)
FOCUS_HELP = ("I don't know your focus music yet. Put the link of your favourite playlist or stream in ARYA's .env "
              "file as ARYA_FOCUS_MUSIC_URL, and I can play it whenever you ask.")


async def handle(message: str) -> str | None:
    if _WHAT.search(message):
        now = await now_playing()
        if not now or not (now["title"] or now["artist"]):
            return "Nothing is playing right now."
        what = now["title"] or "something"
        by = f" by {now['artist']}" if now["artist"] else ""
        state = "Playing" if now["playing"] else "Paused"
        return f"{state}: {what}{by}, in {now['app']}."
    if _FOCUS.search(message):
        url = os.getenv("ARYA_FOCUS_MUSIC_URL", "").strip()
        if not url:
            return FOCUS_HELP
        from arya.core import policy
        from arya.tools.os_tools import open_url
        forget()
        result = await open_url(url)
        if result.startswith("Failed"):
            return "I couldn't open your focus music."
        policy.note_result("open_url")
        return "Opening your focus music."
    return None
