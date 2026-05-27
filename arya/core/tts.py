from io import BytesIO

import edge_tts


VOICE_NAME = "en-GB-SoniaNeural"


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
