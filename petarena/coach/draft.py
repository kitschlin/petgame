"""选将建议：组装上下文 → LLM 提方案 → 校验 → 引擎决选。

分工：LLM 根据经验库和局势提出若干套完整阵容并说明理由；引擎逐套推演，
选出最好的一套中「本手要选的宠物」。面板上的所有数字都来自引擎。
"""

from __future__ import annotations

import itertools
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..engine.draft import PICK_ORDER
from ..engine.pets import SIDE_NAMES, SPECIES, SPECIES_ORDER, TEAM_SIZE, battle_speed
from .knowledge import KnowledgeBase
from .llm import LLM
from . import opponent as smart_opponent
from .matchup import MatchupBook, PlanScore, evaluate_plans, my_completions, robust_score, uniform_replies
from .policy import EXPERT, HEURISTIC, RANDOM_MODE

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompts" / "draft_system.md").read_text(encoding="utf-8").strip()

MAX_PLANS = 8
REASON_LIMIT = 120
STYLE_LIMIT = 16
ENGINE_MARGIN = 0.05  # 引擎找到的最优比 LLM 全部方案高出这么多，才用引擎补充的方案
GAMES_BUDGET_PER_WORKER = 5000  # 现场计算时一次建议最多打多少局（每个进程）
SHORTLIST = 12  # 属性有成长、算不起穷举时，用基础属性的对阵表挑出这么多套最有希望的阵容来现场推演
TIE_MARGIN = 0.01  # 评分相差不到这么多视为并列（已经小于推演本身的误差），并列时听 LLM 的排序
VERIFY_BELOW = 2000  # 选中方案的估计不足这么多局时，用新种子复核后再展示
VERIFY_GAMES = 600  # 复核时每套阵容一共打多少局（平摊到对手的各种补法上）
COMBO_LIMITS = {0: 8, 1: 6, 2: 4}  # 我方已选 0 / 1 / 2 只时检索多少条组合经验

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


# ---------------------------------------------------------------- 上下文

def _stats_text(side: str, key: str, roster: dict) -> str:
    stats = roster[side][key]
    return (f"{stats['max_hp']}/{stats['atk']}/{stats['speed']}"
            f"({battle_speed(key, stats['speed'])})/{stats['hit']}%")


def _slots(keys: Sequence[str]) -> str:
    if not keys:
        return "（还没有）"
    return "，".join(f"{index}号位 {key} {pet_name(key)}" for index, key in enumerate(keys, start=1))


def _combinations(situation: Situation) -> int:
    return len(list(itertools.combinations(situation.available, situation.need)))


def legal_rule(situation: Situation) -> str:
    picked = list(situation.my_picked)
    pool = ", ".join(situation.available)
    if not picked:
        return f"合法要求：每套 3 只都从 [{pool}] 中选择，不重复。"
    head = "、".join(f'"{key}"' for key in picked)
    return f"合法要求：每套以 {head} 开头，另外 {situation.need} 只从 [{pool}] 中选择，不重复。"


def legal_enumeration(situation: Situation) -> str | None:
    """完整的合法阵容枚举（不计顺序）。第 2 手有 56 套，太长，只给规则。"""
    if not situation.my_picked:
        return None
    picked = list(situation.my_picked)
    rows = [json.dumps(picked + list(rest)) for rest in itertools.combinations(situation.available, situation.need)]
    return f"全部 {len(rows)} 套合法阵容：\n" + "\n".join(rows)


def experience_lines(experience: dict[str, Any] | None) -> list[str]:
    """经验库里与当前局势有关的内容：复盘沉淀的经验条目、对这类对手的实战战绩。"""
    if not experience:
        return []
    lines: list[str] = []
    proven = experience.get("proven") or []
    if proven:
        lines += ["", "【推演经验】（玩家照战斗指导出招、对手按它真实的方式出招和补选时，整局推演里胜算最高的阵容；"
                      "这是最可靠的依据，与下面其他经验冲突时以它为准）"]
        lines += [f"- {json.dumps(item['lineup'])}（{names(item['lineup'])}，按这个站位）：约 {round(item['win_rate'] * 100)}%"
                  f"（推演 {item['games']} 局）" for item in proven]
        lines.append("请把这几套原样列入方案（各算一套，lineup 的顺序不要改），并结合宠物的技能机制写清它们为什么有效；"
                     "其余方案换不同的成员组合。")
    lessons = experience.get("lessons") or []
    if lessons:
        lines += ["", "【实战经验】（来自之前真实对局的复盘；印证次数越多越可靠，与下面的组合经验冲突时以它为准）"]
        for lesson in lessons:
            about = f"涉及 {names(lesson['pets'])}"
            if lesson["against"]:
                about += f"，针对对手的 {names(lesson['against'])}"
            lines.append(f"- {'宜' if lesson['polarity'] == 'do' else '忌'}｜{about}｜{lesson['text']}（印证 {lesson['support']} 次）")
    record = experience.get("record") or []
    if record:
        lines += ["", "【实战战绩】（我方这几组成员对这类对手的真实结果）"]
        lines += [f"- {names(row['members'])}：{row['win']} 胜 {row['draw']} 平 {row['loss']} 负" for row in record]
    return lines


def build_user_message(
    situation: Situation, kb: KnowledgeBase, profile: OpponentProfile,
    feedback: str | None = None, attempt: int = 1, experience: dict[str, Any] | None = None,
) -> str:
    step = situation.step
    later = "、".join(
        f"第 {index + 1} 手{'对手' if PICK_ORDER[index] == 'opp' else '我方'}"
        for index in range(step + 1, len(PICK_ORDER))
    )
    mine, opp = list(situation.my_picked), list(situation.opp_picked)
    opp_pool = [key for key in (profile.pool or SPECIES_ORDER) if key not in opp]
    head = json.dumps(mine + ["…"], ensure_ascii=False)
    lines = [
        "【选将局势】",
        f"当前：第 {situation.hand} 手（我方）。" + (f"之后：{later}。" if later else "这是最后一手。"),
        f"对手方操作方式：{profile.brief}",
        f"对手已选：{_slots(opp)}",
        f"我方已选：{_slots(mine)}",
        (f"我方还要再选 {situation.need} 只，所以每套 lineup 必须以 {head} 开头；"
         f"不计顺序一共只有 {_combinations(situation)} 种不同的阵容，方案之间的成员不能完全相同。" if mine
         else "我方还要选 3 只，lineup 可以任意组合；方案之间的成员不能完全相同（只换顺序算同一套）。"),
        (f"对手还可能选：{', '.join(opp_pool)}" if len(opp) < TEAM_SIZE
         else "对手的 3 只已经全部选完，阵容不会再变：predicted_opp 直接填这 3 只，opp_read 写它的打法。"),
        "",
        "【我方可选宠物】（key｜名字｜定位｜生命/攻击/速度(开战)/命中）",
    ]
    for key in situation.available:
        lines.append(f"{key}｜{pet_name(key)}｜{SPECIES[key].role}｜{_stats_text('me', key, situation.roster)}")

    lines += ["", "【对手已选宠物详情】"]
    for pet in kb.search_pet_experience(opp, "opp"):
        lines.append(
            f"- {pet['key']} {pet['name']}（对手方 {_stats_text('opp', pet['key'], situation.roster)}）："
            f"{pet['side_note']}。技能：{pet['skill']}。被动：{pet['passive']}。应对：{pet['counter_notes']}"
        )

    if mine:
        lines += ["", "【我方已选宠物经验】"]
        for pet in kb.search_pet_experience(mine, "me"):
            lines.append(
                f"- {pet['key']} {pet['name']}（我方 {_stats_text('me', pet['key'], situation.roster)}）："
                f"{pet['side_note']}。用法：{pet['how_to_use']}。适合搭配：{', '.join(pet['synergy'])}"
            )

    lines += ["", "【我方可选宠物要点】"]
    for pet in kb.search_pet_experience(situation.available, "me"):
        lines.append(
            f"- {pet['key']} {pet['name']}：{pet['skill']}；{pet['side_note']}；"
            f"适合搭配 {', '.join(pet['synergy'])}；对手有 {', '.join(pet['weak_vs'])} 时吃亏"
        )

    combos = kb.search_combo_experience(
        include=mine, against=opp, limit=COMBO_LIMITS.get(len(mine), 4), opp_policy=profile.opp_policy,
    )
    lines += ["", "【相关组合经验】（经验库只收录了部分组合；档位来自引擎对这类对手的推演：强 > 中上 > 中 > 弱）"]
    for combo in combos:
        lines.append(
            f"- {json.dumps(combo['members'])}（{combo['style']}｜档位：{combo['tier']}）"
            f"配合：{combo['synergy']}。打法：{combo['plan']}。"
            f"对手有 {', '.join(combo['beats'])} 时有利，有 {', '.join(combo['fears'])} 时不利。备注：{combo['notes']}"
        )
    if not combos:
        lines.append("（没有包含我方已选宠物的组合经验，请基于技能机制提出方案）")
    lines += experience_lines(experience)

    if feedback:
        lines += ["", "【上次输出的问题】", feedback]
    if attempt >= 3:
        lines += ["", legal_enumeration(situation) or legal_rule(situation)]
    return "\n".join(lines)


# ---------------------------------------------------------------- 解析与校验

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


@dataclass(frozen=True)
class Plan:
    lineup: tuple[str, ...]
    reason: str
    style: str
    predicted_opp: tuple[str, ...] | None
    rank: int | None  # 在 LLM 输出里是第几套（从 1 开始）；引擎补充的方案为 None
    source: str = "llm"


def _clean_predicted(value: Any, opp_picked: Sequence[str]) -> tuple[str, ...] | None:
    """predicted_opp 不合法时只忽略预测，不丢弃方案。合法时把已选宠物按站位排在前面。"""
    if not isinstance(value, list) or len(value) != TEAM_SIZE:
        return None
    if any(not isinstance(key, str) or key not in SPECIES for key in value) or len(set(value)) != TEAM_SIZE:
        return None
    if any(key not in value for key in opp_picked):
        return None
    return tuple(opp_picked) + tuple(key for key in value if key not in opp_picked)


def validate_plans(raw: list[Any], situation: Situation) -> tuple[list[Plan], list[str]]:
    """逐套校验。返回（合法方案，被丢弃方案的原因）。"""
    picked = list(situation.my_picked)
    plans: list[Plan] = []
    dropped: list[str] = []
    seen: dict[frozenset[str], int] = {}
    for index, item in enumerate(raw, start=1):
        lineup = item.get("lineup") if isinstance(item, dict) else None
        label = f"第 {index} 套 {json.dumps(lineup, ensure_ascii=False)}"
        if len(plans) >= MAX_PLANS:
            dropped.append(f"{label}：超过 {MAX_PLANS} 套，已忽略")
            continue
        if not isinstance(lineup, list) or len(lineup) != TEAM_SIZE or not all(isinstance(k, str) for k in lineup):
            dropped.append(f"{label}：lineup 必须是 {TEAM_SIZE} 只宠物的 key 列表")
            continue
        unknown = [key for key in lineup if key not in SPECIES]
        if unknown:
            dropped.append(f"{label}：{unknown[0]} 不是合法宠物")
            continue
        repeated = [key for key in lineup if lineup.count(key) > 1]
        if repeated:
            dropped.append(f"{label}：{repeated[0]} 重复")
            continue
        if lineup[:len(picked)] != picked:
            dropped.append(f"{label}：必须以我方已选的 {json.dumps(picked)} 开头")
            continue
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            dropped.append(f"{label}：缺少 reason")
            continue
        members = frozenset(lineup)
        if members in seen:
            dropped.append(f"{label}：与第 {seen[members]} 套的成员相同")
            continue
        seen[members] = index
        style = item.get("style")
        plans.append(Plan(
            lineup=tuple(lineup),
            reason=reason.strip()[:REASON_LIMIT],
            style=style.strip()[:STYLE_LIMIT] if isinstance(style, str) else "",
            predicted_opp=_clean_predicted(item.get("predicted_opp"), situation.opp_picked),
            rank=index,
        ))
    return plans, dropped


def feedback_text(situation: Situation, problems: Sequence[str]) -> str:
    lines = ["你上一次的输出存在以下问题："]
    lines += [f"- {problem}" for problem in problems]
    lines += [legal_rule(situation), "请重新输出完整 json。"]
    return "\n".join(lines)


def read_plans(text: str, finish_reason: str, situation: Situation) -> tuple[dict[str, Any], list[Plan], list[str]]:
    """LLM 原始输出 → (整个对象, 合法方案, 丢弃原因)。没有任何合法方案时抛 AdviceFailure。"""
    try:
        data = parse_json(text)
    except ValueError:
        problem = "输出不是完整的 json" + ("（被截断了，请减少方案数或缩短理由）" if finish_reason == "length" else "")
        raise AdviceFailure(INVALID, "AI 输出无法解析", feedback=feedback_text(situation, [problem])) from None
    raw = data.get("plans")
    if not isinstance(raw, list) or not raw:
        problem = '缺少 "plans"，或者它不是非空列表'
        raise AdviceFailure(INVALID, "AI 输出缺少方案", feedback=feedback_text(situation, [problem]))
    plans, dropped = validate_plans(raw, situation)
    if not plans:
        raise AdviceFailure(INVALID, "AI 输出不合法", feedback=feedback_text(situation, dropped), dropped=dropped)
    return data, plans, dropped


# ---------------------------------------------------------------- 引擎决选

def games_for(replies: float) -> int:
    """现场计算时每组对阵打几局：让每套阵容总共打 300 局左右，对手补法越少，单组对阵打得越多。

    这一步只是粗筛，局数不用多：筛出来的推荐和备选还会用新种子各复核 VERIFY_GAMES 局。
    """
    return min(96, max(8, math.ceil(300 / max(replies, 1))))


def _orders(plan: Plan, fixed: int) -> list[tuple[str, ...]]:
    """同一组宠物的所有站位：已选的不动，其余任意排列；LLM 给的顺序排在最前。"""
    head, rest = plan.lineup[:fixed], plan.lineup[fixed:]
    orders = [head + extra for extra in itertools.permutations(rest)]
    orders.remove(plan.lineup)
    return [plan.lineup] + orders


def _engine_plan(lineup: tuple[str, ...], fixed: int, kb: KnowledgeBase) -> Plan:
    members = set(lineup)
    combo = next((c for c in kb.combos if set(c["members"]) == members), None)
    reason = f"引擎补充：穷举推演中，{names(lineup)}这套阵容对上对手各种可能的补选综合表现最好。"
    if combo is not None:
        reason += combo["synergy"]
    else:
        reason += kb.pets[lineup[fixed]]["how_to_use"]
    return Plan(lineup, reason[:REASON_LIMIT], combo["style"][:STYLE_LIMIT] if combo else "引擎推演最优",
                None, None, source="engine")


def strategy_text(style: str, lineup: Sequence[str], fixed: int) -> str:
    """整体思路 = 打法 + 之后几手的计划。我方第 i 只（从 0 数）在第 2(i+1) 手选。"""
    style = style or "综合推演最优"
    if fixed >= TEAM_SIZE - 1:
        return style
    later = "，".join(
        f"第 {2 * (index + 1)} 手计划补{pet_name(lineup[index])}" for index in range(fixed + 1, TEAM_SIZE)
    )
    return f"{style}：本手{pet_name(lineup[fixed])}，{later}"


@dataclass
class Decision:
    advice: dict[str, Any]
    evidence: dict[str, Any]
    chosen_llm_rank: int | None
    engine_better: bool
    reordered: bool


def decide(
    situation: Situation, plans: Sequence[Plan], book: MatchupBook, profile: OpponentProfile,
    kb: KnowledgeBase, opp_read: str = "", prior: MatchupBook | None = None,
) -> Decision:
    """引擎决选。prior 是基础属性的完整对阵表（可选）：属性有成长、算不起穷举时用它做向导。"""
    fixed = len(situation.my_picked)
    opp = situation.opp_picked
    model = reply_model_for(profile, opp)
    budget = GAMES_BUDGET_PER_WORKER * max(1, book.workers)
    weight = profile.worst_weight

    def pairs(lineups: Sequence[tuple[str, ...]]) -> int:
        return sum(len(model(lineup)) for lineup in lineups)

    legal = my_completions(situation.my_picked)
    games = games_for(pairs(legal) / len(legal))  # 平均每套阵容要对上几种对手补法

    def affordable(lineups: Sequence[tuple[str, ...]]) -> bool:
        return book.cost(pairs(lineups), games) <= budget

    all_orders = {plan: _orders(plan, fixed) for plan in plans}
    exhaustive = affordable(legal)
    if exhaustive:
        wanted = list(legal)
    else:
        wanted = [plan.lineup for plan in plans]  # 至少评估 LLM 给出的站位
        if prior is not None and prior.complete:
            # 基础属性下的排名是很好的向导：每套方案取它认为最好的站位，再加上它排名最靠前的几套阵容
            guide = {score.lineup: score.robust for score in evaluate_plans(prior, legal, opp, weight, None, model)}
            extra = [max(all_orders[plan], key=guide.__getitem__) for plan in plans]
            extra += sorted(guide, key=guide.__getitem__, reverse=True)[:SHORTLIST]
        else:
            extra = [order for plan in plans for order in all_orders[plan]]
        for lineup in extra:
            if lineup not in wanted and affordable(wanted + [lineup]):
                wanted.append(lineup)
    scores: dict[tuple[str, ...], PlanScore] = {
        score.lineup: score for score in evaluate_plans(book, wanted, opp, weight, games, model)
    }
    orders = {plan: [order for order in all_orders[plan] if order in scores] for plan in plans}

    # 每只「本手宠物」取它出现过的方案 × 站位里最高的一份
    best: dict[str, tuple[PlanScore, Plan]] = {}
    for plan in plans:
        for order in orders[plan]:
            score = scores[order]
            pet = order[fixed]
            if pet not in best or score.robust > best[pet][0].robust:
                best[pet] = (score, plan)

    # 引擎对照：引擎评估过的阵容里（穷举，或向导挑出的那几套），最优的明显好于 LLM 的全部方案时，补进候选
    engine_better = False
    proposed = {order for plan in plans for order in orders[plan]}
    top = max(scores.values(), key=lambda score: score.robust)
    if top.lineup not in proposed and top.robust - max(score.robust for score, _ in best.values()) > ENGINE_MARGIN:
        engine_better = True
        best[top.lineup[fixed]] = (top, _engine_plan(top.lineup, fixed, kb))

    ranking = sorted(best.items(), key=lambda item: -item[1][0].robust)
    # 与第一名几乎并列的候选里，优先 LLM 自己排得靠前的方案：这点差距引擎分不出好坏，
    # 而 LLM 的首选通常更连贯（比如延续上一手说过的计划）
    near = [item for item in ranking if ranking[0][1][0].robust - item[1][0].robust <= TIE_MARGIN]
    near.sort(key=lambda item: (item[1][1].rank is None, item[1][1].rank or 0))
    ranking = near + ranking[len(near):]
    pet, (score, plan) = ranking[0]

    # 备选优先取「另一套阵容」：每套阵容出一只（它的最佳站位里本手要选的那只）；
    # 不够 2 只时，再用同一套阵容换个先后顺序来补
    candidates: list[tuple[str, tuple[PlanScore, Plan]]] = []
    for other_plan in plans:
        if set(other_plan.lineup) == set(plan.lineup):
            continue
        options = [scores[order] for order in orders[other_plan] if order[fixed] != pet]
        if options:
            top_order = max(options, key=lambda item: item.robust)
            candidates.append((top_order.lineup[fixed], (top_order, other_plan)))
    candidates.sort(key=lambda item: -item[1][0].robust)
    shortlist: list[tuple[str, tuple[PlanScore, Plan]]] = [ranking[0]]
    for other, item in candidates + ranking[1:]:
        if len(shortlist) >= 3:
            break
        if other not in dict(shortlist):
            shortlist.append((other, item))

    # 复核：从很多候选里挑估计值最高的，挑中的往往是运气好的，估计值偏高。样本不多时，
    # 把推荐和备选用没参与过挑选的新种子各重打一遍，展示这些数字，并按它们定最终推荐
    verified = score.samples < VERIFY_BELOW
    if verified:
        def recheck(old: PlanScore) -> PlanScore:
            support = [(reply, prob) for reply, prob in model(old.lineup).items() if prob > 0]
            fresh = book.verify([(old.lineup, reply) for reply, _ in support],
                                max(16, math.ceil(VERIFY_GAMES / len(support))))
            values = [result.value for result in fresh]
            at = values.index(min(values))
            mean = sum(prob * value for (_, prob), value in zip(support, values))
            return PlanScore(
                lineup=old.lineup, worst=values[at], worst_reply=support[at][0][len(opp):], mean=mean,
                robust=robust_score(values[at], mean, weight),
                win_rate=sum(prob * result.win_rate for (_, prob), result in zip(support, fresh)),
                worst_win_rate=fresh[at].win_rate, replies=len(support),
                samples=sum(result.games for result in fresh),
            )

        shortlist = [(key, (recheck(item[0]), item[1])) for key, item in shortlist]
        rest = sorted(shortlist[1:], key=lambda item: -item[1][0].robust)
        if rest and rest[0][1][0].robust - shortlist[0][1][0].robust > TIE_MARGIN:
            shortlist = sorted(shortlist, key=lambda item: -item[1][0].robust)  # 复核后别的候选明显更好：换成它
        else:
            shortlist = [shortlist[0]] + rest
    pet, (score, plan) = shortlist[0]

    vs_predicted = None
    if plan.predicted_opp is not None:
        vs_predicted = round(book.get(score.lineup, plan.predicted_opp, games_for(1)).value, 3)

    alternatives = []
    for other, (other_score, other_plan) in shortlist[1:]:
        reason = other_plan.reason
        if set(other_plan.lineup) == set(plan.lineup):
            reason = f"同一套阵容，也可以先选{pet_name(other)}。"
        alternatives.append({
            "pet": other, "reason": reason, "plan": list(other_score.lineup),
            "robust": round(other_score.robust, 3), "win_rate": round(other_score.win_rate, 3),
            "source": other_plan.source,
        })

    advice = {
        "pet": pet,
        "reason": plan.reason,
        "alternatives": alternatives,
        "strategy": strategy_text(plan.style, score.lineup, fixed),
        "plan": list(score.lineup),
        "opp_read": opp_read,
        "source": plan.source,
    }
    shown = dict(shortlist)
    evidence = {
        "label": profile.label,
        "chosen": {
            "robust": round(score.robust, 3),
            "win_rate": round(score.win_rate, 3),
            "mean": round(score.mean, 3),
            "worst": round(score.worst, 3),
            "worst_win_rate": round(score.worst_win_rate, 3),
            "worst_reply": list(score.worst_reply),
            "vs_predicted": vs_predicted,
            "replies": score.replies,
            "samples": score.samples,
            "verified": verified,  # 这些数字是否来自选定之后的新种子复核
        },
        # 推荐和备选在前（复核过的用复核后的数字），其余候选按初步评分排在后面
        "ranking": [
            {"pet": key, "robust": round(item[0].robust, 3), "win_rate": round(item[0].win_rate, 3)}
            for key, item in shortlist + [entry for entry in ranking if entry[0] not in shown]
        ],
        "exhaustive": exhaustive,
    }
    return Decision(advice, evidence, plan.rank, engine_better, reordered=score.lineup != plan.lineup)


# ---------------------------------------------------------------- 编排

@dataclass
class DraftResult:
    advice: dict[str, Any]
    evidence: dict[str, Any]
    trace: dict[str, Any]


class DraftAdvisor:
    """一手选将建议的完整流程。LLMError / AdviceFailure 由调用方处理。"""

    def __init__(self, llm: LLM, kb: KnowledgeBase) -> None:
        self.llm = llm
        self.kb = kb

    def advise(self, situation: Situation, book: MatchupBook, attempt: int = 1,
               feedback: str | None = None, prior: MatchupBook | None = None,
               experience: dict[str, Any] | None = None) -> DraftResult:
        """book 可以是对阵表，也可以是叠加了实战经验的 ExperienceBook（接口相同）。"""
        if PICK_ORDER[situation.step] != "me":
            raise ValueError(f"第 {situation.hand} 手不是{SIDE_NAMES['me']}的回合")
        profile = profile_for(situation.opponent)
        user = build_user_message(situation, self.kb, profile, feedback, attempt, experience)
        hint = {
            "token": situation.token, "my_picked": list(situation.my_picked),
            "opp_picked": list(situation.opp_picked), "opp_policy": profile.opp_policy,
        }
        reply = self.llm.complete(SYSTEM_PROMPT, user, hint)
        data, plans, dropped = read_plans(reply.text, reply.finish_reason, situation)

        lookups, hits = book.lookups, book.hits
        start = time.monotonic()
        opp_read = data.get("opp_read")
        decision = decide(situation, plans, book, profile, self.kb,
                          opp_read.strip()[:80] if isinstance(opp_read, str) else "", prior)
        engine_ms = int((time.monotonic() - start) * 1000)
        book.save()

        trace = {
            "attempt": attempt,
            "plans": len(data["plans"]),
            "valid": len(plans),
            "dropped": dropped,
            "matchups": book.lookups - lookups,
            "cache_hits": book.hits - hits,
            "llm_ms": reply.ms,
            "engine_ms": engine_ms,
            "usage": reply.usage,
            "chosen_llm_rank": decision.chosen_llm_rank,
            "engine_better": decision.engine_better,
            "reordered": decision.reordered,
        }
        return DraftResult(decision.advice, decision.evidence, trace)
