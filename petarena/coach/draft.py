"""选将的共用部分：对手画像（Coach 对「对手是谁」的假设）、选将局势，以及几个通用工具。

选将建议本身由 Agent 完成（agentic/draft_agent.py）：程序从经验库算出 Top10 阵容表（agentic/draft_context.py），
LLM 检索组合经验、经验条目，在 Top10 里选一套并讲清理由。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Sequence

from ..engine.pets import SPECIES, SPECIES_ORDER, TEAM_SIZE
from . import opponent as smart_opponent
from .matchup import uniform_replies
from .policy import EXPERT, HEURISTIC, RANDOM_MODE

GAMES_BUDGET_PER_WORKER = 5000  # 现场计算时一次建议最多打多少局（每个进程）
SHORTLIST = 12  # 属性有成长、算不起穷举时，用基础属性的对阵表挑出这么多套最有希望的阵容来现场推演
VERIFY_BELOW = 2000  # 估计不足这么多局时，用新种子复核后再展示
VERIFY_GAMES = 600  # 复核时每套阵容一共打多少局（平摊到对手的各种补法上）

INVALID = "invalid"


# ---------------------------------------------------------------- 对手画像

UNIFORM = "uniform"  # 对手的补选：所有补法等概率
PREDICTED = "predicted"  # 对手的补选：按智能对手的选将习惯预测


@dataclass(frozen=True)
class OpponentProfile:
    """Coach 对「对手是谁」的假设：它怎么出招、接下来会怎么补选、要不要防最坏情况。"""

    opp_policy: str  # 查哪张对阵表（表里对手的打法）
    replies: str  # 对手补选的模型：UNIFORM / PREDICTED
    worst_weight: float  # 「对手最强应对」在排序依据里的权重
    label: str  # 面板上对推演口径的说明
    brief: str  # 写给 LLM 的对手描述
    pool: tuple[str, ...] | None = None  # 对手只会从这几只里选；None 表示 8 只都可能
    model: Any = HEURISTIC  # 逐动作推演（战斗指导、复盘）时假设对手怎么出招
    foe: Any = HEURISTIC  # 整局模拟（后台推演、培养方案）时对手实际怎么出招


PROFILES: dict[str, OpponentProfile] = {
    # 电脑随机：不会针对我方，所以只看对手各种补选的平均，并按它的真实打法推演
    "random": OpponentProfile(
        RANDOM_MODE, UNIFORM, 0.0, "推演估计（我方按常规打法，对手随机选将、随机出招）",
        "电脑随机操作：选将完全随机；出招时只要特色技能能用就用，目标随机。它不会针对我方的选择，也不会集火。",
        model=RANDOM_MODE, foe=RANDOM_MODE,
    ),
    # 电脑智能：8 只都可能用，每一手都针对我方已选的宠物（选将博弈树），补选按它的选将习惯预测，取期望。
    # 这里不再给「最差情况」留权重：预测范围内的每种补法本来就按概率算进去了，再防一手只会错过期望更高的阵容。
    # 它出招用推演，没有现成的对阵表：先按「双方常规打法」的表估计，再用实战和引导推演的经验修正
    "smart": OpponentProfile(
        HEURISTIC, PREDICTED, 0.0, "推演估计（双方按常规打法；按它的选将习惯预测它的补选）",
        "电脑智能操作：8 只宠物都可能用，每一手都会针对我方已选的宠物，挑最能压低我方胜算的一只；"
        "出招时把每个可选动作都推演一遍再选最好的，会集火、会留能量，很少失误。",
        model=("reply", 12, HEURISTIC), foe=EXPERT,  # 它接下来的几次应对也按推演来选（见 rollout.py）
    ),
    # 真人：猜不到他会怎么选，所有补法都算上，最差和平均各占一半
    "manual": OpponentProfile(
        HEURISTIC, UNIFORM, 0.5, "推演估计（双方按常规打法）",
        "玩家手动操作：可能针对我方已选的宠物来选将，出招会集火。",
        model=("noisy", 0.15, HEURISTIC), foe=HEURISTIC,
    ),
}


def profile_for(opponent: str) -> OpponentProfile:
    return PROFILES.get(opponent, PROFILES["manual"])


def reply_model_for(profile: OpponentProfile, opp_picked: Sequence[str]):
    if profile.replies == PREDICTED:
        return smart_opponent.reply_model(opp_picked)
    return uniform_replies(opp_picked)


# ---------------------------------------------------------------- 局势

@dataclass(frozen=True)
class Situation:
    token: str
    step: int  # 已完成的选择数；1、3、5 时轮到我方
    my_picked: tuple[str, ...]
    opp_picked: tuple[str, ...]
    roster: dict[str, dict[str, dict[str, int]]]  # 本局锁定的双方属性
    opponent: str = "manual"

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> "Situation":
        picks = snap["picks"]
        return cls(
            token=snap["token"], step=snap["step"],
            my_picked=tuple(key for side, key in picks if side == "me"),
            opp_picked=tuple(key for side, key in picks if side == "opp"),
            roster=snap["roster"], opponent=snap.get("opponent", "manual"),
        )

    @property
    def hand(self) -> int:
        return self.step + 1

    @property
    def available(self) -> list[str]:
        return [key for key in SPECIES_ORDER if key not in self.my_picked]

    @property
    def need(self) -> int:
        return TEAM_SIZE - len(self.my_picked)


def pet_name(key: str) -> str:
    return SPECIES[key].name


def names(keys: Sequence[str], sep: str = "、") -> str:
    return sep.join(pet_name(key) for key in keys)


# ---------------------------------------------------------------- 失败与解析

class AdviceFailure(Exception):
    """一次建议请求失败（内容层面）。feedback 是下次重试时附给 LLM 的问题说明。"""

    def __init__(self, code: str, message: str, retryable: bool = True,
                 feedback: str | None = None, dropped: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.feedback = feedback
        self.dropped = list(dropped)


def parse_json(text: str) -> dict[str, Any]:
    """解析 LLM 输出；先做本地修复：去掉代码块标记，截取最外层大括号。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("找不到 json 对象")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("最外层不是对象")
    return data


def games_for(replies: float) -> int:
    """现场计算时每组对阵打几局：让每套阵容总共打 300 局左右，对手补法越少，单组对阵打得越多。

    这一步只是粗筛，局数不用多：筛出来的推荐和备选还会用新种子各复核 VERIFY_GAMES 局。
    """
    return min(96, max(8, math.ceil(300 / max(replies, 1))))
