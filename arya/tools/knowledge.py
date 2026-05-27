import re

import wikipediaapi


async def calculate(expression: str) -> str:
    normalized = expression.lower().strip()
    normalized = normalized.replace("percent", "/100")
    normalized = normalized.replace("%", "/100")
    normalized = normalized.replace("squared", "**2")
    normalized = normalized.replace("cubed", "**3")

    if not re.fullmatch(r"[0-9.\+\-\*/()%\s]*", normalized):
        return "Invalid expression — only math allowed."

    try:
        result = eval(normalized, {"__builtins__": {}}, {})
        return f"Result: {round(float(result), 4)}"
    except ZeroDivisionError:
        return "Cannot divide by zero."
    except Exception:
        return "Could not calculate that expression."


async def search_wikipedia(query: str) -> str:
    wiki = wikipediaapi.Wikipedia(
        language="en",
        user_agent="ARYA-Assistant/1.0",
    )

    try:
        page = wiki.page(query)
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Wikipedia data: {exc}") from exc

    if not page.exists():
        return f"No Wikipedia article found for: {query}"

    summary = (page.summary or "").strip()
    if not summary:
        return f"No Wikipedia article found for: {query}"

    parts = summary.split(". ")
    first_three = ". ".join(parts[:3]).rstrip(".")
    return first_three + "."
