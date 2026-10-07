"""Thin Claude Messages API client (no SDK dependency) with cost tracking and a budget cap."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .config import settings

API_URL = "https://api.anthropic.com/v1/messages"


class BudgetExceeded(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1e6 * settings.PRICE_IN_PER_M
            + self.output_tokens / 1e6 * settings.PRICE_OUT_PER_M
        )

    def add(self, u: dict) -> None:
        self.input_tokens += int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0)
        self.output_tokens += int(u.get("output_tokens", 0))
        self.calls += 1


@dataclass
class LLM:
    budget_usd: float = settings.BUDGET_USD_PER_TEST
    usage: Usage = field(default_factory=Usage)
    model: str = settings.MODEL
    mock: Callable[[list[dict], list[dict] | None], dict] | None = None

    # ---- raw call -------------------------------------------------------
    def message(self, system: str, messages: list[dict], tools: list[dict] | None = None,
                max_tokens: int = 4096, temperature: float = 0.2, force: bool = False) -> dict:
        if not force and self.usage.cost_usd >= self.budget_usd:
            raise BudgetExceeded(f"spent ${self.usage.cost_usd:.3f} of ${self.budget_usd:.2f}")
        if self.mock is not None:
            resp = self.mock(messages, tools)
            self.usage.add(resp.get("usage", {"input_tokens": 500, "output_tokens": 200}))
            return resp
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env)")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        headers = {
            "x-api-key": settings.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                r = requests.post(API_URL, headers=headers, json=body, timeout=180)
                if r.status_code in (429, 500, 502, 503, 529):
                    last_err = RuntimeError(f"API {r.status_code}: {r.text[:300]}")
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"API {r.status_code}: {r.text[:500]}")
                data = r.json()
                self.usage.add(data.get("usage", {}))
                return data
            except requests.RequestException as e:  # network hiccup
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Claude API failed after retries: {last_err}")

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def text_of(resp: dict) -> str:
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")

    def json_call(self, system: str, prompt: str, max_tokens: int = 4096, force: bool = False) -> Any:
        """Ask for JSON and parse it robustly (handles ```json fences and stray prose)."""
        resp = self.message(system, [{"role": "user", "content": prompt}], max_tokens=max_tokens, force=force)
        return parse_json(self.text_of(resp))

    def tool_loop(self, system: str, messages: list[dict], tools: list[dict],
                  handler: Callable[[str, dict], str], max_steps: int,
                  on_event: Callable[[str, dict], None] | None = None,
                  max_tokens: int = 4096) -> tuple[str, list[dict]]:
        """Run a tool-use loop. Returns (final_text, transcript_of_tool_calls)."""
        transcript: list[dict] = []
        steps = 0
        while True:
            try:
                resp = self.message(system, messages, tools=tools, max_tokens=max_tokens)
            except BudgetExceeded as e:
                transcript.append({"tool": "_budget", "input": {}, "output": str(e)})
                return f"[budget exhausted: {e}]", transcript
            messages.append({"role": "assistant", "content": resp.get("content", [])})
            tool_uses = [b for b in resp.get("content", []) if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not tool_uses:
                return self.text_of(resp), transcript
            results = []
            for tu in tool_uses:
                steps += 1
                name, inp = tu["name"], tu.get("input", {})
                if steps > max_steps:
                    out = "ERROR: tool-call limit reached. Stop testing and write your final report now."
                else:
                    try:
                        out = handler(name, inp)
                    except Exception as e:  # tools must never kill the run
                        out = f"ERROR: {type(e).__name__}: {e}"
                out = str(out)
                if len(out) > 20000:
                    out = out[:20000] + "\n...[truncated]"
                transcript.append({"tool": name, "input": inp, "output": out[:4000]})
                if on_event:
                    on_event("tool", {"name": name, "input": inp, "preview": out[:200]})
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out})
            messages.append({"role": "user", "content": results})
            if steps > max_steps + len(tool_uses):
                # Force a wrap-up turn without tools.
                messages.append({"role": "user", "content": "Write the final report now. No more tools."})
                resp = self.message(system, messages, max_tokens=max_tokens, force=True)
                return self.text_of(resp), transcript


def parse_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # find first { or [ and matching tail
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"model did not return JSON: {text[:200]}")
