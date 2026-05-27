import asyncio

import websockets


async def main() -> None:
    uri = "ws://localhost:8000/ws"
    message = "What is the weather in Munich right now?"

    async with websockets.connect(uri) as websocket:
        await websocket.send(message)
        response = await websocket.recv()
        print(response)


if __name__ == "__main__":
    asyncio.run(main())
