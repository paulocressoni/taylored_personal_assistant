"""Calculator tool — safe arithmetic evaluation with no external deps."""

import ast
import logging
import operator

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Guards against pathological inputs that would otherwise burn CPU/RAM:
#  - a numeric literal larger than a float can hold (~1e308)
#  - an exponent so large the computation explodes in size/time
#  - a result that overflows float range (incl. gigantic ints)
_MAX_NUMERIC_MAGNITUDE = 1e308
_MAX_POW_EXPONENT = 1000


def _eval_node(node):
    """Recursively evaluate a parsed AST node — a safe eval replacement."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if abs(node.value) > _MAX_NUMERIC_MAGNITUDE:
            raise ValueError("Numeric literal too large")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        # Check the exponent BEFORE computing the power.
        if type(node.op) is ast.Pow and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError(f"Exponent too large (max {_MAX_POW_EXPONENT})")
        result = _OPERATORS[type(node.op)](left, right)
        # Two ints can still produce a giant int within the exponent cap;
        # refuse anything that goes past float range.
        if isinstance(result, (int, float)) and abs(result) > _MAX_NUMERIC_MAGNITUDE:
            raise ValueError("Result too large")
        return result
    if isinstance(node, ast.UnaryOp) and type(node.op) is ast.USub:
        return -_eval_node(node.operand)
    raise ValueError(f"Unsupported expression element: {type(node).__name__}")


@tool
def calculate(expression: str) -> str:
    """Evaluate a safe arithmetic expression and return the numeric result.

    Args:
        expression (str): A Python-style arithmetic expression built from
            + - * / // ** % and parentheses, e.g. '0.15 * 240' or '(5 + 3) * 2'.
            Convert a natural language question like "15% of 240" into
            '0.15 * 240' before calling.
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
        return str(result)
    except Exception as exc:  # noqa: BLE001 - tool boundary: return error string, never raise
        logger.warning(
            "calculate failed on %r: %s: %s", expression, type(exc).__name__, exc
        )
        return f"Error evaluating {expression!r}: {exc}"
