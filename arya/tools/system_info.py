import psutil


async def get_system_info() -> str:
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
    except Exception as exc:
        raise RuntimeError(f"Failed to read system info: {exc}") from exc

    used_gb = round(memory.used / (1024**3), 1)
    total_gb = round(memory.total / (1024**3), 1)
    return f"CPU is at {cpu_usage:.1f}% usage. RAM is {used_gb:.1f} GB used out of {total_gb:.1f} GB."


async def get_battery() -> str:
    try:
        battery = psutil.sensors_battery()
    except Exception as exc:
        raise RuntimeError(f"Failed to read battery info: {exc}") from exc

    if battery is None:
        return "No battery detected — this appears to be a desktop."

    percent = round(battery.percent)
    if battery.power_plugged:
        return f"Battery is at {percent}% and currently charging."
    return f"Battery is at {percent}% and not charging."
