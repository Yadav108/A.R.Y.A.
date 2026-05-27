import ast
import operator

import wikipediaapi


_SAFE_OPS: dict = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    """Recursively evaluate a parsed AST, allowing only numeric constants and
    basic arithmetic operators. Raises ValueError for anything else."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError(f"Unsupported constant type: {type(node.value)}")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if op_type is ast.Div and right == 0:
            raise ZeroDivisionError
        return _SAFE_OPS[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        return _SAFE_OPS[op_type](_safe_eval(node.operand))
    raise ValueError(f"Unsupported expression node: {type(node).__name__}")


async def calculate(expression: str) -> str:
    normalized = expression.lower().strip()
    normalized = normalized.replace("percent", "/100")
    normalized = normalized.replace("%", "/100")
    normalized = normalized.replace("squared", "**2")
    normalized = normalized.replace("cubed", "**3")
    normalized = normalized.replace("^", "**")

    try:
        tree = ast.parse(normalized, mode="eval")
        result = _safe_eval(tree)
        return f"Result: {round(result, 4)}"
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
