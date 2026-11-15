"""
ARYA as a desktop app: app window + tray icon + status overlay.

    .venv/Scripts/pythonw.exe arya_desktop.py                # open ARYA
    .venv/Scripts/pythonw.exe arya_desktop.py --background   # start quietly in the tray
    .venv/Scripts/python.exe  arya_desktop.py --install-shortcuts

`--install-shortcuts` puts an "ARYA" shortcut on the Desktop and in the Start menu.
A small watchdog (arya/desktop/supervisor.py) starts ARYA and restarts it after a crash or a hang;
`--child` is how the watchdog starts ARYA itself, ARYA_SUPERVISE=0 skips the watchdog.
(`python main.py` still runs the plain console server.)
"""
import os
import sys

if __name__ == "__main__":
    if "--install-shortcuts" in sys.argv:
        from arya.desktop.system import create_shortcuts

        for path in create_shortcuts():
            print(f"Created {path}")
    elif "--child" in sys.argv or os.getenv("ARYA_SUPERVISE", "1") == "0":
        from arya.desktop.app import main

        main([a for a in sys.argv[1:] if a != "--child"])
    else:
        from arya.desktop.supervisor import supervise

        sys.exit(supervise([a for a in sys.argv[1:]]))
