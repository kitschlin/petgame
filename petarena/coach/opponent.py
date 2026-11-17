"""对手方「智能模式」：全池针对选将 + 推演出招。

它是检验 AI Coach 的强对手，不是随手可破的陪练：

- 选将：8 只宠物都可能用。把选将看成一棵博弈树（对手、我方、对手、我方、对手、
  我方），叶子是基础属性下「双方常规打法」的对阵价值；对手方取最小、我方取最大。
  每一手对还没拿的候选算出「选了它之后，我方再怎么应对最多还剩多少胜算」，在最能
  压低我方胜算的几只里随机挑一只（留一点随机，免得每局都一样）。它看得到我方已选
  的宠物，所以会针对；整棵树只有十几万个结点，第一次用到时解一遍（约 0.3 秒）。
- 出招：逐个推演合法动作，取最好的（policy.expert，见 rollout.py）。

随机数由对局的 ai_seed 派生，不占用战斗随机种子。

AI 军师面对智能对手时，用同一个 pick_candidates() 预测它接下来会怎么补
（见 reply_model），这相当于「知道对手的选将习惯」，但不知道它并列时具体挑哪只。
"""

from __future__ import annotations

import random
from functools import lru_cache
from typing import Sequence

from ..engine.pets import SPECIES_ORDER, TEAM_SIZE
from .matchup import LINEUP_INDEX, BaseTable, Lineup, ReplyModel, load_base_table
from .policy import EXPERT, HEURISTIC, expert

POLICY = EXPERT  # 智能对手的出招方式
TABLE_POLICY = HEURISTIC  # 选将博弈树用哪张对阵表（双方常规打法）
NEAR_BEST = 0.02  # 与最优相差不超过这么多的候选都可能被选中
FALLBACK = ("cat", "dog", "mouse") + tuple(key for key in SPECIES_ORDER if key not in ("cat", "dog", "mouse"))

smart_move = expert


def _opp_turn(opp: Lineup, my: Lineup) -> bool:
    return len(opp) < TEAM_SIZE and (len(opp) <= len(my) or len(my) >= TEAM_SIZE)


@lru_cache(maxsize=400_000)
def _minimax(opp: Lineup, my: Lineup, table: BaseTable) -> float:
    """选将博弈树的值：我方的局面价值，对手方每一手让它最小，我方每一手让它最大。"""
    if len(opp) == TEAM_SIZE and len(my) == TEAM_SIZE:
        return table.values(TABLE_POLICY)[LINEUP_INDEX[my]][LINEUP_INDEX[opp]]
    if _opp_turn(opp, my):
        return min(_minimax(opp + (key,), my, table) for key in SPECIES_ORDER if key not in opp)
    return max(_minimax(opp, my + (key,), table) for key in SPECIES_ORDER if key not in my)


def draft_value(opp_taken: Sequence[str], my_taken: Sequence[str], table: BaseTable | None = None) -> float:
    """从这个选将局面出发、双方都选得最好时，我方的局面价值（常规打法口径）。"""
    table = table if table is not None else load_base_table()
    return _minimax(tuple(opp_taken), tuple(my_taken), table)


def pick_scores(opp_taken: Sequence[str], my_taken: Sequence[str], table: BaseTable) -> dict[str, float]:
    """每只候选宠物的得分：选了它之后，我方最多还能争取到的局面价值（越低对对手越有利）。"""
    opp, my = tuple(opp_taken), tuple(my_taken)
    return {key: _minimax(opp + (key,), my, table) for key in SPECIES_ORDER if key not in opp}


@lru_cache(maxsize=65536)
def _candidates(opp_taken: Lineup, my_taken: Lineup, table: BaseTable | None) -> tuple[str, ...]:
    if table is None or not table.has(TABLE_POLICY):
        return (next(key for key in FALLBACK if key not in opp_taken),)  # 没有对阵表：按固定顺序
    scores = pick_scores(opp_taken, my_taken, table)
    best = min(scores.values())
    return tuple(key for key, score in scores.items() if score - best <= NEAR_BEST)


def pick_candidates(
    opp_taken: Sequence[str], my_taken: Sequence[str], table: BaseTable | None = None,
) -> tuple[str, ...]:
    """智能对手这一手可能选的宠物（等概率）。"""
    table = table if table is not None else load_base_table()
    return _candidates(tuple(opp_taken), tuple(my_taken), table)


def smart_pick(
    opp_taken: Sequence[str], my_taken: Sequence[str], rng: random.Random, table: BaseTable | None = None,
) -> str:
    return rng.choice(pick_candidates(opp_taken, my_taken, table))


def reply_model(opp_picked: Sequence[str], table: BaseTable | None = None) -> ReplyModel:
    """预测智能对手的补选：给定我方计划的完整阵容（顺序即选择顺序），返回它各种最终阵容的概率。

    对手选第 k+1 只时，看得到我方已经选好的前 k 只。
    """
    table = table if table is not None else load_base_table()
    start = tuple(opp_picked)

    def distribution(lineup: Lineup) -> dict[Lineup, float]:
        result: dict[Lineup, float] = {}

        def walk(opp: Lineup, prob: float) -> None:
            if len(opp) == TEAM_SIZE:
                result[opp] = result.get(opp, 0.0) + prob
                return
            candidates = _candidates(opp, tuple(lineup[:len(opp)]), table)
            for key in candidates:
                walk(opp + (key,), prob / len(candidates))

        walk(start, 1.0)
        return result

    return distribution
