"""Graph constants shared by the API and voice transports.

The answer-node set lives here rather than in the state schema: both transports
filter streaming events by node name, and one shared constant stops a future
answer node from going silent on only one of them.
"""

from typing import Final

ANSWER_NODES: Final[frozenset[str]] = frozenset({"assistant", "finalize"})
