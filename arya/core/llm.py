import asyncio
import os
import json
import re
from datetime import datetime

from dotenv import load_dotenv
from groq import AsyncGroq

from arya.memory.database import get_history, save_message, save_memory_vector, semantic_search


load_dotenv()

SYSTEM_PROMPT = (
    "You are ARYA, a fast and helpful voice assistant. "
    "Only call tools when the user explicitly asks for an action or live data. "
    "Never call tools for greetings, casual conversation, or general questions."
)
MODEL_NAME = "llama-3.3-70b-versatile"
INTENT_MODEL_NAME = "llama-3.1-8b-instant"

_client: AsyncGroq | None = None


def _get_client() -> AsyncGroq:
    global _client
    if _client is not None:
        return _client

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing GROQ_API_KEY. Add it to your .env file before calling chat()."
        )

    _client = AsyncGroq(api_key=api_key)
    return _client


# ── Semantic memory helpers ───────────────────────────────────────────────────

_MAX_STORED_LEN = 500   # chars to embed (quality) — content stored up to 300 in DB


async def _store_embedding(kind: str, source_id: int, labelled_content: str) -> None:
    """Background task: embed labelled_content and persist vector. Never raises."""
    try:
        from arya.core.embedder import embed
        vec = await embed(labelled_content[:_MAX_STORED_LEN])
        save_memory_vector(kind, source_id, labelled_content, vec)
    except Exception as exc:
        print(f"Embedder: store failed (kind={kind}, id={source_id}): {exc}")


async def _get_semantic_context(message: str, exclude: set[str]) -> str:
    """
    Embed message, search memory, return a formatted memory block to append to
    SYSTEM_PROMPT — or an empty string when nothing relevant is found.
    """
    try:
        from arya.core.embedder import embed
        vec = await embed(message)
        hits = semantic_search(vec, top_k=3, exclude_content=exclude)
        if not hits:
            return ""
        lines = "\n".join(f"  - {h[:150]}" for h in hits)
        return f"\n\n### Relevant context from memory:\n{lines}"
    except Exception as exc:
        print(f"Semantic search skipped: {exc}")
        return ""


async def chat(message: str) -> str:
    response = await _get_client().chat.completions.create(
        model=INTENT_MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    )
    return response.choices[0].message.content or ""


async def needs_tool(message: str) -> bool:
    response = await _get_client().chat.completions.create(
        model=INTENT_MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a binary classifier. Reply with only YES or NO. No explanation.\n\n"
                    "Reply YES for ANY of these:\n"
                    "- Weather requests: 'weather in X', 'temperature in X'\n"
                    "- App/URL opening: 'open X', 'launch X', 'go to X website'\n"
                    "- File operations: 'search for file X', 'find file X'\n"
                    "- Terminal commands: 'run command X', 'what is my directory'\n"
                    "- System status: 'CPU usage', 'RAM usage', 'battery level'\n"
                    "- Timer/Pomodoro: 'set timer', 'pomodoro', 'focus session'\n"
                    "- Clipboard: 'what is in clipboard', 'copy X to clipboard'\n"
                    "- Math/Calculator: 'calculate X', 'what is X percent of Y', 'convert X to Y'\n"
                    "- Wikipedia: 'search wikipedia for X', 'look up X on wikipedia'\n"
                    "- Notes: 'take a note', 'save note', 'read my notes', 'search notes'\n"
                    "- Study explain: 'explain X', 'what is X simply', 'describe X technically', 'explain X for exam'\n"
                    "- Study formula: 'formula for X', 'equation for X', 'give me formula'\n"
                    "- Study quiz: 'quiz me', 'test me', 'flashcard', 'give me questions on X'\n"
                    "- Daily briefing: 'morning briefing', 'daily briefing', 'brief me'\n\n"
                    "Reply NO for:\n"
                    "- Greetings: 'hello', 'how are you', 'good morning'\n"
                    "- Casual chat: 'tell me a joke', 'what do you think about X'\n"
                    "- Follow-up questions in conversation: 'can you elaborate', 'tell me more'\n"
                    "- Opinion questions: 'which is better X or Y'"
                ),
            },
            {"role": "user", "content": message},
        ],
        temperature=0,
        max_tokens=5,
    )
    decision = (response.choices[0].message.content or "").strip().upper()
    return decision == "YES"


async def get_daily_briefing() -> str:
    now_text = datetime.now().strftime("%A, %B %d %Y, %I:%M %p")

    default_city = os.getenv("DEFAULT_CITY", "Berlin")
    try:
        default_lat = float(os.getenv("DEFAULT_LAT", "52.52"))
        default_lon = float(os.getenv("DEFAULT_LON", "13.405"))
    except ValueError as exc:
        raise RuntimeError("Invalid DEFAULT_LAT or DEFAULT_LON in environment.") from exc

    from arya.tools.notes import get_notes
    from arya.tools.system_info import get_system_info
    from arya.tools.weather import get_weather

    weather = await get_weather(default_city, default_lat, default_lon)
    notes = await get_notes(limit=5)
    system_status = await get_system_info()

    prompt = (
        "Time and date:\n"
        f"{now_text}\n\n"
        "Weather:\n"
        f"{weather}\n\n"
        "Recent notes:\n"
        f"{notes}\n\n"
        "System status:\n"
        f"{system_status}\n"
    )

    response = await _get_client().chat.completions.create(
        model=INTENT_MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are ARYA. Generate a concise morning briefing "
                    "in a friendly professional tone. Cover: time, weather, "
                    "recent notes, and system status. Keep it under 5 sentences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city name to fetch weather for",
                    },
                    "latitude": {
                        "type": "number",
                        "description": "Latitude of the city",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Longitude of the city",
                    },
                },
                "required": ["city", "latitude", "longitude"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an application on Windows by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The name of the application to open",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL in the browser",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open in the default browser",
                    }, \
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "Search for a file by name on Windows",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The filename or partial filename to search for",
                    },
                    "search_path": {
                        "type": "string",
                        "description": "Root directory where search should start",
                        "default": "C:\\Users",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a read-only Windows diagnostic command and return its output. "
                "Only these commands are allowed: dir, echo, whoami, systeminfo, "
                "ipconfig, ping, tasklist, where, hostname, ver, date, time, type, "
                "tree, vol, netstat, driverquery. No shell operators (&, |, ;, <, >)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The Windows terminal command to run",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Get current CPU usage and RAM usage of the computer",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_battery",
            "description": "Get current battery percentage and charging status",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Set a timer for a specified number of seconds",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Number of seconds for the timer",
                    },
                    "label": {
                        "type": "string",
                        "description": "Label or name for the timer",
                    },
                },
                "required": ["duration_seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pomodoro",
            "description": "Start a pomodoro study session with focus and break intervals",
            "parameters": {
                "type": "object",
                "properties": {
                    "sessions": {
                        "type": "integer",
                        "description": "Number of pomodoro sessions, default 1",
                    },
                    "label": {
                        "type": "string",
                        "description": "Topic or subject being studied",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Read the current content of the clipboard",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": "Write or copy text to the clipboard",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to copy to clipboard",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Calculate a math expression or unit conversion",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The math expression to evaluate",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wikipedia",
            "description": "Search Wikipedia and return a short summary of a topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The topic to search on Wikipedia",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_concept",
            "description": (
                "Explain any concept or topic for a Mechatronics engineering student. "
                "Use for: 'explain X', 'what is X', 'describe X', 'explain X simply', "
                "'explain X technically', 'explain X for exam'. "
                "Extract the explanation level from the user message and include it "
                "in the topic parameter like: 'PID controller simply' or "
                "'state space technically' or 'transfer function for exam'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The concept or topic to explain",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "formula_lookup",
            "description": (
                "Look up engineering or math formulas with variables, "
                "units and examples. Use for questions like 'what is the formula for X', "
                "'give me the equation for X'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The formula topic to look up",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flashcard_quiz",
            "description": (
                "Generate flashcard quiz questions on any study topic. "
                "Use for: 'quiz me on X', 'test me on X', 'give me N questions on X'. "
                "Include the number in the topic parameter like: '3 questions on control theory'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic to generate questions on",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Save a research note or study note. Extract the actual note content "
                "from phrases like 'take a note:', 'note this:', 'remember this:', "
                "'save this:'. The content parameter should contain only the note text, "
                "not the trigger phrase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The note content to save",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Category tag like 'thesis', 'exam', 'idea', 'general'",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_notes",
            "description": "Retrieve saved research or study notes, optionally filtered by tag",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Filter notes by tag",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of notes to return, default 10",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Search through saved notes by keyword",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to search in notes",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_briefing",
            "description": "Get a morning briefing with current time, weather, recent notes and system status",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
]


async def _tool_pipeline(message: str) -> str:
    """Execute the full tool-dispatch pipeline. Only call when a tool is confirmed needed."""
    client = _get_client()
    history = get_history(limit=10)

    # ── Semantic memory injection ─────────────────────────────────────────────
    recent_contents = {item["content"] for item in history}
    mem_context = await _get_semantic_context(message, recent_contents)

    msg_id = save_message("user", message)
    asyncio.create_task(_store_embedding("message", msg_id, f"[user]: {message}"))

    history_messages = [
        {"role": item["role"], "content": item["content"]}
        for item in history
    ]
    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT + mem_context},
        *history_messages,
        {"role": "user", "content": message},
    ]

    try:
        first_response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=base_messages,
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
            temperature=0,
        )
    except Exception as exc:
        raise RuntimeError(f"Groq request failed: {exc}") from exc

    first_message = first_response.choices[0].message
    finish_reason = first_response.choices[0].finish_reason
    if finish_reason == "stop":
        assistant_response = first_message.content or ""
        resp_id = save_message("assistant", assistant_response)
        asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {assistant_response}"))
        return assistant_response
    if finish_reason != "tool_calls":
        assistant_response = first_message.content or ""
        resp_id = save_message("assistant", assistant_response)
        asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {assistant_response}"))
        return assistant_response

    tool_calls = first_message.tool_calls
    if not tool_calls or len(tool_calls) == 0:
        assistant_response = first_message.content or ""
        resp_id = save_message("assistant", assistant_response)
        asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {assistant_response}"))
        return assistant_response

    tool_call = first_response.choices[0].message.tool_calls[0]
    tool_name = re.split(r"[\s{=,]", tool_call.function.name)[0].strip()

    try:
        args = json.loads(tool_call.function.arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Model returned invalid tool arguments for {tool_name}.") from exc
    if args is None:
        args = {}
    print(f"Tool called: {tool_name}, Args: {args}")

    if tool_name == "get_weather":
        try:
            city = args["city"]
            latitude = float(args["latitude"])
            longitude = float(args["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid weather arguments.") from exc

        from arya.tools.weather import get_weather

        tool_result = await get_weather(city=city, latitude=latitude, longitude=longitude)
    elif tool_name == "open_app":
        try:
            app_name = str(args["app_name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid app arguments.") from exc

        from arya.tools.os_tools import open_app

        tool_result = await open_app(app_name=app_name)
    elif tool_name == "open_url":
        try:
            url = str(args["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid URL arguments.") from exc

        from arya.tools.os_tools import open_url

        tool_result = await open_url(url=url)
    elif tool_name == "file_search":
        try:
            filename = str(args["filename"])
            search_path = str(args.get("search_path", "C:\\Users"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid file search arguments.") from exc

        from arya.tools.os_tools import file_search

        tool_result = await file_search(filename=filename, search_path=search_path)
    elif tool_name == "run_command":
        try:
            command = str(args["command"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid command arguments.") from exc

        from arya.tools.os_tools import run_command

        tool_result = await run_command(command=command)
    elif tool_name == "get_system_info":
        from arya.tools.system_info import get_system_info

        tool_result = await get_system_info()
    elif tool_name == "get_battery":
        from arya.tools.system_info import get_battery

        tool_result = await get_battery()
    elif tool_name == "set_timer":
        try:
            duration_seconds = int(args["duration_seconds"])
            label = str(args.get("label", "Timer"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid timer arguments.") from exc

        from arya.tools.productivity import set_timer

        tool_result = await set_timer(duration_seconds=duration_seconds, label=label)
    elif tool_name == "pomodoro":
        try:
            sessions = int(args.get("sessions", 1))
            label = str(args.get("label", "Study"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid pomodoro arguments.") from exc

        from arya.tools.productivity import pomodoro

        tool_result = await pomodoro(sessions=sessions, label=label)
    elif tool_name == "get_clipboard":
        from arya.tools.productivity import get_clipboard

        tool_result = await get_clipboard()
    elif tool_name == "set_clipboard":
        try:
            text = str(args["text"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid clipboard arguments.") from exc

        from arya.tools.productivity import set_clipboard

        tool_result = await set_clipboard(text=text)
    elif tool_name == "calculate":
        try:
            expression = str(args["expression"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid calculation arguments.") from exc

        from arya.tools.knowledge import calculate

        tool_result = await calculate(expression=expression)
    elif tool_name == "search_wikipedia":
        try:
            query = str(args["query"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid Wikipedia arguments.") from exc

        from arya.tools.knowledge import search_wikipedia

        tool_result = await search_wikipedia(query=query)
    elif tool_name == "explain_concept":
        try:
            topic = str(args["topic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid explain_concept arguments.") from exc

        lowered_topic = topic.lower()
        if "simply" in lowered_topic or "simple" in lowered_topic:
            level = "simple"
        elif "technically" in lowered_topic or "technical" in lowered_topic:
            level = "technical"
        elif "exam" in lowered_topic:
            level = "exam"
        else:
            level = "simple"

        cleaned_topic = re.sub(r"\b(simply|simple|technically|technical|exam)\b", "", topic, flags=re.IGNORECASE)
        cleaned_topic = re.sub(r"\s+", " ", cleaned_topic).strip()
        if not cleaned_topic:
            cleaned_topic = topic

        from arya.tools.study import explain_concept

        tool_result = await explain_concept(topic=cleaned_topic, level=level)
    elif tool_name == "formula_lookup":
        try:
            topic = str(args["topic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid formula_lookup arguments.") from exc

        from arya.tools.study import formula_lookup

        tool_result = await formula_lookup(topic=topic)
    elif tool_name == "flashcard_quiz":
        try:
            topic = str(args["topic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid flashcard_quiz arguments.") from exc

        match = re.search(r"\d+", topic)
        num_questions = int(match.group()) if match else 3
        cleaned_topic = re.sub(r"\d+", "", topic)
        cleaned_topic = re.sub(r"\s+", " ", cleaned_topic).strip()
        if not cleaned_topic:
            cleaned_topic = topic

        from arya.tools.study import flashcard_quiz

        tool_result = await flashcard_quiz(topic=cleaned_topic, num_questions=num_questions)
    elif tool_name == "save_note":
        try:
            content = str(args["content"])
            tag = str(args.get("tag", "general"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid note arguments.") from exc

        from arya.tools.notes import save_note

        tool_result = await save_note(content=content, tag=tag)
    elif tool_name == "get_notes":
        from arya.tools.notes import get_notes

        tool_result = await get_notes(tag=args.get("tag"), limit=args.get("limit", 10))
    elif tool_name == "search_notes":
        try:
            keyword = str(args["keyword"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid search_notes arguments.") from exc

        from arya.tools.notes import search_notes

        tool_result = await search_notes(keyword=keyword)
    elif tool_name == "get_daily_briefing":
        tool_result = await get_daily_briefing()
    else:
        raise RuntimeError(f"Unsupported tool requested by model: {tool_name}")

    follow_up_messages = [
        *base_messages,
        first_response.choices[0].message,
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": (
                f"Tool result: {str(tool_result)}\n\n"
                "Report this result to the user accurately and verbatim. "
                "Do not recalculate, rephrase numbers, or add information not present in the result."
            ),
        },
    ]

    try:
        final_response = await client.chat.completions.create(
            model=INTENT_MODEL_NAME,
            messages=follow_up_messages,
            temperature=0,
        )
    except Exception as exc:
        raise RuntimeError(f"Groq follow-up request failed: {exc}") from exc

    assistant_response = final_response.choices[0].message.content or ""
    resp_id = save_message("assistant", assistant_response)
    asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {assistant_response}"))
    return assistant_response


async def chat_with_tools(message: str) -> str:
    """Public entry point: checks intent then dispatches tools if needed."""
    if not await needs_tool(message):
        return await chat(message)
    return await _tool_pipeline(message)


async def stream_with_tools(message: str):
    needs_tool_result = await needs_tool(message)
    print(f"Needs tool: {needs_tool_result}")

    if needs_tool_result:
        response = await _tool_pipeline(message)
        yield response
        return

    client = _get_client()
    history = get_history(limit=10)

    # ── Semantic memory injection ─────────────────────────────────────────────
    recent_contents = {item["content"] for item in history}
    mem_context = await _get_semantic_context(message, recent_contents)

    msg_id = save_message("user", message)
    asyncio.create_task(_store_embedding("message", msg_id, f"[user]: {message}"))

    history_messages = [
        {"role": item["role"], "content": item["content"]}
        for item in history
    ]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + mem_context},
        *history_messages,
        {"role": "user", "content": message},
    ]

    try:
        stream = await client.chat.completions.create(
            model=INTENT_MODEL_NAME,
            messages=messages,
            stream=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Groq streaming request failed: {exc}") from exc

    buffer = ""
    full_response_parts: list[str] = []

    async for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue

        delta = getattr(choices[0], "delta", None)
        token = getattr(delta, "content", None) if delta else None
        if not token:
            continue

        buffer += token

        while True:
            match = re.search(r"(?<!\d)[.!?](?=\s|$)", buffer)
            if not match:
                break

            sentence = buffer[: match.end()]
            if sentence:
                yield sentence
                full_response_parts.append(sentence)
            buffer = buffer[match.end():]

    if buffer:
        yield buffer
        full_response_parts.append(buffer)

    full_response = "".join(full_response_parts)
    resp_id = save_message("assistant", full_response)
    asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {full_response}"))
