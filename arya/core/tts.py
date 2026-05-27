from io import BytesIO

import edge_tts


VOICE_NAME = "en-GB-SoniaNeural"


async def speak(text: str) -> bytes:
    if not text:
        raise RuntimeError("Cannot synthesize empty text.")

    buffer = BytesIO()

    try:
        communicate = edge_tts.Communicate(text=text, voice=VOICE_NAME)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, (bytes, bytearray)):
                    buffer.write(data)
    except Exception as exc:
        raise RuntimeError(f"Failed to synthesize speech: {exc}") from exc

    audio_bytes = buffer.getvalue()
    if not audio_bytes:
        raise RuntimeError("Speech synthesis produced no audio output.")

    return audio_bytes


async def speak_sentence(text: str) -> bytes:
    sentence = text.strip()
    if not sentence:
        return b""

    buffer = BytesIO()

    try:
        communicate = edge_tts.Communicate(text=sentence, voice=VOICE_NAME)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, (bytes, bytearray)):
                    buffer.write(data)
    except Exception as exc:
        raise RuntimeError(f"Failed to synthesize speech: {exc}") from exc

    return buffer.getvalue()
