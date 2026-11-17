"""LLM 客户端：真实接口（httpx → OpenAI 兼容的 /chat/completions：DeepSeek 官方、SiliconFlow 等）和测试用的 Fake 模式。

两种调用：complete（一问一答，要求 JSON 输出：复盘讲解、培养建议）和 chat（带工具的多轮对话的一步：
选将、战斗 Agent，见 agentic/runtime.py）。调用层只负责「发请求、分类错误」，不关心内容对不对；
解析、校验、重试由上层负责。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

from .config import CoachConfig

# 失败原因（对应接口返回的 code）
TIMEOUT = "timeout"
LLM_ERROR = "llm_error"
AUTH = "auth"


class LLMError(Exception):
    """一次调用失败。retryable 表示自动重试是否有意义。"""

    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class LLMReply:
    text: str
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    ms: int = 0


@dataclass
class ChatReply:
    """一次带工具的对话回复：message 是 assistant 消息原样（content / tool_calls）。"""

    message: dict[str, Any]
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    ms: int = 0


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, hint: dict[str, Any] | None = None) -> LLMReply:
        """system / user 是两条消息；hint 是同一局势的结构化副本，只有 FakeLLM 使用。"""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
             hint: dict[str, Any] | None = None, max_tokens: int | None = None,
             timeout: float | None = None) -> ChatReply:
        """多轮 function calling 的一步。hint 同上，只有 FakeLLM 使用。"""


# SiliconFlow 上支持 enable_thinking 开关的混合思考模型（其余模型不认这个参数）
_HYBRID_THINKING = ("qwen3", "deepseek-v3.1", "deepseek-v3.2", "deepseek-v4", "glm-4.5", "glm-4.6", "glm-5",
                    "hunyuan", "hy4", "kimi-k2")


def is_reasoner(model: str) -> bool:
    """只会先思考再回答的推理模型（DeepSeek-R1 一类）：关不掉思考，也不一定支持 JSON 模式。"""
    name = model.lower()
    return "-r1" in name or name.endswith("reasoner") or "thinking" in name


def provider_options(base_url: str, model: str, json_mode: bool = True) -> dict[str, Any]:
    """不同服务商关闭思考、要求 JSON 输出的写法不同；只放对方认识的参数。

    json_mode=False 用于带工具的对话：输出走 tool_calls，不能再要求整条回复是 JSON。
    """
    name = model.lower()
    options: dict[str, Any] = {}
    # DeepSeek 官方接口（模型名不带「组织/」前缀，如 deepseek-flash）；SiliconFlow 的模型名都是「组织/模型」
    if "api.deepseek.com" in base_url or ("/" not in name and name.startswith("deepseek")):
        # 该模型默认开启思考，必须显式关闭，否则延迟成倍增加
        options["thinking"] = {"type": "disabled"}
    elif any(tag in name for tag in _HYBRID_THINKING) and not is_reasoner(model):
        options["enable_thinking"] = False  # SiliconFlow 等：Qwen3 / DeepSeek-V3.1+ 这类混合思考模型
    if json_mode and not is_reasoner(model):
        options["response_format"] = {"type": "json_object"}  # 推理模型靠提示词 + 本地修复解析 JSON
    return options


class HttpLLM:
    def __init__(self, config: CoachConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self.name = config.model
        self._client = httpx.Client(timeout=config.timeout, transport=transport)

    def _post(self, body: dict[str, Any], timeout: float | None = None) -> tuple[dict[str, Any], int]:
        """发一次请求，按状态码把错误分类成 LLMError。返回 (choices[0]（附上整理过的 usage），耗时毫秒)。"""
        config = self.config
        start = time.monotonic()
        try:
            response = self._client.post(
                f"{config.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {config.api_key}"},
                json=body,
                **({"timeout": timeout} if timeout else {}),
            )
        except httpx.TimeoutException:
            raise LLMError(TIMEOUT, "AI 响应超时", True) from None
        except httpx.HTTPError as error:
            raise LLMError(LLM_ERROR, f"网络错误：{type(error).__name__}", True) from None
        ms = int((time.monotonic() - start) * 1000)

        status = response.status_code
        if status in (401, 403):
            raise LLMError(AUTH, "AI 未配置或 key 无效", False)
        if status == 402:
            raise LLMError(AUTH, "AI 账户余额不足（402）：请充值，或在 .env 换一个 key", False)
        if status == 404 or (status == 400 and "model" in response.text.lower()):
            raise LLMError(LLM_ERROR, f"AI 服务不认识模型 {config.model}（{status}）：检查 .env 里的 COACH_MODEL", False)
        if status == 429 or status >= 500:
            raise LLMError(LLM_ERROR, f"AI 服务暂时不可用（{status}）", True)
        if status != 200:
            raise LLMError(LLM_ERROR, f"AI 请求被拒绝（{status}）：{response.text[:120]}", False)
        try:
            data = response.json()
            choice = data["choices"][0]
            if not isinstance(choice["message"], dict):
                raise TypeError
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError(LLM_ERROR, "AI 返回的内容格式不对", True) from None
        usage = data.get("usage") or {}
        choice["usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens")
                           if isinstance(usage.get(key), int)}
        return choice, ms

    def _limit(self, max_tokens: int | None) -> int:
        limit = max_tokens or self.config.max_tokens
        # 推理模型的思考过程也算在输出里：放宽上限，免得答案被截断
        return limit * 4 if is_reasoner(self.config.model) else limit

    def complete(self, system: str, user: str, hint: dict[str, Any] | None = None) -> LLMReply:
        config = self.config
        body = {
            "model": config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **provider_options(config.base_url, config.model),
            "temperature": config.temperature,
            "max_tokens": self._limit(None),
        }
        choice, ms = self._post(body)
        return LLMReply(text=choice["message"].get("content") or "",
                        finish_reason=choice.get("finish_reason") or "stop", usage=choice["usage"], ms=ms)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
             hint: dict[str, Any] | None = None, max_tokens: int | None = None,
             timeout: float | None = None) -> ChatReply:
        config = self.config
        body = {
            "model": config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            **provider_options(config.base_url, config.model, json_mode=False),
            "temperature": config.temperature,
            "max_tokens": self._limit(max_tokens),
        }
        choice, ms = self._post(body, timeout)
        message = choice["message"]
        kept: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            kept["tool_calls"] = [
                {"id": call.get("id") or f"call_{index}", "type": "function",
                 "function": {"name": call["function"]["name"], "arguments": call["function"].get("arguments") or "{}"}}
                for index, call in enumerate(message["tool_calls"])
                if isinstance(call, dict) and isinstance(call.get("function"), dict) and call["function"].get("name")
            ]
        if message.get("reasoning_content"):  # 推理模型：多轮对话里要原样带回
            kept["reasoning_content"] = message["reasoning_content"]
        return ChatReply(kept, choice.get("finish_reason") or "stop", choice["usage"], ms)

    def close(self) -> None:
        self._client.close()


class FakeLLM:
    """不联网的假模型，只给自动化测试和 e2e 用（必须显式开启，永远不会自动启用）。

    - 选将 / 战斗（chat，带工具）：按固定规则调用工具。选将直接提交 Top10 的第 1 套；战斗直接提交
      推演胜率最高的动作（依据 engine）。它看的是 hint（Agent 放进去的结构化副本），不解析文字。
    - 复盘 / 培养（complete）：照着引擎给的事实编号写。
    fail_first > 0 时，同一手的前 N 次请求故意提交不合法的结果，用来演示重试。
    """

    name = "fake"

    def __init__(self, knowledge=None, fail_first: int = 0, delay: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.knowledge = knowledge
        self.fail_first = fail_first
        self.delay = delay
        self._sleep = sleep
        self._calls: dict[str, int] = {}

    def _failing(self, hint: dict[str, Any]) -> bool:
        """这一次请求要不要故意失败（每个 token 的第一次 LLM 调用计一次请求）。"""
        key = str(hint.get("token"))
        if hint.get("first_call", True):
            self._calls[key] = self._calls.get(key, 0) + 1
        return 0 < self._calls.get(key, 0) <= self.fail_first

    # ------------------------------------------------------------ 带工具的对话

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
             hint: dict[str, Any] | None = None, max_tokens: int | None = None,
             timeout: float | None = None) -> ChatReply:
        hint = hint if hint is not None else {}
        if self.delay:
            self._sleep(self.delay)
        failing = self._failing(hint)
        hint["first_call"] = False
        names = {tool["function"]["name"] for tool in tools}
        if "submit_recommendation" in names:
            rows = hint.get("rows") or []
            first = rows[0] if rows else {"id": "T1"}
            alternatives, picks = [], {first.get("pick")}
            for row in rows[1:]:
                if row["pick"] not in picks and len(alternatives) < 2:
                    picks.add(row["pick"])
                    alternatives.append({"choice": row["id"], "reason": "（假模型）表上胜率靠前的另一套"})
            args = {"choice": "T99" if failing else first["id"],
                    "reason": "（假模型）选 Top10 表里胜率最高的一套", "alternatives": alternatives,
                    "strategy": "（假模型）按表上第一套打"}
            return self._call("submit_recommendation", args)
        moves = hint.get("moves") or []
        if failing or not moves:
            return self._call("submit_move", {"basis": "engine", "skill": "nonsense", "target": None,
                                              "reason": "（假模型）故意出错"})
        skill, target, text = moves[0]
        return self._call("submit_move", {
            "basis": "engine", "skill": skill, "target": target, "reason": f"（假模型）推演最优：{text}"[:90],
            "plan": "（假模型）每一步都照推演最优出招"})

    _ids = 0

    def _call(self, name: str, args: dict[str, Any]) -> ChatReply:
        FakeLLM._ids += 1
        return ChatReply({"role": "assistant", "content": "", "tool_calls": [{
            "id": f"fake_{FakeLLM._ids}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})

    # ------------------------------------------------------------ 一问一答（复盘、培养）

    def complete(self, system: str, user: str, hint: dict[str, Any] | None = None) -> LLMReply:
        hint = hint or {}
        if self.delay:
            self._sleep(self.delay)
        if self._failing(hint):
            return LLMReply(text="（假模型：这次故意输出了无法解析的内容）")
        answer = {"review": self._review, "growth": self._growth}[hint.get("kind")](hint)
        return LLMReply(text=json.dumps(answer, ensure_ascii=False))

    @staticmethod
    def _review(hint: dict[str, Any]) -> dict[str, Any]:
        """复盘讲解：照着引擎给的事实编号写。"""
        moments = hint.get("moments") or []
        lessons = []
        if moments and hint.get("my_lineup"):
            lessons.append({
                "scope": "play", "pets": hint["my_lineup"][:1], "against": (hint.get("opp_lineup") or [])[:1],
                "polarity": "avoid" if moments[0].get("kind") in ("blunder", "mistake") else "do",
                "text": f"（假模型）{moments[0]['text'][:40]}", "evidence": [moments[0]["id"]],
            })
        return {
            "summary": f"（假模型）这一局{hint.get('result', '')}，主要原因：{hint.get('cause', '')}。",
            "moments": [{"id": moment["id"], "comment": f"（假模型）{moment['text'][:40]}"} for moment in moments[:3]],
            "tips": ["（假模型）先看复盘里损失最大的那一步，下次照着推演最优的动作打。"],
            "lessons": lessons,
        }

    @staticmethod
    def _growth(hint: dict[str, Any]) -> dict[str, Any]:
        """培养建议：照着引擎给的路线逐步说明。"""
        steps = hint.get("steps") or []
        return {
            "summary": f"（假模型）{hint.get('headline', '')}"[:60],
            "steps": [{"id": step["id"], "why": f"（假模型）{step['text']}"[:50]} for step in steps],
            "stop": "（假模型）练到路线里写的数值就停，再往上没有额外收益。" if steps else "（假模型）现在不用练。",
            "avoid": "（假模型）「别练」清单里的项目会让胜算下降。" if hint.get("avoid") else "",
        }
