"""出招策略：对阵推演和数值平衡模拟共用同一套打法。

策略的签名都是 (battle, rng) -> (技能, 目标)。rng 是独立的 random.Random，
不占用战斗随机种子。

- heuristic：按常识出招（能放有意义的技能就放，否则普攻集火最残的敌人）。
- random_mode：对手方「随机模式」的真实打法（能用特色技能就用，目标随机），
  用来推演「对手是电脑随机」时的胜率。
- skill_first：有技能就放，目标挑得有道理（敌方集火血量最少的，己方护盾 / 治疗
  给最需要的，小兔拉攻击最高的队友）。智能对手最初的打法，现在留作对照。
- expert：对手方「智能模式」现在的打法：逐个推演合法动作，取最好的（见 rollout.py）。
- uniform：在全部合法动作里等概率随机。逐动作推演时，我方执行完被评估的这一手之后就这样打完。
"""

from __future__ import annotations

import random
from typing import Callable

from ..ai import random_move
from ..engine import Battle
from ..engine.pets import ENERGY_CAP

Move = tuple[str, str | None]
Policy = Callable[[Battle, random.Random], Move]


def effective_hp(pet) -> int:
    return pet.hp + pet.shield


def choose(b: Battle, rng: random.Random) -> Move:
    """启发式出招：能放有意义的技能就放，否则普攻集火最残的敌人。"""
    pet = b.pets[b.actor]
    team = b.teams[pet.side]
    special = b.options()[1]
    enemies = b.alive(b.enemy(pet.side))
    allies = b.alive(pet.side)
    weakest = min(enemies, key=lambda p: (effective_hp(p), p.slot))
    basic = ("basic", weakest.id)
    if not special["enabled"]:
        return basic
    beans = team.beans
    kind = pet.species
    if kind == "cat":
        return ("special", weakest.id)
    if kind == "dog":
        return ("special", None) if len(enemies) >= 2 else basic
    if kind == "rabbit":
        # 把攻击最高、尚未行动的队友拉到前面；能量要留给大招时就普攻
        candidates = [b.pets[t] for t in special["targets"]]
        best = max(candidates, key=lambda p: p.atk)
        if best.atk >= 18 and beans >= 5:
            return ("special", best.id)
        return basic
    if kind == "mouse":
        return ("special", None) if beans <= 5 else basic
    if kind == "turtle":
        target = min(allies, key=lambda p: (p.hp + p.shield) / p.max_hp)
        if (target.hp + target.shield) / target.max_hp < 0.75:
            return ("special", target.id)
        return basic
    if kind == "deer":
        target = max(allies, key=lambda p: p.max_hp - p.hp)
        if target.max_hp - target.hp >= target.max_hp * 0.3:
            return ("special", target.id)
        return basic
    if kind == "snake":
        return ("special", None) if team.barrier is None else basic
    if kind == "fish":
        return ("special", None) if len(allies) >= 2 else basic
    return basic


def skill_first(b: Battle, rng: random.Random) -> Move:
    """有技能就放技能（能量够、有合法目标、水链不在冷却），否则普攻；目标都挑最有用的。

    唯一的例外：能量豆已满时小鼠不放零食补给（放了也加不了能量，白扣自己的血）。
    """
    pet = b.pets[b.actor]
    special = b.options()[1]
    weakest = min(b.alive(b.enemy(pet.side)), key=lambda p: (effective_hp(p), p.slot))
    full = pet.species == "mouse" and b.teams[pet.side].beans >= ENERGY_CAP
    if not special["enabled"] or full:
        return ("basic", weakest.id)
    if special["targets"] is None:
        return ("special", None)
    targets = [b.pets[t] for t in special["targets"]]
    if targets[0].side != pet.side:  # 打敌人的技能：集火最残的
        return ("special", min(targets, key=lambda p: (effective_hp(p), p.slot)).id)
    if pet.species == "rabbit":  # 拉攻击最高的队友
        return ("special", max(targets, key=lambda p: (p.atk, -p.slot)).id)
    if pet.species == "deer":  # 治疗掉血最多的
        return ("special", max(targets, key=lambda p: (p.max_hp - p.hp, -p.slot)).id)
    # 护盾给（生命 + 护盾）占比最低的
    return ("special", min(targets, key=lambda p: ((p.hp + p.shield) / p.max_hp, p.slot)).id)


def uniform(b: Battle, rng: random.Random) -> Move:
    """在全部合法动作（技能 × 目标）里等概率随机选一个。推演里我方执行完这一手之后就这样出招。"""
    return rng.choice(b.legal_moves())


def expert(b: Battle, rng: random.Random) -> Move:
    """推演出招：把每个合法动作各推演若干局（之后双方按常规打法），取最好的。"""
    from .rollout import expert_move  # 延迟导入：推演内核依赖本模块

    return expert_move(b, rng)


HEURISTIC = "heuristic"
RANDOM_MODE = "random"
SKILL_FIRST = "skill_first"
EXPERT = "expert"
UNIFORM = "uniform"

POLICIES: dict[str, Policy] = {
    HEURISTIC: choose,
    RANDOM_MODE: random_move,
    SKILL_FIRST: skill_first,
    EXPERT: expert,
    UNIFORM: uniform,
}
