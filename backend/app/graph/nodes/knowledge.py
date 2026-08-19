"""knowledge_node — a specialist bound to exactly one tool: a calculator.

This is the FIRST specialist. It demonstrates the tool-calling pattern that
every later specialist (weather, calendar, ...) will copy:
  1. get a role-specific model
  2. bind_tools([...])
  3. invoke on the message history
The model then EITHER emits tool_calls (graph routes them to tool_exec)
OR emits a plain text answer (graph routes that to responder).
"""

import ast
import operator

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.core.llm import get_chat_model
from app.graph.state import IPAState

# ---- calculator tool (safe AST evaluation, never eval()) ----

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_node(node):
    """Recursively evaluate a parsed AST node — a safe eval replacement."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) is ast.USub:
        return -_eval_node(node.operand)
    raise ValueError(f"Unsupported expression element: {type(node).__name__}")


@tool
def calculate(expression: str) -> str:
    """Evaluate a safe arithmetic expression and return the numeric result.

    Accepts a Python-style expression built from + - * / // ** % and
    parentheses, e.g. '0.15 * 240' or '(5 + 3) * 2'. Convert a natural
    language question like "15% of 240" into '0.15 * 240' before calling.
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
        return str(result)
    except Exception as exc:
        return f"Error evaluating {expression!r}: {exc}"


# Registry: name -> tool. tool_exec looks tools up here, so adding a tool is
# one line in this list and nothing else.
TOOLS = [calculate]
tools_by_name = {tool_.name: tool_ for tool_ in TOOLS}


def knowledge_node(state: IPAState, config: RunnableConfig) -> dict:
    """Function to handle knowledge requests by invoking a specialist model bound to the calculator tool.

    Args:
        state (IPAState): The current state of the IPA, including message history.
        config (RunnableConfig): The configuration for the runnable.

    Returns:
        dict: A dictionary containing the response from the knowledge specialist.
    """
    model = get_chat_model("specialist").bind_tools(TOOLS)
    response = model.invoke(state["messages"], config)
    return {"messages": [response]}
