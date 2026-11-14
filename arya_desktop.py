"""
ARYA as a desktop app: app window + tray icon + status overlay.

    .venv/Scripts/pythonw.exe arya_desktop.py                # open ARYA
    .venv/Scripts/pythonw.exe arya_desktop.py --background   # start quietly in the tray
    .venv/Scripts/python.exe  arya_desktop.py --install-shortcuts

`--install-shortcuts` puts an "ARYA" shortcut on the Desktop and in the Start menu.
(`python main.py` still runs the plain console server.)
"""
import sys

if __name__ == "__main__":
    if "--install-shortcuts" in sys.argv:
        from arya.desktop.system import create_shortcuts

        for path in create_shortcuts():
            print(f"Created {path}")
    else:
        from arya.desktop.app import main

        main()
