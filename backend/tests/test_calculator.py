"""Fast unit tests for the calculator tool — pure logic, no network, no LLM.

Guards the two contracts that matter:
  1. Correct arithmetic (including the "15% of 240" word-problem path).
  2. NEVER raises — every bad input returns an "Error evaluating ..." string,
     because a raising tool would break the "never crash the graph" rule.

NOTE: calculate is a langchain StructuredTool (the @tool decorator returns a
tool OBJECT, not a plain function), so we call it with .invoke() — the same
way tool_exec_node does in production.
"""

import pytest

from app.tools.calculator import calculate


def _calc(expr: str) -> str:
    """Run the calculator through its tool interface (as the graph does)."""
    return calculate.invoke(expr)


def test_addition() -> None:
    assert _calc("2 + 3") == "5"


def test_subtraction() -> None:
    assert _calc("10 - 4") == "6"


def test_multiplication() -> None:
    assert _calc("6 * 7") == "42"


def test_division() -> None:
    assert _calc("7 / 2") == "3.5"


def test_floor_division() -> None:
    assert _calc("7 // 2") == "3"


def test_modulo() -> None:
    assert _calc("7 % 3") == "1"


def test_power() -> None:
    assert _calc("2 ** 10") == "1024"


def test_precedence() -> None:
    assert _calc("5 + 3 * 2") == "11"


def test_parentheses() -> None:
    assert _calc("(5 + 3) * 2") == "16"


def test_unary_minus() -> None:
    assert _calc("-5 + 3") == "-2"


def test_percentage_word_problem() -> None:
    # The LLM converts "15% of 240" -> "0.15 * 240". Float-tolerant compare.
    assert float(_calc("0.15 * 240")) == pytest.approx(36.0)


def test_division_by_zero_returns_error_not_raise() -> None:
    assert _calc("1 / 0").startswith("Error evaluating")


def test_invalid_expression_returns_error_not_raise() -> None:
    assert _calc("banana").startswith("Error evaluating")


def test_empty_expression_returns_error_not_raise() -> None:
    assert _calc("").startswith("Error evaluating")
