"""
The webcam, only when you ask (roadmap F).

  "take a photo with the webcam"  "what do you see?"  "what am I holding?"  "can you read this page?" (held up)
  "look at this"  "use the camera to read this"
    -> one still photo from the laptop's camera (its light comes on for those ~3 seconds, then it's off again).
       The photo is shown in the app and kept in memory only (never saved to disk unless you say "add it to my
       library"). Text on it (a page, a label, a whiteboard) is read here with Windows OCR and the local model
       answers. A photo with little text (a gadget, a plant, your face) can only be described by the cloud vision
       model, so ARYA asks first; "no" keeps it on the laptop.
  Never from the phone: the laptop's camera is not something to switch on remotely.
  Never in the background: there is no timer, no motion detection, nothing that opens the camera unasked.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time

DEFAULT_DEVICE = "Integrated Camera"
SIZE = "1280x720"
MAX_FRAMES = 60                  # ~6 s: auto exposure needs ~3 s in a dim room
SETTLE = 2.0                     # brightness steady within this over 5 frames -> exposure has settled
MIN_FRAMES = 10
TITLE = "Photo: webcam"

_device: dict = {"name": None}

_CAM = r"(?:web\s?cam|(?:laptop'?s?\s+|the\s+|your\s+|my\s+)?camera)"
_ASK = re.compile(
    rf"\b(?:take|snap|grab|capture)\s+(?:a\s+|me\s+a\s+)?(?:photo|picture|pic|snapshot|selfie|shot)\s+(?:with|using|from|on|through)\s+"
    rf"(?:the\s+|your\s+|my\s+)?{_CAM}|"
    rf"\b(?:use|open|turn\s+on|switch\s+on|look\s+through|check)\s+(?:the\s+|your\s+|my\s+)?{_CAM}\b(?!\s+settings)|"
    rf"\b(?:with|through|using)\s+(?:the\s+|your\s+)?{_CAM}\b.{{0,30}}\b(?:read|see|look|check|tell)\b|"
    rf"\b(?:read|look\s+at|check|see)\b.{{0,30}}\b(?:with|through|using)\s+(?:the\s+|your\s+)?{_CAM}\b|"
    r"^\W*(?:(?:hey|ok)\W+)?(?:arya\W+)?(?:what\s+do\s+you\s+see|what\s+can\s+you\s+see|can\s+you\s+see\s+(?:me|this|what)|"
    r"what\s+am\s+i\s+(?:holding|showing\s+you)|look\s+at\s+(?:this|me|what\s+i'?m\s+holding)|"
    r"(?:can\s+you\s+)?read\s+(?:this|what)\s+i'?m\s+holding(?:\s+up)?)\b",
    re.IGNORECASE)
_JUST_TAKE = re.compile(r"\b(?:take|snap|grab|capture)\s+(?:a\s+|me\s+a\s+)?(?:photo|picture|pic|snapshot|selfie|shot)\b|"
                        r"\b(?:open|turn\s+on|switch\s+on)\s+(?:the\s+|your\s+|my\s+)?(?:web\s?cam|camera)\b",
                        re.IGNORECASE)
_NOT = re.compile(r"\b(?:screen|display|monitor|window|tab|desktop)\b|\bhow\s+(?:do|does|to|can)\b|"
                  r"\bsettings?\b|\bdriver\b|\bnot\s+working\b|\bisn'?t\s+working\b", re.IGNORECASE)


def matches(message: str) -> bool:
    return bool(_ASK.search(message)) and not _NOT.search(message)


def device_name() -> str:
    """ARYA_WEBCAM, else the first camera Windows lists, else the usual laptop name."""
    if os.environ.get("ARYA_WEBCAM"):
        return os.environ["ARYA_WEBCAM"]
    if _device["name"] is None:
        name = ""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -eq 'Camera' -and $_.Status -eq 'OK' }"
                 " | Select-Object -First 1).Name"],
                capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            name = out.stdout.strip()
        except Exception:
            pass
        _device["name"] = name or DEFAULT_DEVICE
    return _device["name"]


def _brightness(img) -> float:
    from PIL import ImageStat
    return ImageStat.Stat(img.convert("L")).mean[0]


def grab():
    """One still (PIL image) once auto exposure has settled; the camera is closed again right after.
    Raises RuntimeError("busy") when another app (a call) holds the camera."""
    import av
    try:
        container = av.open(f"video={device_name()}", format="dshow", options={"video_size": SIZE})
    except Exception as exc:
        try:                                       # some cameras don't do 1280x720: their default size
            container = av.open(f"video={device_name()}", format="dshow")
        except Exception:
            raise RuntimeError("busy") from exc
    img, recent = None, []
    try:
        for i, frame in enumerate(container.decode(container.streams.video[0])):
            img = frame.to_image()
            recent = (recent + [_brightness(img)])[-5:]
            if i + 1 >= MIN_FRAMES and len(recent) == 5 and max(recent) - min(recent) < SETTLE:
                break
            if i + 1 >= MAX_FRAMES:
                break
    finally:
        container.close()
    if img is None:
        raise RuntimeError("no frames")
    return img.convert("RGB")


def _dark(img) -> bool:
    return _brightness(img) < 8


async def handle(message: str, remote: bool = False):
    """(reply, artifacts) like screen.handle, or None."""
    if not matches(message):
        return None
    if remote:
        return ("For privacy I only switch on the laptop's camera when you ask at the laptop. "
                "To show me something from here, tap 📷 and use your phone's camera."), []
    import io

    from arya.core.audit import log_event
    from arya.knowledge import visuals
    from arya.tools import screen

    try:
        img = await asyncio.to_thread(grab)
    except Exception as exc:
        print(f"Webcam: no photo ({type(exc).__name__}: {exc})")
        if "busy" in str(exc):
            return "I couldn't open the webcam. Another app may be using it, like a video call.", []
        return "I couldn't get a picture from the webcam.", []
    log_event("webcam_photo", size=f"{img.width}x{img.height}")       # never the picture itself
    if _dark(img):
        return "The webcam only sees black. Is the camera shutter closed or the lens covered?", []
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_id = visuals._store(buf.getvalue())
    shown = ("VISUAL", {"type": "image", "id": image_id, "caption": "Webcam photo (kept in memory only)"})
    text = await asyncio.to_thread(screen.ocr_image, img)
    screen._context.update(text=text, title=TITLE, image_id=image_id, until=time.monotonic() + screen.CONTEXT_S,
                           kind="photo")
    print(f"Webcam: {img.width}x{img.height}, {len(text)} characters read")
    if len(text.strip()) < screen.MIN_TEXT_CHARS:
        from arya.core import router
        if not router.cloud_available():
            return ("Here's what the webcam sees; it's on the screen. There's no text on it I can read, and "
                    "describing a picture needs the cloud vision model, which isn't set up."), [shown]
        from arya.core import policy
        question = "Describe briefly what this photo shows." if _JUST_TAKE.search(message) else message
        return policy.offer("screen_vision", {"image_id": image_id, "question": question, "title": TITLE},
                            "Got the photo; it's on the screen. There's little text on it, so I can't read it "
                            "here. Want me to send this one photo to the cloud to describe it? Say yes or no."), [shown]
    if _JUST_TAKE.search(message) and not re.search(r"\?|\b(?:what|read|explain|tell|which|who|how)\b", message,
                                                    re.IGNORECASE):
        return (f"Got it; the photo is on the screen. I can read about {len(text.split())} words on it. "
                "Ask me anything about it, or say \"add it to my library\"."), [shown]
    reply, passages = await screen.answer(message, text, TITLE)
    return reply, [shown] + ([("SOURCES", [p.as_dict() for p in passages])] if passages else [])
