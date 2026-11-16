"""Media playback and system volume via Windows virtual media keys.

Works with whatever app currently owns media focus (Spotify, YouTube in a
browser, VLC, ...) — the same as pressing the keyboard's media keys.
"""
import asyncio

import win32api

# Windows virtual-key codes (winuser.h) — not all are exported by win32con.
_KEYEVENTF_KEYUP = 0x0002
_VK = {
    "play_pause": 0xB3,   # VK_MEDIA_PLAY_PAUSE
    "next": 0xB0,         # VK_MEDIA_NEXT_TRACK
    "previous": 0xB1,     # VK_MEDIA_PREV_TRACK
    "stop": 0xB2,         # VK_MEDIA_STOP
    "volume_up": 0xAF,    # VK_VOLUME_UP
    "volume_down": 0xAE,  # VK_VOLUME_DOWN
    "mute": 0xAD,         # VK_VOLUME_MUTE
}

_SPOKEN = {
    "play_pause": "Toggled play/pause.",
    "next": "Skipped to the next track.",
    "previous": "Went back to the previous track.",
    "stop": "Stopped playback.",
    "mute": "Toggled mute.",
}

# Common phrasings the model might send instead of the enum value. "play" / "resume" and "pause" are real
# play and pause (see media_control), not the toggle key — from the log: "resume the song", then "play",
# then "play the song" each toggled, so the second one paused the music again.
_ALIASES = {
    "resume": "play", "unpause": "play", "continue": "play", "toggle": "play_pause",
    "skip": "next", "next_track": "next", "previous_track": "previous", "back": "previous",
    "louder": "volume_up", "up": "volume_up", "quieter": "volume_down", "down": "volume_down",
    "unmute": "mute",
}


def _press(vk: int, times: int = 1) -> None:
    for _ in range(times):
        win32api.keybd_event(vk, 0, 0, 0)
        win32api.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


async def media_control(action: str, steps: int = 5) -> str:
    from arya.tools.music import forget, paused_by_arya, set_playing
    key = action.strip().lower().replace(" ", "_").replace("-", "_")
    key = _ALIASES.get(key, key)
    if key in ("play", "pause", "play_music"):
        play = key != "pause"
        # "Hey ARYA" paused the music while you talk: that session is the one you mean.
        app = paused_by_arya()
        forget()                                # you're controlling the music: ARYA won't resume it later
        done = await set_playing(play, app)
        if done == "played":
            return "Playing."
        if done == "paused":
            return "Paused."
        if done == "already":
            return "It's already playing." if play else "It's already paused."
        key = "play_pause"                      # no media controls: fall back to the keyboard's toggle key
    forget()                                    # you're controlling the music: ARYA won't resume it later
    vk = _VK.get(key)
    if vk is None:
        return f"Unknown media action '{action}'. Use one of: play, pause, {', '.join(_VK)}."

    if key in ("volume_up", "volume_down"):
        steps = max(1, min(int(steps), 50))  # each press is 2%
        await asyncio.to_thread(_press, vk, steps)
        direction = "up" if key == "volume_up" else "down"
        return f"Turned the volume {direction} by about {steps * 2} percent."

    await asyncio.to_thread(_press, vk)
    return _SPOKEN[key]
