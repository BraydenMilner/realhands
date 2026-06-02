"""ChatRunner — read-only Q&A agent that answers questions about the current page.

Reuses the BYO-key model config (_load_vision / _build_vision_config) but never
actuates the browser. Tools: read_current_page, view_screenshot, web_search
(always registered, free keyless default), fetch_url (always registered).
Publishes answers over the existing SSE /events stream as {type:"chat", ...} events.
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
    "You can read their current page, search the web, and fetch URLs to answer questions. "
    "Prefer information from the current page when the question is about what's on screen. "
    "Answer concisely."
)

_MAX_TOOL_ITERATIONS = 5
_MAX_PAGE_TEXT_CHARS = 6000
_MAX_FETCH_CHARS = 6000


def _build_tools() -> list[dict]:
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
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Fetch and extract the text content of a web page URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch.",
                        }
                    },
                    "required": ["url"],
                },
            },
        },
    ]
    return tools


def _resolve_search_provider() -> str:
    explicit = os.environ.get("REALHANDS_SEARCH_PROVIDER", "").lower()
    if explicit in ("ddg", "searxng", "tavily", "brave"):
        return explicit
    if os.environ.get("REALHANDS_SEARXNG_URL"):
        return "searxng"
    if os.environ.get("REALHANDS_TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("REALHANDS_BRAVE_API_KEY"):
        return "brave"
    return "ddg"


def _search_ddg(query: str) -> list[dict]:
    try:
        from ddgs import DDGS
    except Exception:
        return []
    with DDGS() as ddgs:
        return ddgs.text(query, max_results=5)


def _search_searxng(query: str) -> list[dict]:
    import httpx

    base = os.environ.get("REALHANDS_SEARXNG_URL", "").rstrip("/")
    resp = httpx.get(f"{base}/search", params={"q": query, "format": "json"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for r in data.get("results", [])[:5]:
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
        )
    return out


def _search_tavily(query: str) -> list[dict]:
    import httpx

    api_key = os.environ.get("REALHANDS_TAVILY_API_KEY", "")
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": 5},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for r in data.get("results", [])[:5]:
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
        )
    return out


def _search_brave(query: str) -> list[dict]:
    import httpx

    api_key = os.environ.get("REALHANDS_BRAVE_API_KEY", "")
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query},
        headers={"X-Subscription-Token": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for r in data.get("web", {}).get("results", [])[:5]:
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
            }
        )
    return out


_DDGS_GRACEFUL = (
    "Web search is temporarily unavailable (free DuckDuckGo backend can rate-limit). "
    "Answer from the page or your own knowledge, or set REALHANDS_TAVILY_API_KEY / "
    "REALHANDS_SEARXNG_URL for reliable search."
)

_PROVIDER_GRACEFUL = "Web search failed ({provider}). Answer from the page or your own knowledge."


def _format_results(results: list[dict]) -> str:
    if not results:
        return "No search results found."
    parts = []
    for r in results[:5]:
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        parts.append(f"- {title}\n  {url}\n  {snippet}")
    return "\n".join(parts)


async def _tool_web_search(query: str) -> str:
    provider = _resolve_search_provider()
    try:
        if provider == "ddg":
            raw = _search_ddg(query)
            if not raw:
                return _DDGS_GRACEFUL
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href") or r.get("url", ""),
                    "snippet": r.get("body") or r.get("snippet", ""),
                }
                for r in raw
            ]
            return _format_results(results)
        elif provider == "searxng":
            return _format_results(_search_searxng(query))
        elif provider == "tavily":
            return _format_results(_search_tavily(query))
        elif provider == "brave":
            return _format_results(_search_brave(query))
        return _format_results([])
    except Exception:
        if provider == "ddg":
            return _DDGS_GRACEFUL
        return _PROVIDER_GRACEFUL.format(provider=provider)


async def _tool_fetch_url(url: str) -> str:
    import httpx

    try:
        try:
            from trafilatura import extract as _trafilatura_extract
        except Exception:
            _trafilatura_extract = None

        resp = httpx.get(
            url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True
        )
        resp.raise_for_status()
        html = resp.text

        if _trafilatura_extract:
            text = _trafilatura_extract(html)
            if text:
                return f"Fetched: {url}\n{text[:_MAX_FETCH_CHARS]}"

        jina_resp = httpx.get(
            f"https://r.jina.ai/{url}",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        )
        jina_resp.raise_for_status()
        text = jina_resp.text
        if text:
            return f"Fetched: {url}\n{text[:_MAX_FETCH_CHARS]}"

        return "Couldn't fetch that page (it may block bots or require JS)."
    except Exception:
        return "Couldn't fetch that page (it may block bots or require JS)."


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

    tools = _build_tools()

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
    elif name == "fetch_url":
        return await _tool_fetch_url(args.get("url", ""))
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
