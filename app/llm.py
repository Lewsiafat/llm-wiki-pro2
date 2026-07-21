"""OpenRouter client — single entry point for all LLM calls."""
from __future__ import annotations

import json
import os

import httpx

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 8192) -> str:
    """Send a chat completion request via OpenRouter and return the text content."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    model = os.getenv("AI_MODEL", DEFAULT_MODEL)

    resp = httpx.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Lewsiafat/llm-wiki-pro2",
            "X-Title": "llm-wiki-pro2",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=240.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"OpenRouter returned empty content: {json.dumps(data)[:500]}")
    return content.strip()


def _repair_stray_quotes_and_newlines(raw: str) -> str:
    """Best-effort repair for the LLM's most common JSON mistake: leaving a
    literal " or newline inside a long string value unescaped. Walks the text
    tracking string/escape state; a " encountered mid-string is only treated
    as the real terminator if a JSON structural character plausibly follows
    it (: , } ] or end of text) — otherwise it's escaped in place. Raw
    newlines/tabs inside strings are escaped too."""
    out = []
    in_string = False
    escaped = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == '"':
                j = i + 1
                while j < n and raw[j] in " \t\r\n":
                    j += 1
                if j >= n or raw[j] in ":,}]":
                    out.append(ch)
                    in_string = False
                else:
                    out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_string = True
        i += 1
    return "".join(out)


def extract_json(text: str) -> dict:
    """Extract the first JSON object from an LLM response (tolerates code fences / prose)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in LLM response: {text[:300]}")
    raw = text[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # common failure: trailing commas — try a light cleanup
        cleaned = raw.replace(",\n}", "\n}").replace(",\n]", "\n]")
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # tougher failure: unescaped quotes/newlines inside a string value
        try:
            return json.loads(_repair_stray_quotes_and_newlines(raw))
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON from LLM: {e}: {raw[:300]}") from e
