import os
import subprocess
import webbrowser


DANGEROUS_KEYWORDS = ["rm -rf", "format", "del /f", "shutdown", "rmdir"]


async def open_app(app_name: str) -> str:
    if not app_name.strip():
        return "Failed to open app: app name is empty."

    try:
        os.startfile(app_name)  # type: ignore[attr-defined]
        return f"Opened {app_name}"
    except Exception:
        try:
            subprocess.Popen(app_name, shell=True)
            return f"Opened {app_name}"
        except Exception as exc:
            return f"Failed to open {app_name}: {exc}"


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


async def file_search(filename: str, search_path: str = "C:\\Users") -> str:
    target = filename.strip().lower()
    if not target:
        return "No files found"

    matches: list[str] = []
    permission_error_count = 0

    def _on_walk_error(exc: OSError) -> None:
        nonlocal permission_error_count
        if isinstance(exc, PermissionError):
            permission_error_count += 1

    try:
        for root, dirs, files in os.walk(search_path, onerror=_on_walk_error):
            dirs[:] = [d for d in dirs if not d.startswith(".")]

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

    lower_command = normalized_command.lower()
    if any(keyword in lower_command for keyword in DANGEROUS_KEYWORDS):
        return "Command not allowed"

    try:
        result = subprocess.run(
            normalized_command,
            shell=True,
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
