"""realhands Vision Decision Service — an optional decision layer.

Stateless library: pass a screenshot + task context + step history, get back a
structured ActionDecision. Bring your own model(s): configure one (one-shot) or
several (cheap→fallback chain) via VisionConfig.models. It is a plain library:
import decide_action and call it directly — no HTTP server.
"""

from vision.models import (
    ActionDecision,
    ModelConfig,
    StepHistoryItem,
    VisionConfig,
)
from vision.decide import decide_action

__all__ = [
    "ActionDecision",
    "ModelConfig",
    "StepHistoryItem",
    "VisionConfig",
    "decide_action",
]
