"""
Windows integration: start with Windows, one ARYA at a time.
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "arya_desktop.py"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE = "ARYA"
_MUTEX_NAME = "Local\\ARYA-voice-assistant"
_mutex = None


def _command() -> str:
    """pythonw = no console window."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{LAUNCHER}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Per-user Run key — no admin rights, removed cleanly when switched off."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE)
            except FileNotFoundError:
                pass


def acquire_single_instance() -> bool:
    """False if another ARYA is already running (it owns the mic and the port)."""
    global _mutex
    import win32api
    import win32event
    import winerror

    _mutex = win32event.CreateMutex(None, False, _MUTEX_NAME)
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
