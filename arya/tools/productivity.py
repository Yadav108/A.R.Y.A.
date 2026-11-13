import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pyperclip


_notify_callback: Callable[[str], Awaitable[None]] | None = None


def set_notify_callback(callback) -> None:
    global _notify_callback
    _notify_callback = callback


async def _notify(text: str) -> None:
    if _notify_callback is not None:
        await _notify_callback(text)


# ── Registry of everything scheduled (timers, reminders, pomodoros) ──────────

@dataclass
class _Scheduled:
    kind: str          # "timer" | "reminder" | "pomodoro"
    label: str
    ends_at: float     # time.time() when it finishes
    task: asyncio.Task


_SCHEDULED: list[_Scheduled] = []


def _register(kind: str, label: str, seconds: float, coro) -> _Scheduled:
    task = asyncio.create_task(coro)
    item = _Scheduled(kind=kind, label=label, ends_at=time.time() + seconds, task=task)
    _SCHEDULED.append(item)
    task.add_done_callback(lambda _t: _SCHEDULED.remove(item) if item in _SCHEDULED else None)
    return item


def _speak_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


# ── Timers ────────────────────────────────────────────────────────────────────

async def _timer_worker(duration_seconds: int, label: str) -> None:
    await asyncio.sleep(duration_seconds)
    print(f"TIMER DONE: {label}")
    await _notify(f"Timer done! {label} has finished.")


async def set_timer(duration_seconds: int, label: str = "Timer") -> str:
    if duration_seconds <= 0:
        return "Invalid timer duration. Use a positive number of seconds."

    _register("timer", label, duration_seconds, _timer_worker(duration_seconds, label))
    return f"Timer set for {_speak_duration(duration_seconds)}. Label: {label}"


# ── Reminders ─────────────────────────────────────────────────────────────────

def _parse_clock(at_time: str, now: datetime) -> datetime | None:
    """Accept '17:30', '5:30 pm', '5pm', '17.30', '530' (how Whisper writes
    "five thirty"). Without am/pm, picks the next upcoming occurrence — at
    16:00, '5:30' means 17:30 today, not 05:30 tomorrow."""
    text = at_time.strip().lower().replace("a.m.", "am").replace("p.m.", "pm").replace(".", ":")
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3) or ""
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    def upcoming(h: int) -> datetime:
        t = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)

    target = upcoming(hour)
    if not meridiem and 1 <= hour <= 11:
        target = min(target, upcoming(hour + 12))
    return target


async def _reminder_worker(seconds: float, message: str) -> None:
    await asyncio.sleep(seconds)
    print(f"REMINDER: {message}")
    await _notify(f"Reminder: {message}")


async def set_reminder(message: str, at_time: str | None = None, in_minutes: float | None = None) -> str:
    message = message.strip()
    if not message:
        return "What should I remind you about?"

    now = datetime.now()
    if at_time:
        target = _parse_clock(at_time, now)
        if target is None:
            return f"I couldn't understand the time '{at_time}'. Use a time like 17:30."
    elif in_minutes is not None and in_minutes > 0:
        target = now + timedelta(minutes=in_minutes)
    else:
        return "When should I remind you? Give a clock time or a number of minutes."

    seconds = (target - now).total_seconds()
    _register("reminder", message, seconds, _reminder_worker(seconds, message))
    day = "tomorrow " if target.date() > now.date() else ""
    return f"Reminder set for {day}{target.strftime('%H:%M')}, in {_speak_duration(seconds)}: {message}"


async def list_timers() -> str:
    if not _SCHEDULED:
        return "No active timers or reminders."
    now = time.time()
    lines = [
        f"{item.kind.capitalize()} '{item.label}': {_speak_duration(item.ends_at - now)} left"
        for item in sorted(_SCHEDULED, key=lambda i: i.ends_at)
    ]
    return "; ".join(lines) + "."


async def cancel_timer(label: str | None = None) -> str:
    if not _SCHEDULED:
        return "There's nothing to cancel."
    needle = (label or "").strip().lower()
    targets = [i for i in _SCHEDULED if not needle or needle in i.label.lower() or needle == i.kind]
    if not targets:
        names = ", ".join(i.label for i in _SCHEDULED)
        return f"No timer matches '{label}'. Active: {names}."
    for item in list(targets):
        item.task.cancel()
        if item in _SCHEDULED:
            _SCHEDULED.remove(item)
    names = ", ".join(f"{i.kind} '{i.label}'" for i in targets)
    return f"Cancelled {names}."


# ── Clipboard ─────────────────────────────────────────────────────────────────

async def get_clipboard() -> str:
    try:
        content = pyperclip.paste()
    except Exception as exc:
        raise RuntimeError(f"Failed to read clipboard: {exc}") from exc

    if not content:
        return "Clipboard is empty."

    if len(content) > 200:
        content = content[:200] + "..."
    return f"Clipboard contains: {content}"


async def set_clipboard(text: str) -> str:
    try:
        pyperclip.copy(text)
    except Exception as exc:
        raise RuntimeError(f"Failed to copy to clipboard: {exc}") from exc

    return f"Copied to clipboard: {text[:50]}..."


# ── Pomodoro ──────────────────────────────────────────────────────────────────

async def _pomodoro_worker(sessions: int, label: str) -> None:
    for session in range(sessions):
        await _notify(f"Pomodoro session {session + 1} started. Focus for 25 minutes, {label}.")
        await asyncio.sleep(750)
        await _notify(f"Halfway through session {session + 1}. Keep going, {label}!")
        await asyncio.sleep(450)
        await _notify(f"5 minutes remaining in session {session + 1}. Stay focused!")
        await asyncio.sleep(300)
        await _notify(
            f"Session {session + 1} focus complete! You studied {label} for 25 minutes. Take a 5 minute break."
        )
        await asyncio.sleep(300)
        if session == sessions - 1:
            await _notify(f"All {sessions} pomodoro sessions complete. Great work!")
        else:
            await _notify(f"Break over. Starting session {session + 2}.")


async def pomodoro(sessions: int = 1, label: str = "Study") -> str:
    if sessions <= 0:
        return "Invalid pomodoro sessions. Use a positive number of sessions."

    _register("pomodoro", label, sessions * 30 * 60, _pomodoro_worker(sessions, label))
    return (
        f"Pomodoro started: {sessions} session(s) of 25 min focus + 5 min break. "
        f"Topic: {label}"
    )
