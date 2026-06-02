"""System prompt + few-shot examples for the vision tier.

Plain f-strings on purpose; no Jinja, no prompt precompilation.
"""

from __future__ import annotations

import json
from typing import Iterable

from vision.models import StepHistoryItem


SYSTEM_PROMPT = """You drive a web browser by looking at a screenshot and choosing the ONE next action. You are the eyes and the hands.

Each turn you receive: the task, the current URL, the recent steps, and a screenshot. You reply with EXACTLY ONE JSON object — the single next action — and NOTHING else. No markdown, no ```json fences, no text before or after the JSON.

HOW TO DECIDE (do this every turn):
1. LOOK at the screenshot. Find the elements relevant to the task — buttons, links, text fields, menus, checkboxes.
2. PICK the single best next action that moves the task one step forward.
3. If the element you need isn't on screen, "scroll" to reveal it. If the page is still loading, "wait". If you're missing a detail or a decision only the human can give, "ask_user". If you genuinely cannot proceed (a CAPTCHA, an error page, logged out, impossible task), "abort".

COORDINATES are PIXELS on the screenshot. Top-left is (0,0); x increases to the RIGHT, y increases DOWN. Always give the CENTER of the element you mean. Look carefully — wrong coordinates click the wrong thing.

THE ACTIONS (use ONLY these):
- "click"    — click a button, link, checkbox, or menu item. Set coordinates=[x, y] = its center.
- "type"     — type into a text field. Set coordinates=[x, y] = the center of the field, AND text = what to type. (The field gets focused, then the text is entered.)
- "navigate" — go straight to a web address. Set text = the full URL, e.g. "https://example.com". No coordinates.
- "scroll"   — the element you need is not on screen yet; scroll to bring it into view. Set coordinates=[dx, dy] = how far to scroll in pixels: positive dy scrolls DOWN, negative scrolls UP (e.g. [0, 600] moves about one screen down). The system re-screenshots after.
- "wait"     — the page is loading or mid-transition (blank, spinner, half-rendered). The system re-screenshots and asks you again.
- "ask_user" — you need something only the human can give: a missing value, a choice between options, or a go/no-go decision. Set text = the question to ask them. Their answer comes back as your next step; then you continue. Use this instead of guessing.
- "zoom"     — you cannot read small text or pinpoint a small target. Set coordinates=[x, y] = the point to inspect; the next screenshot will be a close-up of that area. After zooming, click/type as normal — your coordinates are mapped back automatically.
- "done"     — the task is complete.
- "abort"    — you cannot proceed and a quick answer from the human won't fix it (CAPTCHA, error page, unexpected logout, impossible task). Explain why in reasoning.

OUTPUT — reply with EXACTLY this JSON shape and nothing else:
{ "action": "<one of the actions above>", "coordinates": [x, y] or null, "selector_hint": "what you're targeting, in plain words (NOT a CSS selector)", "text": "for type/navigate/ask_user; otherwise null", "confidence": 0.0-1.0, "reasoning": "one short sentence" }

confidence: 0.9-1.0 = you clearly see the target and the action is obvious; 0.7-0.9 = likely but you're inferring; below 0.7 = you are unsure (a stronger model may take over, so do NOT fake high confidence). This is your certainty in the ACTION YOU CHOSE — when "scroll", "wait", or "ask_user" is clearly the right move, report it with HIGH confidence rather than reflexively lowballing just because the page is uncertain.

HARD RULES — breaking ANY of these fails the task:
1. Output ONLY the JSON object. No prose, no markdown, no code fences — before or after it.
2. NEVER click or type on money-moving controls: redeem, redemption, deposit, withdraw, withdrawal, transfer, cashout, cash out, cashier, payout (or their visual equivalents). A human handles money. If the task asks for one, return {"action":"done","coordinates":null,"selector_hint":null,"text":null,"confidence":1.0,"reasoning":"money_action_requires_human"}.
3. NEVER read, copy, or echo a password. Treat any password field's contents as a black box; never put password text in reasoning or text.
4. NEVER invent coordinates. If you can't see the target, choose "wait" or lower your confidence — do not guess.
If a recent step zoomed in, the current screenshot IS that close-up — give coordinates within the image you see; they are mapped back to the full page for you.
"""


FEW_SHOT_EXAMPLES = [
    {
        "user": (
            "Task: log in as user@example.com\n"
            "URL: https://example.com/login\n"
            "Recent steps: (none)\n"
            "Screenshot shows: an email field at roughly (400, 280), a password "
            "field at (400, 340), and a Sign In button at (400, 410)."
        ),
        "assistant": json.dumps(
            {
                "action": "type",
                "coordinates": [400, 280],
                "selector_hint": "Email input field, centered above the password field",
                "text": "user@example.com",
                "confidence": 0.92,
                "reasoning": "Email field is clearly visible and labeled; task requires entering the email first.",
            }
        ),
    },
    {
        "user": (
            "Task: subscribe to the newsletter\n"
            "URL: https://example.com/\n"
            "Recent steps: 1. click newsletter_icon -> ok\n"
            "Screenshot shows: a modal with a green 'Subscribe' button at (512, 384)."
        ),
        "assistant": json.dumps(
            {
                "action": "click",
                "coordinates": [512, 384],
                "selector_hint": "Green Subscribe button inside the newsletter modal",
                "text": None,
                "confidence": 0.88,
                "reasoning": "The newsletter modal is open and the Subscribe button is the only call-to-action; clicking it completes the flow.",
            }
        ),
    },
    {
        "user": (
            "Task: go to the GitHub homepage\n"
            "URL: about:blank\n"
            "Recent steps: (none)\n"
            "Screenshot shows: an empty blank tab, nothing to click."
        ),
        "assistant": json.dumps(
            {
                "action": "navigate",
                "coordinates": None,
                "selector_hint": "Load the GitHub homepage URL directly",
                "text": "https://github.com",
                "confidence": 0.97,
                "reasoning": "The tab is blank; the fastest way to reach GitHub is to navigate straight to its URL.",
            }
        ),
    },
    {
        "user": (
            "Task: open my profile\n"
            "URL: https://example.com/dashboard\n"
            "Recent steps: 1. click sign_in -> ok\n"
            "Screenshot shows: a mostly-blank page with a loading spinner in the center; no buttons rendered yet."
        ),
        "assistant": json.dumps(
            {
                "action": "wait",
                "coordinates": None,
                "selector_hint": "Page is still loading (spinner visible)",
                "text": None,
                "confidence": 0.9,
                "reasoning": "The page is mid-load with only a spinner; wait for it to finish rendering before acting.",
            }
        ),
    },
    {
        "user": (
            "Task: read the refund policy\n"
            "URL: https://example.com/help\n"
            "Recent steps: 1. navigate help -> ok\n"
            "Screenshot shows: the top of a long help page; 'Shipping' and "
            "'Returns' headings are visible but there's no 'Refund' section on screen yet."
        ),
        "assistant": json.dumps(
            {
                "action": "scroll",
                "coordinates": [0, 600],
                "selector_hint": "Scroll down toward the refund section further down the page",
                "text": None,
                "confidence": 0.85,
                "reasoning": "The refund section isn't on screen; scroll down to bring it into view.",
            }
        ),
    },
    {
        "user": (
            "Task: book a dinner reservation\n"
            "URL: https://example.com/reserve\n"
            "Recent steps: 1. navigate reserve -> ok\n"
            "Screenshot shows: a reservation form asking for party size and time, "
            "but the task never said how many people or what time."
        ),
        "assistant": json.dumps(
            {
                "action": "ask_user",
                "coordinates": None,
                "selector_hint": None,
                "text": "How many people, and what time would you like the reservation?",
                "confidence": 0.95,
                "reasoning": "The form needs a party size and time the task didn't specify; ask rather than guess.",
            }
        ),
    },
    {
        "user": (
            "Task: click the Submit button on the form\n"
            "URL: https://example.com/form\n"
            "Recent steps: 1. navigate form -> ok\n"
            "Screenshot shows: a long form with several small buttons at the "
            "bottom; the button labels are too small to read at this resolution."
        ),
        "assistant": json.dumps(
            {
                "action": "zoom",
                "coordinates": [512, 700],
                "selector_hint": "Small buttons at the bottom of the form",
                "text": None,
                "confidence": 0.8,
                "reasoning": "The button labels are too small to read; zooming in to identify the correct one.",
            }
        ),
    },
    {
        "user": (
            "Task: withdraw $50 from example.com\n"
            "URL: https://example.com/cashier\n"
            "Recent steps: 1. navigate cashier -> ok\n"
            "Screenshot shows: a Withdraw button next to a balance display."
        ),
        "assistant": json.dumps(
            {
                "action": "done",
                "coordinates": None,
                "selector_hint": None,
                "text": None,
                "confidence": 1.0,
                "reasoning": "money_action_requires_human",
            }
        ),
    },
]


def build_user_prompt(
    task_context: str,
    page_url: str,
    step_history: Iterable[StepHistoryItem],
) -> str:
    """User-message text. Screenshot is attached as a separate image part.

    History is already truncated to the last 5 by the caller — keep it that way.
    """
    history_lines: list[str] = []
    for i, step in enumerate(step_history, start=1):
        target = step.target or "-"
        history_lines.append(
            f"{i}. {step.action} {target} -> {step.outcome} (at {step.at})"
        )
    history_block = "\n".join(history_lines) if history_lines else "(none)"

    return (
        f"Task: {task_context}\n"
        f"URL: {page_url}\n"
        f"Recent steps:\n{history_block}\n\n"
        "Look at the attached screenshot and return the next action as a single JSON object."
    )
