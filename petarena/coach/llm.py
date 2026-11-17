"""LLM 客户端：真实接口（httpx → DeepSeek 兼容的 /chat/completions）和 Fake 模式。

调用层只负责「发请求、分类错误」，不关心内容对不对；解析、校验、重试由上层负责。
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


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, hint: dict[str, Any] | None = None) -> LLMReply:
        """system / user 是两条消息；hint 是同一局势的结构化副本，只有 FakeLLM 使用。"""


class HttpLLM:
    def __init__(self, config: CoachConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self.name = config.model
        self._client = httpx.Client(timeout=config.timeout, transport=transport)

    def complete(self, system: str, user: str, hint: dict[str, Any] | None = None) -> LLMReply:
        config = self.config
        body = {
            "model": config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # 该模型默认开启思考，必须显式关闭，否则延迟成倍增加
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        start = time.monotonic()
        try:
            response = self._client.post(
                f"{config.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {config.api_key}"},
                json=body,
            )
        except httpx.TimeoutException:
            raise LLMError(TIMEOUT, "AI 响应超时", True) from None
        except httpx.HTTPError as error:
            raise LLMError(LLM_ERROR, f"网络错误：{type(error).__name__}", True) from None
        ms = int((time.monotonic() - start) * 1000)

        status = response.status_code
        if status in (401, 403):
            raise LLMError(AUTH, "AI 未配置或 key 无效", False)
        if status == 429 or status >= 500:
            raise LLMError(LLM_ERROR, f"AI 服务暂时不可用（{status}）", True)
        if status != 200:
            raise LLMError(LLM_ERROR, f"AI 请求被拒绝（{status}）", False)
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError(LLM_ERROR, "AI 返回的内容格式不对", True) from None
        usage = data.get("usage") or {}
        return LLMReply(
            text=text,
            finish_reason=choice.get("finish_reason") or "stop",
            usage={key: usage[key] for key in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens")
                   if isinstance(usage.get(key), int)},
            ms=ms,
        )

    def close(self) -> None:
        self._client.close()


class FakeLLM:
    """不联网的假模型：按固定规则从组合经验库里挑方案，用于演示和测试。

    fail_first > 0 时，同一手的前 N 次调用返回无法解析的内容，用来演示重试。
    """

    name = "fake"

    def __init__(self, knowledge, fail_first: int = 0, delay: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.knowledge = knowledge
        self.fail_first = fail_first
        self.delay = delay
        self._sleep = sleep
        self._calls: dict[str, int] = {}

    def complete(self, system: str, user: str, hint: dict[str, Any] | None = None) -> LLMReply:
        hint = hint or {}
        if self.delay:
            self._sleep(self.delay)
        key = str(hint.get("token"))
        self._calls[key] = self._calls.get(key, 0) + 1
        if self._calls[key] <= self.fail_first:
            return LLMReply(text="（假模型：这次故意输出了无法解析的内容）")
        answer = {"battle": self._battle, "review": self._review, "growth": self._growth}.get(
            hint.get("kind"), self._plans)(hint)
        return LLMReply(text=json.dumps(answer, ensure_ascii=False))

    @staticmethod
    def _battle(hint: dict[str, Any]) -> dict[str, Any]:
        """战斗指导：选引擎排在第一的候选，理由照抄它的直接后果。"""
        first = hint["candidates"][0]
        return {"skill": first["skill"], "target": first["target"],
                "reason": f"（假模型）{first['text']}：{first['facts']}"[:90], "risk": ""}

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

    def _plans(self, hint: dict[str, Any]) -> dict[str, Any]:
        from ..engine.pets import SPECIES_ORDER

        mine: list[str] = list(hint.get("my_picked", []))
        opp: list[str] = list(hint.get("opp_picked", []))
        predicted = (opp + [key for key in ("cat", "dog", "snake", "deer", "turtle") if key not in opp])[:3]
        plans = []
        combos = self.knowledge.search_combo_experience(
            include=mine, against=opp, limit=5, opp_policy=hint.get("opp_policy", "heuristic"))
        for combo in combos:
            order = combo.get("order") or combo["members"]
            rest = [key for key in order if key not in mine]
            plans.append({
                "lineup": mine + rest, "predicted_opp": predicted,
                "style": combo["style"][:12], "reason": combo["synergy"][:80],
            })
        if not plans:  # 经验库里没有包含已选宠物的组合：按固定优先级补齐
            priority = ["cat", "dog", "snake", "mouse", "fish", "rabbit", "turtle", "deer"]
            rest = [key for key in priority if key not in mine and key in SPECIES_ORDER]
            plans.append({
                "lineup": (mine + rest)[:3], "predicted_opp": predicted,
                "style": "补足输出", "reason": "已选宠物没有现成的组合经验，优先补上输出位",
            })
        return {"opp_read": "（假模型）对手倾向于厚血持久战", "plans": plans}
