"""
Housekeeping (roadmap C5): a daily backup of what ARYA knows about you, and starting with Windows.

Backup: once a day (at startup if today's is missing, then checked hourly) ARYA copies arya.db — history,
reminders, practice cards, exams, calendar events, routines, what it remembers about you — plus your
corrections, photo notes and handwriting transcriptions into %LOCALAPPDATA%\\ARYA\\backups\\<date>\\, keeping
the last 7 days. The copy is made with SQLite's backup API, so it's consistent even while ARYA is writing.
The knowledge library and lecture transcripts are not copied (they can be rebuilt from your course files).

Voice: "back up now", "when was the last backup?", "start ARYA with Windows", "don't start with Windows".
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

KEEP_DAYS = 7


def backup_dir() -> Path:
    from arya.knowledge.library import DATA_DIR
    return DATA_DIR / "backups"


def backups() -> list[Path]:
    root = backup_dir()
    return sorted((p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d\d-\d\d", p.name)),
                  key=lambda p: p.name) if root.exists() else []


def back_up(today: date | None = None) -> Path:
    """Today's backup (made now, replacing an earlier one from today); older than KEEP_DAYS ones removed."""
    from arya.knowledge import library
    from arya.memory.database import DB_PATH

    day = (today or date.today()).isoformat()
    target = backup_dir() / day
    target.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(target / "arya.db")
    try:
        src.backup(dst)                                       # consistent copy while ARYA keeps writing
    finally:
        dst.close()                                           # closed, or Windows keeps the file locked
        src.close()
    for f in (library.CORRECTIONS, library.PHOTO_NOTES, library.TRANSCRIPTIONS):
        if Path(f).exists():
            shutil.copy2(f, target / Path(f).name)
    for old in backups()[:-KEEP_DAYS]:
        shutil.rmtree(old, ignore_errors=True)                # only ARYA's own dated backup folders
    print(f"Backup: saved {target}")
    return target


def backed_up_today(today: date | None = None) -> bool:
    day = (today or date.today()).isoformat()
    return any(p.name == day for p in backups())


async def _refit_listening() -> None:
    """Once a day: refit "was that for me?" on everything labelled so far — each "It was for you" and
    "that wasn't for you" since yesterday included (arya/research/addressee_model.py). Local only."""
    try:
        from arya.research import addressee_data, addressee_model
        await asyncio.to_thread(addressee_data.main)          # rebuild the dataset from the log
        print("Listening: " + await asyncio.to_thread(addressee_model.retrain))
    except Exception as exc:
        print(f"Listening: refit skipped ({type(exc).__name__}: {exc})")


async def run() -> None:
    """Started by the server: back up once a day."""
    await asyncio.sleep(120)                                  # let startup finish first
    while True:
        try:
            if not backed_up_today():
                await asyncio.to_thread(back_up)
                await _refit_listening()
        except Exception as exc:
            print(f"Backup failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(3600)


_BACKUP_NOW = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:(?:can|could) you\s+)?(?:back ?up|make a backup|"
                         r"save a backup|do a backup|create a backup)(?:\s+(?:now|my data|everything|the data|"
                         r"my stuff))?(?:\s+please)?\W*$", re.IGNORECASE)
_LAST = re.compile(r"\b(?:when|what)\b.{0,20}\b(?:last|latest)\s+back ?up\b|\bbackups?\s+(?:status|done)\b", re.IGNORECASE)
_AUTOSTART_ON = re.compile(r"\b(?:start|launch|open|run)\s+(?:arya\s+|yourself\s+)?(?:automatically\s+)?(?:with|when|at)\s+"
                           r"(?:windows|login|log ?in|startup|boot|i (?:log in|start (?:the|my) (?:laptop|computer|pc)))\b|"
                           r"\bautostart on\b|\bturn on autostart\b", re.IGNORECASE)
_AUTOSTART_OFF = re.compile(r"\b(?:don'?t|do not|stop|never)\s+(?:start|launch|open|run)(?:ing)?\s+(?:arya\s+|yourself\s+)?"
                            r"(?:automatically\s+)?(?:with|when|at)\s+(?:windows|login|log ?in|startup|boot)\b|"
                            r"\bautostart off\b|\bturn off autostart\b", re.IGNORECASE)


def handle(message: str) -> str | None:
    if _AUTOSTART_OFF.search(message):
        from arya.desktop import system
        system.set_autostart(False)
        return "Okay, I won't start with Windows any more. Start me from the desktop shortcut."
    if _AUTOSTART_ON.search(message):
        from arya.desktop import system
        system.set_autostart(True)
        return "Done, I'll start with Windows from now on, quietly in the tray."
    if _LAST.search(message):
        found = backups()
        if not found:
            return "There's no backup yet. Say \"back up now\" and I'll make one."
        last = found[-1]
        when = datetime.fromtimestamp((last / "arya.db").stat().st_mtime) if (last / "arya.db").exists() else None
        return (f"The last backup is from {when:%A at %H:%M}" if when else f"The last backup is from {last.name}") + \
            f"; I keep the last {KEEP_DAYS} days in your ARYA folder."
    if _BACKUP_NOW.search(message):
        path = back_up()
        return f"Backed up: your history, reminders, practice cards, routines and what I remember about you, " \
               f"in {path.parent.name}\\{path.name}."
    return None
