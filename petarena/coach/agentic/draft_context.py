"""选将 Agent 的上下文：Top10 阵容表和局面文字。都是程序固定算好、一次性放进上下文的，不做成工具。

Top10 从经验库来：book 是 ExperienceBook（对阵表 + 内置经验 + 玩家自己的对局，按「照战斗指导出招」修正），
对手补选按它的选将习惯预测（智能）或所有补法等概率（随机、手动）。算法沿用 v1 引擎决选里的穷举和复核：

1. 候选：我方全部合法阵容（已选的在前，其余任意顺序）。
2. 打分：预算够时全部打分；属性有成长、算不起穷举时，用基础属性的对阵表挑出最有希望的几套再现场推演。
3. 同一组成员只留最好的站位，取前 k 套。
4. 复核：前 VERIFY_TOP 套里样本不够的，用没参与挑选的新种子重估，再按复核后的数字排序（从很多候选里挑最高的，挑中的往往
   是运气好的）。LLM 看到的数字和面板上显示的是同一份。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from ...engine.draft import PICK_ORDER
from ...engine.pets import SIDE_NAMES, SPECIES, SPECIES_ORDER, TEAM_SIZE, battle_speed
from ..draft import (
    GAMES_BUDGET_PER_WORKER, PREDICTED, SHORTLIST, VERIFY_BELOW, VERIFY_GAMES, OpponentProfile, Situation,
    games_for, names, pet_name, reply_model_for,
)
from ..knowledge import KnowledgeBase
from ..matchup import MatchupBook, PlanScore, evaluate_plans, my_completions, robust_score

TOP_K = 10
VERIFY_TOP = 5  # 只复核前几套：后面的通常比第一名低一大截，复核它们只是白花时间（每套 600 局）


@dataclass
class Row:
    """Top10 表的一行。"""

    id: str
    score: PlanScore
    pick: str  # 本手要选的宠物
    likely: tuple[str, ...] | None  # 对手最可能的补选（只有按习惯预测时才有）
    likely_chance: float
    combo: bool  # 组合经验库里有这组成员
    verified: bool  # 数字是否来自新种子复核
    basis: dict[str, Any]  # 经验依据（ExperienceBook.explain）

    @property
    def lineup(self) -> tuple[str, ...]:
        return self.score.lineup

    def to_dict(self) -> dict[str, Any]:
        score = self.score
        return {
            "id": self.id, "lineup": list(score.lineup), "pick": self.pick,
            "win_rate": round(score.win_rate, 3), "robust": round(score.robust, 3),
            "worst_win_rate": round(score.worst_win_rate, 3), "worst_reply": list(score.worst_reply),
            "likely_reply": list(self.likely) if self.likely else None, "likely_chance": round(self.likely_chance, 3),
            "replies": score.replies, "samples": score.samples, "verified": self.verified, "combo": self.combo,
            "basis": self.basis,
        }


def recheck(book: MatchupBook, model: Any, old: PlanScore, opp_picked: Sequence[str], weight: float) -> PlanScore:
    """用没参与挑选的新种子把这套阵容重打一遍（平摊到对手各种补法上）。"""
    support = [(reply, prob) for reply, prob in model(old.lineup).items() if prob > 0]
    fresh = book.verify([(old.lineup, reply) for reply, _ in support], max(16, math.ceil(VERIFY_GAMES / len(support))))
    values = [result.value for result in fresh]
    at = values.index(min(values))
    mean = sum(prob * value for (_, prob), value in zip(support, values))
    return PlanScore(
        lineup=old.lineup, worst=values[at], worst_reply=support[at][0][len(opp_picked):], mean=mean,
        robust=robust_score(values[at], mean, weight),
        win_rate=sum(prob * result.win_rate for (_, prob), result in zip(support, fresh)),
        worst_win_rate=fresh[at].win_rate, replies=len(support), samples=sum(result.games for result in fresh),
    )


def top_lineups(situation: Situation, book: MatchupBook, profile: OpponentProfile, kb: KnowledgeBase,
                prior: MatchupBook | None = None, k: int = TOP_K) -> tuple[list[Row], bool]:
    """返回（Top k 行，是否穷举）。book 一般是 ExperienceBook。"""
    fixed = len(situation.my_picked)
    opp = situation.opp_picked
    model = reply_model_for(profile, opp)
    weight = profile.worst_weight
    budget = GAMES_BUDGET_PER_WORKER * max(1, book.workers)

    def pairs(lineups: Sequence[tuple[str, ...]]) -> int:
        return sum(len(model(lineup)) for lineup in lineups)

    legal = my_completions(situation.my_picked)
    games = games_for(pairs(legal) / len(legal))
    exhaustive = book.cost(pairs(legal), games) <= budget
    if exhaustive:
        wanted = list(legal)
    else:
        if prior is not None and prior.complete:
            guide = {score.lineup: score.robust for score in evaluate_plans(prior, legal, opp, weight, None, model)}
            ordered = sorted(legal, key=lambda lineup: -guide[lineup])
        else:
            ordered = list(legal)
        wanted, groups = [], set()
        for lineup in ordered:  # 每组成员先取向导认为最好的站位，凑够 SHORTLIST 组为止
            members = frozenset(lineup)
            if members in groups:
                continue
            if len(groups) >= max(SHORTLIST, k) or book.cost(pairs(wanted + [lineup]), games) > budget:
                break
            groups.add(members)
            wanted.append(lineup)
    scores = sorted(evaluate_plans(book, wanted, opp, weight, games, model), key=lambda score: (-score.robust, score.lineup))

    best: list[PlanScore] = []
    seen: set[frozenset[str]] = set()
    for score in scores:
        if frozenset(score.lineup) not in seen:
            seen.add(frozenset(score.lineup))
            best.append(score)
        if len(best) >= k:
            break

    checked = []
    for index, score in enumerate(best):
        verified = index < VERIFY_TOP and score.samples < VERIFY_BELOW
        checked.append((recheck(book, model, score, opp, weight) if verified else score, verified))
    # 复核过的前几套按复核后的数字重排；后面的本来就差得远，不复核，排在它们之后
    checked = sorted(checked[:VERIFY_TOP], key=lambda item: (-item[0].robust, item[0].lineup)) + checked[VERIFY_TOP:]

    combos = {frozenset(combo["members"]) for combo in kb.combos}
    rows = []
    for index, (score, verified) in enumerate(checked, start=1):
        replies = model(score.lineup)
        likely, chance = None, 0.0
        if profile.replies == PREDICTED and len(opp) < TEAM_SIZE and replies:
            reply, chance = max(replies.items(), key=lambda item: (item[1], item[0]))
            likely = reply[len(opp):]
        explain = getattr(book, "explain", None)
        basis = explain(score.lineup, replies) if explain else {}
        rows.append(Row(f"T{index}", score, score.lineup[fixed], likely, chance,
                        frozenset(score.lineup) in combos, verified, basis))
    return rows, exhaustive


# ---------------------------------------------------------------- 文字

def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def basis_text(basis: dict[str, Any]) -> str:
    parts = []
    if basis.get("seed_games"):
        parts.append(f"内置经验推演 {basis['seed_games']} 局")
    own = (basis.get("guided_games") or 0) - (basis.get("seed_games") or 0)
    if own > 0:
        parts.append(f"你的对局后推演 {own} 局")
    real = basis.get("real") or [0, 0, 0]
    if sum(real):
        parts.append(f"实战 {real[0]} 胜 {real[1]} 平 {real[2]} 负")
    return "，".join(parts) or "只有对阵表（按常规打法）"


def table_text(rows: Sequence[Row], profile: OpponentProfile) -> str:
    lines = ["编号｜阵容（按站位）｜本手选｜胜率｜最坏补选：胜率｜对手最可能补选｜依据｜有组合经验"]
    for row in rows:
        score = row.score
        worst = f"{names(score.worst_reply) or '—'}：{_pct(score.worst_win_rate)}" if score.worst_reply else "—"
        likely = f"{names(row.likely)}（{_pct(row.likely_chance)}）" if row.likely else "—"
        lines.append(
            f"{row.id}｜{' → '.join(f'{key} {pet_name(key)}' for key in score.lineup)}｜{row.pick}｜{_pct(score.win_rate)}"
            f"｜{worst}｜{likely}｜{basis_text(row.basis)}｜{'✓' if row.combo else ''}")
    lines.append(f"（口径：{profile.label}；经验库修正为「照战斗指导出招」）")
    return "\n".join(lines)


def _stats(side: str, key: str, roster: dict) -> str:
    stats = roster[side][key]
    hit = f"，命中 {stats['hit']}%" if stats["hit"] else ""
    speed = battle_speed(key, stats["speed"])
    shown = f"{stats['speed']}（开战 {speed}）" if speed != stats["speed"] else str(stats["speed"])
    return f"生命 {stats['max_hp']}，攻击 {stats['atk']}，速度 {shown}{hit}"


def _slots(keys: Sequence[str]) -> str:
    if not keys:
        return "（还没有）"
    return "，".join(f"{index} 号位 {key} {pet_name(key)}" for index, key in enumerate(keys, start=1))


def situation_text(situation: Situation, profile: OpponentProfile, kb: KnowledgeBase,
                   record: Sequence[dict[str, Any]] = ()) -> str:
    step = situation.step
    later = "、".join(
        f"第 {index + 1} 手{SIDE_NAMES[PICK_ORDER[index]]}" for index in range(step + 1, len(PICK_ORDER)))
    lines = [
        "【选将局面】",
        f"当前：第 {situation.hand} 手（我方）。" + (f"之后：{later}。" if later else "这是最后一手。"),
        f"对手方操作方式：{profile.brief}",
        f"对手已选：{_slots(situation.opp_picked)}",
        f"我方已选：{_slots(situation.my_picked)}",
        f"我方还可以选：{'、'.join(f'{key} {pet_name(key)}' for key in situation.available)}",
        "",
        "【宠物】（技能和被动双方相同；数值和特点双方不同）",
    ]
    for key in SPECIES_ORDER:
        pet = kb.pets[key]
        lines.append(f"- {key} {pet['name']}（{SPECIES[key].role}）：{pet['skill']}；被动 {pet['passive']}。"
                     f"用法：{pet['how_to_use']}")
        lines.append(f"  我方：{_stats('me', key, situation.roster)}。{pet['sides']['me']}")
        lines.append(f"  对手方：{_stats('opp', key, situation.roster)}。{pet['sides']['opp']}")
    if record:
        lines += ["", "【我方各组成员对这类对手的实战战绩】"]
        lines += [f"- {names(row['members'])}：{row['win']} 胜 {row['draw']} 平 {row['loss']} 负" for row in record]
    return "\n".join(lines)
