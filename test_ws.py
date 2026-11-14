import asyncio
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arya.server.security import PORT, TOKEN  # noqa: E402  (reads the local token file)


async def main() -> None:
    uri = f"ws://127.0.0.1:{PORT}/ws?token={TOKEN}"
    message = "What is the weather in Munich right now?"

    async with websockets.connect(uri) as websocket:
        await websocket.send(message)
        response = await websocket.recv()
        print(response)


if __name__ == "__main__":
    asyncio.run(main())
