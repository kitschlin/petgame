"""对手方由电脑操作的模式。

随机模式：随机选将、随机出招（能用特色技能就用，目标随机），实现在本文件。
智能模式：只用小猫、小狗、小鼠，按对阵经验针对我方决定先后；出招有技能就放，实现在 coach/opponent.py。

随机数不来自战斗种子（那条随机序列由规则规定了抽取顺序），而是由每场对局的
ai_seed 加上「第几步」派生，因此不需要保存随机数状态，重启后也能复现同样的选择。
AI 的选择会和玩家的选择一样记进选将记录和行动序列，回放不依赖这里。
"""

from __future__ import annotations

import random

from .engine import Battle
from .engine.pets import SPECIES_ORDER

MODES = {"manual": "手动", "random": "随机", "smart": "智能"}
AI_MODES = ("random", "smart")  # 由电脑操作对手方的模式
AI_SIDE = "opp"


def rng_for(ai_seed: int, phase: str, step: int) -> random.Random:
    return random.Random(f"{ai_seed}:{phase}:{step}")


def random_pick(taken: list[str], rng: random.Random) -> str:
    """从本方还没选过的宠物里随机选一只。"""
    return rng.choice([key for key in SPECIES_ORDER if key not in taken])


def random_move(battle: Battle, rng: random.Random) -> tuple[str, str | None]:
    """能用特色技能就用，否则普通攻击；需要选目标时在合法目标里随机选。"""
    basic, special = battle.options()
    option = special if special["enabled"] else basic
    target = None if option["targets"] is None else rng.choice(option["targets"])
    return option["skill"], target
