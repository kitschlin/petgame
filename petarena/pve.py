"""PVE 属性副本：不运行实际战斗，每次挑战做一次概率判定。只能培养我方宠物。

永久成长不单独存档，而是由 PVE 记录累加得出：每条成功记录让对应阵营、
对应宠物的对应属性 +1。这样一次挑战只有一次写入，不会出现「属性加了但
记录没写」或重复结算。
"""

from __future__ import annotations

from typing import Any

from .engine.pets import BASE_STATS, HIT_CAP, SIDES, SPECIES, STAT_KEYS, STAT_NAMES, battle_speed

DUNGEONS: dict[str, dict[str, str]] = {
    "atk": {"name": "攻击副本", "stat": "atk", "effect": "攻击永久 +1"},
    "hp": {"name": "生命副本", "stat": "max_hp", "effect": "生命上限永久 +1"},
    "speed": {"name": "速度副本", "stat": "speed", "effect": "基础速度永久 +1"},
    "hit": {"name": "命中副本", "stat": "hit", "effect": "效果命中永久 +1%，最高 100%"},
}

# enabled=False 的模式不出现在 PVE 页，服务端也不接受新的挑战；以前用它留下的记录照常显示。
# 本版关闭了自动托管模式，把 enabled 改回 True 即可恢复（前端的连续挑战流程还在）。
MODES: dict[str, dict[str, Any]] = {
    "manual": {"name": "手动模式", "chance": 30, "enabled": True},
    "auto": {"name": "自动托管模式", "chance": 40, "enabled": False},
}


def best_chance() -> float:
    """玩家现在能用的模式里最高的通关概率（0–1），用来估算练一项属性要挑战几次。"""
    return max(m["chance"] for m in MODES.values() if m["enabled"]) / 100


# 只有我方可以历练；对手方属性固定为基础属性表
PVE_SIDES = ("me",)


class PveError(ValueError):
    """不合法的 PVE 请求。"""


def zero_growth() -> dict[str, dict[str, dict[str, int]]]:
    return {side: {key: dict.fromkeys(STAT_KEYS, 0) for key in SPECIES} for side in SIDES}


def apply(growth: dict, record: dict[str, Any]) -> None:
    """把一条 PVE 记录的成长效果计入 growth。"""
    if record["success"] and record["side"] in PVE_SIDES:  # 旧存档里的对手方记录不再生效
        growth[record["side"]][record["species"]][record["stat"]] += 1


def stats_of(growth: dict, side: str, species: str) -> dict[str, int]:
    """当前永久属性 = 基础属性 + 成长。"""
    base = BASE_STATS[side][species]
    gained = growth[side][species]
    return {key: base[key] + gained[key] for key in STAT_KEYS}


def roster(growth: dict) -> dict[str, dict[str, dict[str, int]]]:
    """双方全部宠物的当前属性，附带开战后的速度。"""
    result: dict[str, dict[str, dict[str, int]]] = {}
    for side in SIDES:
        result[side] = {}
        for key in SPECIES:
            stats = stats_of(growth, side, key)
            stats["battle_speed"] = battle_speed(key, stats["speed"])
            result[side][key] = stats
    return result


def is_capped(stats: dict[str, int], dungeon: str) -> bool:
    """只有效果命中有上限。"""
    return dungeon == "hit" and stats["hit"] >= HIT_CAP


def validate(side: str, species: str, dungeon: str, mode: str) -> None:
    if side not in PVE_SIDES:
        raise PveError("只能培养我方宠物")
    if species not in SPECIES:
        raise PveError(f"未知宠物：{species}")
    if dungeon not in DUNGEONS:
        raise PveError(f"未知副本：{dungeon}")
    if mode not in MODES:
        raise PveError(f"未知模式：{mode}")
    if not MODES[mode]["enabled"]:
        raise PveError(f"{MODES[mode]['name']}已关闭")


def settle(
    growth: dict,
    *,
    challenge_id: str,
    side: str,
    species: str,
    dungeon: str,
    mode: str,
    roll: int,
    time: str,
) -> dict[str, Any]:
    """结算一次挑战：roll 是 0–99 的随机数，小于通关概率即成功。"""
    validate(side, species, dungeon, mode)
    stat = DUNGEONS[dungeon]["stat"]
    chance = MODES[mode]["chance"]
    before = stats_of(growth, side, species)[stat]
    if is_capped(stats_of(growth, side, species), dungeon):
        raise PveError(f"{STAT_NAMES[stat]}已达到上限 {HIT_CAP}%")
    success = roll < chance
    record = {
        "id": challenge_id,
        "time": time,
        "side": side,
        "species": species,
        "dungeon": dungeon,
        "mode": mode,
        "chance": chance,
        "roll": roll,
        "success": success,
        "stat": stat,
        "before": before,
        "after": before + 1 if success else before,
    }
    apply(growth, record)
    return record
