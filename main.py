import asyncio
import uvicorn

from arya.core.llm import chat, chat_with_tools
from arya.server.websocket import app


if __name__ == "__main__":
    # response = chat("Hey ARYA, introduce yourself in one sentence.")
    # print(response)
    # response = asyncio.run(chat_with_tools("What is the weather in Berlin right now?"))
    # print(response)
    uvicorn.run(app, host="0.0.0.0", port=8000)
