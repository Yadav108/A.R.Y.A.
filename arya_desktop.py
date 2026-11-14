"""
ARYA as a desktop app: tray icon + status overlay, no browser or console needed.

    .venv\\Scripts\\pythonw.exe arya_desktop.py

Tick "Start with Windows" in the tray menu to launch it automatically at login.
(`python main.py` still runs the plain console server.)
"""
from arya.desktop.app import main

if __name__ == "__main__":
    main()
