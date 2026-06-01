"""realhands Vision Decision Service — an optional decision layer.

Stateless library: pass a screenshot + task context + step history, get back a
structured ActionDecision. Tiered routing escalates local -> cheap -> frontier
when confidence falls below threshold. It is a plain library: import
decide_action and call it directly — no HTTP server.
"""

from vision.models import (
    ActionDecision,
    StepHistoryItem,
    VisionConfig,
)
from vision.decide import decide_action

__all__ = [
    "ActionDecision",
    "StepHistoryItem",
    "VisionConfig",
    "decide_action",
]
