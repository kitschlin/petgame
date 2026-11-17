"""战斗指导的共用部分：引擎评估全部合法动作，以及把动作、场上效果写成文字。

战斗建议本身由 Agent 完成（agentic/battle_agent.py）：引擎先把每个合法动作都推演一遍，结果连同每个动作的
直接后果一起交给 LLM；LLM 判断有没有明显领先的动作——有就跟推演，没有就按自己的判断原则选，并说明理由。
数字只来自引擎。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..engine.pets import SIDE_NAMES, SIDES
from .draft import OpponentProfile
from .policy import Move
from .rollout import (
    NEAR_MARGIN, OWN, ROLLOUT_GAMES, MoveValue, evaluate_all, fact_card, fact_text, near_best, paired_diff, rank,
)

MODEL_LABELS = {
    "random": "对手能放技能就放、目标随机",
    "smart": "对手接下来的一轮按推演来选",
    "manual": "对手大体按常规打法",
}
OWN_LABEL = "执行这一手后我方随机出招"  # 见 rollout.OWN


# ---------------------------------------------------------------- 引擎评估

@dataclass
class Evaluation:
    """引擎对一个决策点的评估。按 token 缓存：同一次行动重试时不重算。"""

    ranked: list[MoveValue]  # 全部合法动作，从好到差
    cards: dict[Move, dict[str, Any]]  # 每个动作的直接后果
    gaps: dict[Move, tuple[float, float]]  # 每个动作比最优少多少（价值），以及这个差距的标准误（逐局配对）
    games: int
    ms: int

    @property
    def best(self) -> MoveValue:
        return self.ranked[0]

    @property
    def flat(self) -> bool:
        """全部动作推演结果完全相同（例如都是 0% 或都是 100%）。"""
        return all(value.value == self.best.value for value in self.ranked)

    def top(self, move: Move) -> bool:
        """这个动作的推演价值是不是最高（与最优并列也算）。"""
        value = next((v for v in self.ranked if v.move == move), None)
        return value is not None and value.value >= self.best.value - 1e-9

    # 以下两个只给自对弈 agent（petarena/agent/env.py）的事后统计用，战斗 Agent 和面板不再使用
    @property
    def candidates(self) -> list[MoveValue]:
        return near_best(self.ranked)

    def tier(self, value: MoveValue) -> str:
        if value is self.best:
            return "最优"
        if value.move in {v.move for v in self.candidates}:
            return "与最优相当"
        return "略逊" if self.best.value - value.value <= NEAR_MARGIN + 1e-9 else "明显较差"


def evaluate(snap: dict[str, Any], profile: OpponentProfile, games: int = ROLLOUT_GAMES, workers: int = 0) -> Evaluation:
    """引擎评估全部合法动作（执行这一手后我方随机出招、对手按它的模型出招，见 rollout.py），
    并给每个动作算出直接后果和它比最优少多少。workers > 1 时每个动作交给一个进程，结果与单进程相同。"""
    start = time.monotonic()
    battle = snap["battle"]
    values = rank(evaluate_all(battle, own=OWN, other=profile.model, games=games, salt=snap["token"], workers=workers))
    best = values[0]
    gaps = {value.move: (0.0, 0.0) if value is best else paired_diff(best, value) for value in values}
    cards = {value.move: fact_card(battle, value.move) for value in values}
    return Evaluation(values, cards, gaps, games, int((time.monotonic() - start) * 1000))


# ---------------------------------------------------------------- 上下文

def _label(snap: dict[str, Any], pet_id: str) -> str:
    pet = snap["static"]["pets"][pet_id]
    return SIDE_NAMES[pet["side"]] + pet["name"]


def skill_name(snap: dict[str, Any], skill: str) -> str:
    return next(option["name"] for option in snap["options"] if option["skill"] == skill)


def describe(snap: dict[str, Any], move: Move) -> str:
    skill, target = move
    name = skill_name(snap, skill)
    if target is None:
        return name
    return f"{name} → {'自己' if target == snap['actor'] else _label(snap, target)}"


def _n(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _effects(snap: dict[str, Any]) -> list[str]:
    lines = []
    for side in SIDES:
        team = snap["view"]["teams"][side]
        foe = SIDE_NAMES["opp" if side == "me" else "me"]
        if team["barrier"]:
            barrier = team["barrier"]
            lines.append(
                f"{SIDE_NAMES[side]}的寒毒结界（剩余 {barrier['remaining']} 回合）：{foe}宠物每次行动前受到 "
                f"{_n(round(barrier['atk'] * 0.3, 2))} 点伤害，{barrier['hit']}% 概率被冻结")
        if team["chain"]:
            members = "、".join(_label(snap, pet_id) for pet_id in team["chain"]["members"])
            lines.append(f"{SIDE_NAMES[side]}的同心水链（剩余 {team['chain']['remaining']} 回合）：连接 {members}")
    return lines


def evidence_for(snap: dict[str, Any], evaluation: Evaluation, chosen: Move | None,
                 basis: str | None = None) -> dict[str, Any]:
    """面板上的推演结果：全部动作，从好到差。basis 是 LLM 这一步的依据（engine 跟推演 / judgment 自己判断）。"""
    model = MODEL_LABELS.get(snap["opponent"], MODEL_LABELS["manual"])
    return {
        "label": f"每个动作推演 {evaluation.games} 局（{OWN_LABEL}，{model}）",
        "games": evaluation.games,
        "basis": basis,
        "flat": evaluation.flat,
        "moves": [
            {
                "skill": value.move[0], "target": value.move[1], "text": describe(snap, value.move),
                "value": round(value.value, 3), "win_rate": round(value.win_rate, 3),
                "draw_rate": round(value.draw_rate, 3),
                "gap": round(evaluation.gaps[value.move][0], 3), "se": round(evaluation.gaps[value.move][1], 3),
                "facts": fact_text(evaluation.cards[value.move]),
                "top": evaluation.top(value.move),
                "chosen": value.move == chosen,
            }
            for value in evaluation.ranked
        ],
    }
