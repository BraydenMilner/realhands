"""ChatRunner — read-only Q&A agent that answers questions about the current page.

Reuses the BYO-key model config (_load_vision / _build_vision_config) but never
actuates the browser. Tools: read_current_page, view_screenshot, web_search
(optional, env-gated). Publishes answers over the existing SSE /events stream
as {type:"chat", ...} events.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

from redaction import redact_text

log = logging.getLogger("agent_bridge.chat")

_SYSTEM_PROMPT = (
    "You are RealHands, a helpful assistant embedded in the user's browser. "
    "You can read their current page and search the web to answer questions. "
    "Prefer information from the current page when the question is about what's on screen. "
    "Answer concisely."
)

_MAX_TOOL_ITERATIONS = 5
_MAX_PAGE_TEXT_CHARS = 6000


def _build_tools(include_search: bool) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_current_page",
                "description": "Get the visible text, title, and URL of the current browser tab.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "view_screenshot",
                "description": "Take a screenshot of the current browser tab to see the visual layout.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]
    if include_search:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web for information.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query.",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        )
    return tools


async def _tool_read_current_page(executor) -> str:
    if executor is None:
        return "No browser page available."
    try:
        result = await executor.call("get_page_text", {}, timeout=15.0)
        if isinstance(result, dict):
            text = result.get("text", "")[:_MAX_PAGE_TEXT_CHARS]
            title = result.get("title", "")
            url = result.get("url", "")
            parts = []
            if url:
                parts.append(f"URL: {url}")
            if title:
                parts.append(f"Title: {title}")
            if text:
                parts.append(f"Page text:\n{text}")
            return "\n".join(parts) if parts else "Page is empty."
        return "Could not read page text."
    except Exception as exc:
        return f"Could not read page text: {exc}"


async def _tool_view_screenshot(executor) -> list[dict]:
    if executor is None:
        return [{"role": "tool", "content": "No browser page available."}]
    try:
        result = await executor.call("screenshot", {}, timeout=15.0)
        if isinstance(result, dict) and "base64" in result:
            b64 = result["base64"]
            url = result.get("url", "")
            content = [
                {
                    "type": "text",
                    "text": f"Screenshot of {url}" if url else "Screenshot of current page",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ]
            return [{"role": "tool", "content": content}]
        return [{"role": "tool", "content": "Screenshot not available."}]
    except Exception as exc:
        return [{"role": "tool", "content": f"Screenshot failed: {exc}"}]


async def _tool_web_search(query: str) -> str:
    import urllib.request
    import json as _json

    api_key = os.environ.get("REALHANDS_SEARCH_API_KEY", "")
    if not api_key:
        return "Web search is not configured."
    try:
        payload = _json.dumps(
            {"api_key": api_key, "query": query, "max_results": 5}
        ).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        results = data.get("results", [])
        if not results:
            return "No search results found."
        parts = []
        for r in results[:5]:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")
            parts.append(f"- {title}\n  {url}\n  {snippet}")
        return "\n".join(parts)
    except Exception as exc:
        return f"Search failed: {exc}"


async def run_chat_turn(
    message: str,
    broker,
    executor=None,
    browser_id: Optional[str] = None,
) -> None:
    """Run one chat turn: call the LLM with tools, publish the answer over SSE.

    Publishes {type:"chat", role:"tool", text, done:false} for tool-use progress,
    and {type:"chat", role:"assistant", text, done:true} for the final answer.
    On error: {type:"chat", role:"error", text, done:true}.
    """
    from agent_runner import _load_vision, _build_vision_config

    try:
        decide_action, VisionConfig, ModelConfig, StepHistoryItem = _load_vision()
        config = _build_vision_config(VisionConfig, ModelConfig)
    except Exception as exc:
        await broker.publish(
            {
                "type": "chat",
                "role": "error",
                "text": f"Model not available: {exc}",
                "done": True,
            }
        )
        return

    import litellm

    model = config.models[0].model if config.models else "gpt-4o-mini"
    api_key = config.models[0].api_key if config.models else None
    base_url = config.models[0].base_url if config.models else None

    include_search = bool(os.environ.get("REALHANDS_SEARCH_API_KEY"))
    tools = _build_tools(include_search)

    litellm_kwargs: dict[str, Any] = {
        "model": model,
        "tools": tools,
        "tool_choice": "auto",
    }
    if api_key:
        litellm_kwargs["api_key"] = api_key
    if base_url:
        litellm_kwargs["api_base"] = base_url

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]

    try:
        for _ in range(_MAX_TOOL_ITERATIONS):
            response = await litellm.acompletion(messages=messages, **litellm_kwargs)
            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                messages.append(choice.message.model_dump())
                for tc in choice.message.tool_calls:
                    tool_name = tc.function.name
                    tool_result = await _execute_tool(tool_name, tc.function.arguments, executor)
                    tool_text = _extract_tool_text(tool_result)
                    await broker.publish(
                        {
                            "type": "chat",
                            "role": "tool",
                            "text": redact_text(f"used {tool_name}: {tool_text[:200]}"),
                            "done": False,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tool_name,
                            "content": tool_text,
                        }
                    )
            else:
                answer = choice.message.content or ""
                await broker.publish(
                    {
                        "type": "chat",
                        "role": "assistant",
                        "text": redact_text(answer),
                        "done": True,
                    }
                )
                return

        await broker.publish(
            {
                "type": "chat",
                "role": "assistant",
                "text": "Sorry, I couldn't complete that query within the tool-call limit.",
                "done": True,
            }
        )
    except Exception as exc:
        log.warning("chat turn failed: %s", exc)
        await broker.publish(
            {
                "type": "chat",
                "role": "error",
                "text": redact_text(str(exc)),
                "done": True,
            }
        )


async def _execute_tool(name: str, arguments_json: str, executor) -> Any:
    import json as _json

    try:
        args = _json.loads(arguments_json) if arguments_json else {}
    except Exception:
        args = {}

    if name == "read_current_page":
        return await _tool_read_current_page(executor)
    elif name == "view_screenshot":
        return await _tool_view_screenshot(executor)
    elif name == "web_search":
        return await _tool_web_search(args.get("query", ""))
    return f"Unknown tool: {name}"


def _extract_tool_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(result)
