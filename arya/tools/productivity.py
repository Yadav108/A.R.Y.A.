import asyncio
from collections.abc import Awaitable, Callable

import pyperclip


_ACTIVE_TIMERS: set[asyncio.Task] = set()
_ACTIVE_POMODOROS: set[asyncio.Task] = set()
_notify_callback: Callable[[str], Awaitable[None]] | None = None


def set_notify_callback(callback) -> None:
    global _notify_callback
    _notify_callback = callback


async def _timer_worker(duration_seconds: int, label: str) -> None:
    try:
        await asyncio.sleep(duration_seconds)
        print(f"TIMER DONE: {label}")
        print("\a" * 3)
        if _notify_callback is not None:
            await _notify_callback(f"Timer done! {label} has finished.")
    finally:
        _ACTIVE_TIMERS.discard(asyncio.current_task())


async def set_timer(duration_seconds: int, label: str = "Timer") -> str:
    if duration_seconds <= 0:
        return "Invalid timer duration. Use a positive number of seconds."

    task = asyncio.create_task(_timer_worker(duration_seconds, label))
    _ACTIVE_TIMERS.add(task)
    return f"Timer set for {duration_seconds} seconds. Label: {label}"


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


async def _pomodoro_worker(sessions: int, label: str) -> None:
    try:
        for session in range(sessions):
            if _notify_callback is not None:
                await _notify_callback(
                    f"Pomodoro session {session + 1} started. Focus for 25 minutes, {label}."
                )

            await asyncio.sleep(750)

            if _notify_callback is not None:
                await _notify_callback(
                    f"Halfway through session {session + 1}. Keep going, {label}!"
                )

            await asyncio.sleep(450)

            if _notify_callback is not None:
                await _notify_callback(
                    f"5 minutes remaining in session {session + 1}. Stay focused!"
                )

            await asyncio.sleep(300)

            if _notify_callback is not None:
                await _notify_callback(
                    f"Session {session + 1} focus complete! You studied {label} for 25 minutes. Take a 5 minute break."
                )

            await asyncio.sleep(300)

            if _notify_callback is not None:
                if session == sessions - 1:
                    await _notify_callback(
                        f"All {sessions} pomodoro sessions complete. Great work!"
                    )
                else:
                    await _notify_callback(
                        f"Break over. Starting session {session + 2}."
                    )
    finally:
        _ACTIVE_POMODOROS.discard(asyncio.current_task())


async def pomodoro(sessions: int = 1, label: str = "Study") -> str:
    if sessions <= 0:
        return "Invalid pomodoro sessions. Use a positive number of sessions."

    task = asyncio.create_task(_pomodoro_worker(sessions, label))
    _ACTIVE_POMODOROS.add(task)
    return (
        f"Pomodoro started: {sessions} session(s) of 25 min focus + 5 min break. "
        f"Topic: {label}"
    )
