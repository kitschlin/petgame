"""战斗指导：引擎评估全部合法动作 → LLM 在候选里选择并讲解 → 校验。

分工和选将阶段一样：数字只来自引擎。引擎把当前宠物的每个合法动作各推演若干局，
挑出结果最好、彼此分不出高下的几个作为候选，并算出每个候选的直接后果；LLM 在候选
里选一个，讲清楚为什么。LLM 选了候选之外的动作、不合法的目标，都算这次请求失败，
失败原因会附在下一次重试的上下文里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..engine.pets import SIDE_NAMES, SIDES
from .draft import AdviceFailure, OpponentProfile, parse_json
from .llm import LLM
from .policy import Move
from .rollout import NEAR_MARGIN, OWN, ROLLOUT_GAMES, MoveValue, evaluate_all, fact_card, fact_text, near_best, rank

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "battle_system.md").read_text(encoding="utf-8").strip()

REASON_LIMIT = 100
RISK_LIMIT = 60
INVALID = "invalid"

MODEL_LABELS = {
    "random": "对手能放技能就放、目标随机",
    "smart": "对手接下来的一轮也按推演来选",
    "manual": "对手大体按常规打法",
}
OWN_LABEL = "我方接下来的一轮按推演最好的动作算，之后按常规打法"  # 见 rollout.OWN


# ---------------------------------------------------------------- 引擎评估

@dataclass
class Evaluation:
    """引擎对一个决策点的评估。按 token 缓存：同一次行动重试时不重算。"""

    ranked: list[MoveValue]  # 全部合法动作，从好到差
    candidates: list[MoveValue]  # 候选：与最优分不出高下的前几个
    cards: dict[Move, dict[str, Any]]  # 候选动作的直接后果
    games: int
    ms: int

    @property
    def best(self) -> MoveValue:
        return self.ranked[0]

    def tier(self, value: MoveValue) -> str:
        gap = self.best.value - value.value
        if value is self.best:
            return "最优"
        if value.move in self.cards:
            return "与最优相当"  # 候选：推演分不出高下
        if gap <= NEAR_MARGIN + 1e-9:
            return "略逊"
        return "明显较差"


def evaluate(snap: dict[str, Any], profile: OpponentProfile, games: int = ROLLOUT_GAMES, workers: int = 0) -> Evaluation:
    """引擎评估全部合法动作。往前看一整轮（我方、对手接下来的几次决策都按推演来选，见 rollout.py）；
    workers > 1 时每个动作交给一个进程，结果与单进程相同。"""
    start = time.monotonic()
    battle = snap["battle"]
    values = evaluate_all(battle, own=OWN, other=profile.model, games=games, salt=snap["token"], workers=workers)
    candidates = near_best(values)
    cards = {value.move: fact_card(battle, value.move) for value in candidates}
    return Evaluation(rank(values), candidates, cards, games, int((time.monotonic() - start) * 1000))


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


def build_user_message(
    snap: dict[str, Any], evaluation: Evaluation, profile: OpponentProfile,
    feedback: str | None = None, attempt: int = 1,
) -> str:
    view, static = snap["view"], snap["static"]
    queue = " → ".join(_label(snap, pet_id) for pet_id in view["upcoming"]) or "（本回合没有其他宠物要行动了）"
    done = "、".join(_label(snap, pet_id) for pet_id in view["done"]) or "无"
    lines = [
        "【战况】",
        f"第 {view['round']} / {static['max_rounds']} 回合。"
        f"能量豆：我方 {view['teams']['me']['beans']}，对手方 {view['teams']['opp']['beans']}（上限 {static['energy_cap']}）。",
        f"本回合已行动：{done}。当前行动：{_label(snap, snap['actor'])}。之后依次：{queue}",
    ]
    lines += [f"场上效果：{line}" for line in _effects(snap)] or ["场上效果：无"]
    lines.append(f"对手方操作方式：{profile.brief}")

    lines += ["", "【宠物状态】（id｜名字｜站位｜生命/上限｜护盾｜攻击｜速度）"]
    for side in ("me", "opp"):
        for pet in static["pets"].values():
            if pet["side"] != side:
                continue
            state = view["pets"][pet["id"]]
            if not state["alive"]:
                lines.append(f"{pet['id']}｜{SIDE_NAMES[side]}{pet['name']}｜{pet['slot']} 号位｜已倒下")
                continue
            extra = "｜水链连接中" if state["linked"] else ""
            lines.append(
                f"{pet['id']}｜{SIDE_NAMES[side]}{pet['name']}｜{pet['slot']} 号位｜{_n(state['hp'])}/{_n(pet['max_hp'])}"
                f"｜护盾 {_n(state['shield'])}｜攻 {pet['atk']}｜速 {pet['speed']}{extra}")

    lines += ["", f"【当前行动】{snap['actor']} {_label(snap, snap['actor'])}"]
    for option in snap["options"]:
        cost = f"，消耗 {option['cost']} 豆" if option["cost"] else ""
        if not option["enabled"]:
            lines.append(f"- {option['skill']}：{option['name']}（不可用：{option['reason']}）")
        elif option["targets"] is None:
            lines.append(f"- {option['skill']}：{option['name']}{cost}，无需选择目标")
        else:
            lines.append(f"- {option['skill']}：{option['name']}{cost}，可选目标 {', '.join(option['targets'])}")

    lines += ["", "【候选动作】（引擎推演后认可的动作，从好到差；只能从这里选）"]
    for index, value in enumerate(evaluation.candidates, start=1):
        skill, target = value.move
        lines.append(
            f'{index}. skill="{skill}" target={"null" if target is None else chr(34) + target + chr(34)}'
            f"｜{describe(snap, value.move)}｜档位：{evaluation.tier(value)}"
            f"｜直接后果：{fact_text(evaluation.cards[value.move])}｜之后我方能量豆 {evaluation.cards[value.move]['beans']}")
    rest = [value for value in evaluation.ranked if value not in evaluation.candidates]
    if rest:
        lines += ["", "【其余合法动作】（引擎推演后判断更差，不要选）"]
        lines += [f"- {describe(snap, value.move)}（{evaluation.tier(value)}）" for value in rest[:5]]

    if feedback:
        lines += ["", "【上次输出的问题】", feedback]
    return "\n".join(lines)


def hint_for(snap: dict[str, Any], evaluation: Evaluation) -> dict[str, Any]:
    """同一局势的结构化副本，只有假模型使用。"""
    return {
        "kind": "battle", "token": snap["token"],
        "candidates": [
            {"skill": value.move[0], "target": value.move[1], "text": describe(snap, value.move),
             "facts": fact_text(evaluation.cards[value.move])}
            for value in evaluation.candidates
        ],
    }


# ---------------------------------------------------------------- 解析与校验

def feedback_text(snap: dict[str, Any], evaluation: Evaluation, problems: list[str]) -> str:
    lines = ["你上一次的输出存在以下问题："]
    lines += [f"- {problem}" for problem in problems]
    lines.append("只能从候选动作里选一个，原样照抄 skill 和 target：")
    for value in evaluation.candidates:
        skill, target = value.move
        lines.append(f'- {{"skill": "{skill}", "target": {"null" if target is None else chr(34) + target + chr(34)}}}'
                     f"（{describe(snap, value.move)}）")
    lines.append("请重新输出完整 json。")
    return "\n".join(lines)


def read_choice(text: str, finish_reason: str, snap: dict[str, Any], evaluation: Evaluation) -> dict[str, Any]:
    """LLM 原始输出 → {skill, target, reason, risk}。不合格时抛 AdviceFailure。"""

    def fail(message: str, problem: str) -> AdviceFailure:
        return AdviceFailure(INVALID, message, feedback=feedback_text(snap, evaluation, [problem]))

    try:
        data = parse_json(text)
    except ValueError:
        raise fail("AI 输出无法解析",
                   "输出不是完整的 json" + ("（被截断了，请缩短理由）" if finish_reason == "length" else "")) from None

    skill, target = data.get("skill"), data.get("target")
    option = next((o for o in snap["options"] if o["skill"] == skill), None)
    if option is None:
        raise fail("AI 建议了不存在的动作", f'skill 必须是 "basic" 或 "special"，你写的是 {skill!r}')
    if not option["enabled"]:
        raise fail("AI 建议了不可用的技能", f"【{option['name']}】现在不可用：{option['reason']}")
    if option["targets"] is None:
        target = None  # 无需选择目标的技能不给目标
    elif target not in option["targets"]:
        raise fail("AI 建议的目标不合法",
                   f"【{option['name']}】的目标 {target!r} 不合法（已倒下或不可选），可选：{', '.join(option['targets'])}")
    if (skill, target) not in evaluation.cards:
        raise fail("AI 建议的动作不在引擎认可的候选里",
                   f"{describe(snap, (skill, target))} 在引擎推演里明显更差，不在候选动作中")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise fail("AI 没有给出理由", "缺少 reason")
    risk = data.get("risk")
    return {
        "skill": skill, "target": target, "reason": reason.strip()[:REASON_LIMIT],
        "risk": risk.strip()[:RISK_LIMIT] if isinstance(risk, str) else "",
    }


# ---------------------------------------------------------------- 编排

@dataclass
class BattleResult:
    advice: dict[str, Any]
    evidence: dict[str, Any]
    trace: dict[str, Any]


def evidence_for(snap: dict[str, Any], evaluation: Evaluation, chosen: Move | None) -> dict[str, Any]:
    model = MODEL_LABELS.get(snap["opponent"], MODEL_LABELS["manual"])
    return {
        "label": f"推演估计（每个动作 {evaluation.games} 局；{OWN_LABEL}，{model}）",
        "games": evaluation.games,
        "moves": [
            {
                "skill": value.move[0], "target": value.move[1], "text": describe(snap, value.move),
                "value": round(value.value, 3), "win_rate": round(value.win_rate, 3),
                "tier": evaluation.tier(value), "candidate": value.move in evaluation.cards,
                "chosen": value.move == chosen,
            }
            for value in evaluation.ranked
        ],
    }


class BattleAdvisor:
    """一次行动建议的 LLM 部分。LLMError / AdviceFailure 由调用方处理。"""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def advise(self, snap: dict[str, Any], evaluation: Evaluation, profile: OpponentProfile,
               attempt: int = 1, feedback: str | None = None) -> BattleResult:
        user = build_user_message(snap, evaluation, profile, feedback, attempt)
        reply = self.llm.complete(SYSTEM_PROMPT, user, hint_for(snap, evaluation))
        choice = read_choice(reply.text, reply.finish_reason, snap, evaluation)
        move = (choice["skill"], choice["target"])
        order = [value.move for value in evaluation.candidates]
        advice = {
            "actor": snap["actor"], "skill": choice["skill"], "target": choice["target"],
            "name": skill_name(snap, choice["skill"]), "text": describe(snap, move),
            "reason": choice["reason"], "risk": choice["risk"],
            "facts": fact_text(evaluation.cards[move]),
        }
        trace = {
            "attempt": attempt, "moves": len(evaluation.ranked), "candidates": len(order),
            "chosen_rank": order.index(move) + 1, "llm_ms": reply.ms, "engine_ms": evaluation.ms,
            "usage": reply.usage,
        }
        return BattleResult(advice, evidence_for(snap, evaluation, move), trace)
