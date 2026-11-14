import asyncio
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path


# Only these read-only, informational commands may be run via voice — the LLM
# decides the command from what it thinks it heard, so this is an allowlist
# (not a blocklist) to keep a misheard/misinterpreted phrase from ever running
# something destructive. First whitespace-separated token of the command must
# match one of these (case-insensitive).
ALLOWED_COMMANDS = {
    "dir", "echo", "whoami", "systeminfo", "ipconfig", "ping",
    "tasklist", "where", "hostname", "ver", "date", "time",
    "type", "tree", "vol", "netstat", "driverquery",
}

# Shell metacharacters that could chain an allowed command into something
# else (e.g. "echo hi & del /f x") are always blocked, even for allowed verbs.
# % and ! would expand environment variables (%USERPROFILE%\.ssh\...), ^ escapes
# characters past this check - all blocked too.
_SHELL_METACHARACTERS = set("&|;<>`$%!^")

# Commands whose arguments are paths: those paths must pass the folder sandbox
# (so "type" can't read ARYA's token, SSH keys, .env files...).
_PATH_COMMANDS = {"type", "dir", "tree"}


# os.startfile() / ShellExecute only resolves a bare name if it's an exact
# match on PATH or in the "App Paths" registry — spoken names like "vs code"
# or "visual studio code" (what the LLM extracts from natural speech) match
# neither, so the previous implementation silently failed for anything but
# a handful of well-known system exes. Map common spoken names to their real
# executables, and fall back to a bounded search of the usual install roots.
_APP_ALIASES: dict[str, str] = {
    "vscode": "Code.exe",
    "vs code": "Code.exe",
    "visual studio code": "Code.exe",
    "code": "Code.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "word": "WINWORD.EXE",
    "microsoft word": "WINWORD.EXE",
    "excel": "EXCEL.EXE",
    "microsoft excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE",
    "spotify": "Spotify.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "task manager": "Taskmgr.exe",
    "paint": "mspaint.exe",
    "settings": "ms-settings:",
}

_APP_SEARCH_ROOTS = [
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
]

_resolved_app_cache: dict[str, str] = {}


def _walk_limited(root: str, target_filename: str, max_depth: int = 4) -> str | None:
    """Depth-limited search so this never turns into a full Program Files crawl."""
    root = os.path.normpath(root)
    base_depth = root.count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        if target_filename in filenames:
            return os.path.join(dirpath, target_filename)
    return None


def _resolve_app(app_name: str) -> str | None:
    """Best-effort resolve a spoken app name to a launchable path. Runs on a
    worker thread (see open_app) since the fallback search touches disk."""
    key = app_name.strip().lower()
    if key in _resolved_app_cache:
        return _resolved_app_cache[key]

    alias = _APP_ALIASES.get(key)
    if alias and alias.endswith(":"):
        _resolved_app_cache[key] = alias
        return alias

    candidates = [alias] if alias else []
    stripped = key.replace(" ", "")
    for guess in (f"{stripped}.exe", key if key.endswith(".exe") else f"{stripped}.exe"):
        if guess not in candidates:
            candidates.append(guess)

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            _resolved_app_cache[key] = found
            return found

    for candidate in candidates:
        for root in _APP_SEARCH_ROOTS:
            if root and os.path.isdir(root):
                found = _walk_limited(root, candidate)
                if found:
                    _resolved_app_cache[key] = found
                    return found

    return None


async def open_app(app_name: str) -> str:
    if not app_name.strip():
        return "Failed to open app: app name is empty."

    loop = asyncio.get_event_loop()
    resolved = await loop.run_in_executor(None, _resolve_app, app_name)
    target = resolved or app_name

    try:
        os.startfile(target)  # type: ignore[attr-defined]
        return f"Opened {app_name}"
    except Exception as exc:
        try:
            subprocess.Popen(f'start "" "{target}"', shell=True)
            return f"Opened {app_name}"
        except Exception:
            return f"Failed to open {app_name}: couldn't find or launch it ({exc})."


async def open_url(url: str) -> str:
    raw_url = url.strip()
    if not raw_url:
        return "Failed to open URL: URL is empty."

    normalized_url = raw_url
    if not normalized_url.lower().startswith(("http://", "https://")):
        normalized_url = f"https://{normalized_url}"

    chrome_paths = [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ]

    try:
        for chrome_path in chrome_paths:
            if os.path.exists(chrome_path):
                subprocess.Popen([chrome_path, normalized_url])
                return f"Opened {normalized_url} in Chrome"

        opened = webbrowser.open(normalized_url)
        if not opened:
            return f"Failed to open {normalized_url}: browser could not open URL."
        return "Chrome not found, opened in default browser"
    except Exception as exc:
        return f"Failed to open {normalized_url}: {exc}"


async def file_search(filename: str, search_path: str = "") -> str:
    from arya.core.policy import HOME, check_path

    target = filename.strip().lower()
    if not target:
        return "No files found"
    root = check_path(search_path) if search_path.strip() else HOME
    search_path = str(root)

    matches: list[str] = []
    permission_error_count = 0

    def _on_walk_error(exc: OSError) -> None:
        nonlocal permission_error_count
        if isinstance(exc, PermissionError):
            permission_error_count += 1

    try:
        for root, dirs, files in os.walk(search_path, onerror=_on_walk_error):
            # Same sandbox as the file tools: skip hidden, AppData and system folders.
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in
                       ("appdata", "application data", "local settings", "node_modules", "__pycache__")]

            for name in files:
                if target in name.lower():
                    matches.append(os.path.join(root, name))
                    if len(matches) >= 5:
                        break
            if len(matches) >= 5:
                break
    except PermissionError:
        permission_error_count += 1
    except Exception as exc:
        return f"File search failed: {exc}"

    if not matches:
        if permission_error_count > 0:
            return "No files found (some folders were skipped due to permission restrictions)"
        return "No files found"

    return "Found files:\n" + "\n".join(matches)


async def run_command(command: str) -> str:
    normalized_command = command.strip()
    if not normalized_command:
        return "Command not allowed"

    if any(ch in normalized_command for ch in _SHELL_METACHARACTERS):
        return "Command not allowed: shell operators like &, |, ;, <, >, `, $ are blocked."

    tokens = normalized_command.split()
    first_token = tokens[0].lower()
    if first_token not in ALLOWED_COMMANDS:
        return (
            f"Command not allowed: '{first_token}' isn't in ARYA's allowed command "
            f"list ({', '.join(sorted(ALLOWED_COMMANDS))})."
        )
    if first_token in _PATH_COMMANDS:
        from arya.core.policy import check_path

        path_args = [t.strip('"') for t in tokens[1:] if not t.startswith("/")]
        if first_token == "type" and not path_args:
            return "Command not allowed: type needs a file path."
        for arg in path_args:
            check_path(arg)            # raises PolicyError with a spoken reason

    try:
        result = subprocess.run(
            normalized_command,
            shell=True,
            cwd=str(Path.home()),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "Command failed: timed out after 10 seconds."
    except Exception as exc:
        return f"Command failed: {exc}"

    if result.returncode == 0:
        return (result.stdout or "").strip()
    return (result.stderr or "").strip()
