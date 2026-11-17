"""LLM 代理的主循环：多轮 function calling，直到对局结束。

    [system, user(开局局面)] → LLM → tool_calls → 执行（pick / act 交给引擎）→ tool 结果 → LLM → ...

- 整局一段对话，LLM 看得到自己之前的判断和全部事件。
- 一次回复里只执行第一个 pick / act；后面的动作类调用被拒绝（局面已经变了）。
- LLM 只回文字不调工具时，提醒它继续；连续提醒太多次或调用次数超过上限就中止（记为未完成，
  不会退回到任何内置打法替它下完）。
- 每次 LLM 请求、每次工具调用都写进 transcript.jsonl。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..coach.config import CoachConfig
from .env import ACTIONS, AgentEnv

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "system.md").read_text(encoding="utf-8").strip()


class ChatError(Exception):
    pass


@dataclass
class ChatReply:
    message: dict[str, Any]
    finish_reason: str
    usage: dict[str, Any]
    ms: int


class ChatClient:
    """OpenAI 兼容的 /chat/completions，带 tools。DeepSeek 官方接口用 thinking 开关。"""

    def __init__(self, config: CoachConfig, *, thinking: bool = False, max_tokens: int = 2000,
                 timeout: float = 120.0, temperature: float = 0.3, retries: int = 4) -> None:
        self.config = config
        self.model = config.model
        self.thinking = thinking
        self.max_tokens = max_tokens * 4 if thinking else max_tokens
        self.temperature = temperature
        self.retries = retries
        self._client = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             tool_choice: str = "auto") -> ChatReply:
        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "tools": tools, "tool_choice": tool_choice,
            "max_tokens": self.max_tokens,
        }
        if "api.deepseek.com" in self.config.base_url:
            body["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        elif not self.thinking:
            body["enable_thinking"] = False
        if not self.thinking:
            body["temperature"] = self.temperature
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            start = time.monotonic()
            try:
                response = self._client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"}, json=body)
            except httpx.HTTPError as error:
                problem = f"{type(error).__name__}"
            else:
                if response.status_code == 200:
                    data = response.json()
                    choice = data["choices"][0]
                    return ChatReply(choice["message"], choice.get("finish_reason") or "stop",
                                     data.get("usage") or {}, int((time.monotonic() - start) * 1000))
                problem = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in (429,) and response.status_code < 500:
                    raise ChatError(problem)
            if attempt == self.retries:
                raise ChatError(problem)
            time.sleep(delay)
            delay *= 2
        raise ChatError("unreachable")

    def close(self) -> None:
        self._client.close()


@dataclass
class RunStats:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    reasoning_tokens: int = 0
    llm_ms: int = 0
    tool_ms: int = 0
    nudges: int = 0
    rejected_extra_actions: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)

    def add_usage(self, usage: dict[str, Any], ms: int) -> None:
        self.llm_calls += 1
        self.llm_ms += ms
        self.prompt_tokens += usage.get("prompt_tokens") or 0
        self.completion_tokens += usage.get("completion_tokens") or 0
        self.cache_hit_tokens += usage.get("prompt_cache_hit_tokens") or 0
        self.reasoning_tokens += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """原样带回 assistant 消息（含 tool_calls；思考模式下 DeepSeek 要求带回 reasoning_content）。"""
    kept: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        kept["tool_calls"] = message["tool_calls"]
    if message.get("reasoning_content"):
        kept["reasoning_content"] = message["reasoning_content"]
    return kept


def run_game(env: AgentEnv, client: ChatClient, out: Path, *, max_calls: int = 200, max_nudges: int = 6,
             log=print) -> dict[str, Any]:
    """让 LLM 打完一局。out 目录下写 transcript.jsonl、messages.json。返回统计。"""
    out.mkdir(parents=True, exist_ok=True)
    transcript = (out / "transcript.jsonl").open("w", encoding="utf-8")
    stats = RunStats()
    tools = env.tools()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": env.opening_text()},
    ]
    started = time.monotonic()
    error: str | None = None

    def write(record: dict[str, Any]) -> None:
        transcript.write(json.dumps({"t": round(time.monotonic() - started, 2), **record}, ensure_ascii=False) + "\n")
        transcript.flush()

    write({"type": "start", "seed": env.seed, "tools": list(env.tool_names), "model": client.model,
           "thinking": client.thinking, "system": SYSTEM_PROMPT, "user": messages[1]["content"]})
    try:
        while not env.finished:
            if stats.llm_calls >= max_calls:
                error = f"LLM 调用次数超过上限 {max_calls}"
                break
            reply = client.chat(messages, tools)
            stats.add_usage(reply.usage, reply.ms)
            message = reply.message
            calls = message.get("tool_calls") or []
            write({"type": "llm", "call": stats.llm_calls, "ms": reply.ms, "finish_reason": reply.finish_reason,
                   "usage": reply.usage, "content": message.get("content"),
                   "reasoning": message.get("reasoning_content"), "tool_calls": calls})
            messages.append(_assistant_message(message))
            if not calls:
                stats.nudges += 1
                if stats.nudges > max_nudges:
                    error = "LLM 多次只回复文字、不调用工具"
                    break
                phase = "选将（pick）" if env.match.battle is None else "行动（act）"
                messages.append({"role": "user", "content": f"对局还没有结束，现在轮到你{phase}。请调用工具继续。"})
                continue
            acted = False
            rationale = (message.get("content") or "").strip()
            for call in calls:
                name = call["function"]["name"]
                stats.tool_calls[name] = stats.tool_calls.get(name, 0) + 1
                tool_start = time.monotonic()
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("参数必须是 JSON 对象")
                except ValueError as parse_error:
                    result = {"ok": False, "text": f"参数不是合法的 JSON：{parse_error}"}
                    args = call["function"].get("arguments")
                else:
                    if name in ACTIONS and acted:
                        stats.rejected_extra_actions += 1
                        result = {"ok": False, "text": "这次回复里已经执行过一个 pick / act，局面已经变了；"
                                                       "这个调用没有执行，请先看上一个调用返回的新局面。"}
                    else:
                        result = env.call(name, args, rationale)
                        acted = acted or (name in ACTIONS and result["ok"])
                ms = int((time.monotonic() - tool_start) * 1000)
                stats.tool_ms += ms
                write({"type": "tool", "name": name, "args": args, "ok": result["ok"], "ms": ms,
                       "result": result["text"]})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result["text"]})
                if name in ACTIONS and result["ok"]:
                    decision = env.decisions[-1]
                    log(f"  [{out.name}] {decision['kind']} {decision['choice']}"
                        + (f" (rank {decision['audit']['rank']}/{decision['audit']['moves']})"
                           if decision.get("audit") else ""))
        if env.finished:  # 让它用一两句话总结这局（不再执行任何工具）
            reply = client.chat(messages, tools, tool_choice="none")
            stats.add_usage(reply.usage, reply.ms)
            messages.append(_assistant_message(reply.message))
            write({"type": "llm", "call": stats.llm_calls, "ms": reply.ms, "final": True,
                   "usage": reply.usage, "content": reply.message.get("content"),
                   "reasoning": reply.message.get("reasoning_content")})
    except ChatError as chat_error:
        error = f"LLM 请求失败：{chat_error}"
    finally:
        wall = round(time.monotonic() - started, 1)
        write({"type": "end", "error": error, "wall_s": wall})
        transcript.close()
        (out / "messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"error": error, "wall_s": wall, "illegal": env.illegal, **stats.__dict__,
            "final_comment": (messages[-1].get("content") if env.finished and messages[-1]["role"] == "assistant" else None)}
