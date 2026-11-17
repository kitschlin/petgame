"""Agent 的主循环（ReAct）：LLM 决定调哪些工具，程序执行工具并回填结果，直到 LLM 调用结束工具。

    messages → LLM → tool_calls → 执行只读工具（结果带编号 E1、E2…）→ 回填 → LLM → … → submit_*

- 预算：工具调用次数和总时长。只剩最后一次机会（或时间用掉大半）时，只把结束工具交给 LLM，逼它提交。
- 一次回复里有多个调用时按顺序执行；遇到结束工具就停，后面的调用不再执行。
- LLM 只回文字、不调工具时提醒一次；再不调就算这次请求失败。
- 结束工具的参数原样交回调用方，由调用方的 guard 校验；不合格算一次请求失败（AdviceFailure），
  失败原因附在下一次请求里（见 AGENTS.md 第八节第 4 条）。
- trace.steps 记录每一步：LLM 写的想法、调用的工具、参数、结果摘要、耗时。前端靠它显示「思考过程」。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..draft import AdviceFailure
from ..llm import LLM

INVALID = "invalid"
SUMMARY_LIMIT = 160  # trace 里每个工具结果保留多少字


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any]], str] | None = None  # None 表示结束工具（submit_*）

    @property
    def final(self) -> bool:
        return self.run is None

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


@dataclass(frozen=True)
class Budget:
    calls: int  # 最多调用几次工具（含结束工具）
    seconds: float  # 总时长；用掉 FORCE_SHARE 之后只允许提交
    max_tokens: int | None = None  # 每次 LLM 回复的输出上限
    timeout: float | None = None  # 每次 LLM 请求的超时


FORCE_SHARE = 0.6


@dataclass
class Outcome:
    """一次 Agent 运行的结果。messages 是这次新增的消息（会话模式要把它们接到对话后面）。"""

    final: str
    args: dict[str, Any]
    messages: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    evidence: dict[str, str]  # 编号 → 工具结果全文（guard 核对数字用）
    llm_calls: int = 0
    llm_ms: int = 0
    tool_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    def trace(self) -> dict[str, Any]:
        return {"steps": self.steps, "llm_calls": self.llm_calls, "llm_ms": self.llm_ms,
                "tool_ms": self.tool_ms, "usage": self.usage}


def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise ValueError("参数不是合法的 JSON") from None
    if not isinstance(value, dict):
        raise ValueError("参数必须是 JSON 对象")
    return value


def run(llm: LLM, messages: list[dict[str, Any]], tools: list[Tool], budget: Budget,
        hint: dict[str, Any] | None = None, clock: Callable[[], float] = time.monotonic,
        first_evidence: int = 1) -> Outcome:
    """跑一次 Agent。messages 不会被修改；LLMError 原样抛出，内容问题抛 AdviceFailure。

    first_evidence：编号从几开始（会话模式里接着上一轮的编号，避免同一段对话里出现两个 E1）。
    """
    finals = [tool for tool in tools if tool.final]
    if len(finals) != 1:
        raise ValueError("必须有且只有一个结束工具")
    final = finals[0]
    by_name = {tool.name: tool for tool in tools}
    hint = hint if hint is not None else {}
    start = clock()
    added: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    out = Outcome(final.name, {}, added, steps, evidence)
    used = 0
    nudged = False
    number = first_evidence

    while True:
        elapsed = clock() - start
        if elapsed > budget.seconds:
            raise AdviceFailure("timeout", "AI 思考超时", feedback="上一次思考时间太长：少调用工具，尽快提交。")
        if out.llm_calls >= budget.calls + 2:  # 只给了结束工具还不提交：不再等
            raise AdviceFailure(INVALID, "AI 没有给出建议", feedback=f"必须调用 {final.name} 提交结果。")
        force = used >= budget.calls - 1 or elapsed > budget.seconds * FORCE_SHARE
        offered = [final] if force else tools
        reply = llm.chat(messages + added, [tool.schema() for tool in offered], hint=hint,
                         max_tokens=budget.max_tokens, timeout=budget.timeout)
        out.llm_calls += 1
        out.llm_ms += reply.ms
        _add_usage(out.usage, reply.usage)
        message = reply.message
        added.append(message)
        thought = (message.get("content") or "").strip()
        if thought:
            steps.append({"kind": "thought", "text": thought[:300]})
        calls = message.get("tool_calls") or []
        if not calls:
            if nudged:
                raise AdviceFailure(INVALID, "AI 没有给出建议",
                                    feedback=f"上一次你只回复了文字。必须调用 {final.name} 提交结果。")
            nudged = True
            added.append({"role": "user", "content": f"请继续：用工具收集信息，或者直接调用 {final.name} 提交。"})
            continue

        for call in calls:
            name = call["function"]["name"]
            call_id = call.get("id") or f"call_{len(added)}"
            used += 1
            try:
                args = _arguments(call["function"].get("arguments"))
            except ValueError as error:
                if name == final.name:
                    raise AdviceFailure(INVALID, "AI 输出无法解析", feedback=f"{final.name} 的{error}。") from None
                added.append({"role": "tool", "tool_call_id": call_id, "content": f"调用失败：{error}"})
                continue
            if name == final.name:
                out.args = args
                steps.append({"kind": "submit", "tool": name, "args": args})
                added.append({"role": "tool", "tool_call_id": call_id, "content": "已提交。"})
                # 同一条回复里结束工具之后的调用不再执行，但对话格式要求每个调用都有回应
                for rest in calls[calls.index(call) + 1:]:
                    added.append({"role": "tool", "tool_call_id": rest.get("id") or "", "content": "未执行：已经提交。"})
                return out
            tool = by_name.get(name)
            if tool is None or tool not in offered:
                content = f"没有这个工具：{name}" if tool is None else f"现在只能调用 {final.name} 提交。"
                added.append({"role": "tool", "tool_call_id": call_id, "content": content})
                steps.append({"kind": "tool", "tool": name, "args": args, "error": content})
                continue
            began = clock()
            try:
                result = tool.run(args)
            except (KeyError, TypeError, ValueError) as error:
                result, ok = f"调用失败：{error}", False
            else:
                ok = True
            ms = int((clock() - began) * 1000)
            out.tool_ms += ms
            if ok:
                ref = f"E{number}"
                number += 1
                evidence[ref] = result
                content = f"[{ref}] {result}"
                steps.append({"kind": "tool", "tool": name, "args": args, "id": ref,
                              "summary": result[:SUMMARY_LIMIT], "ms": ms})
            else:
                content = result
                steps.append({"kind": "tool", "tool": name, "args": args, "error": result, "ms": ms})
            added.append({"role": "tool", "tool_call_id": call_id, "content": content})
