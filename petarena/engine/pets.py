"""宠物、技能的静态定义，以及两套基础属性表（AGENTS.md 第五节）。"""

from __future__ import annotations

from dataclasses import dataclass

# 阵营。对手方排在前面：界面上方、选将先手。
SIDES = ("opp", "me")
SIDE_NAMES = {"me": "我方", "opp": "对手方"}

# ---- 战斗常量 ----
ENERGY_START = 3
ENERGY_CAP = 8
ENERGY_REGEN = 2
MAX_ROUNDS = 10
TEAM_SIZE = 3

MOUSE_START_BONUS = 3
MOUSE_SKILL_GAIN = 3
MOUSE_SKILL_HP_PCT = 10  # 零食补给消耗小鼠自身生命上限的 10%
SPEED_PASSIVE_BONUS = 100
CAT_SKILL_PCT = 200
DOG_SKILL_PCT = 80
DOG_PURSUIT_CHANCE = 50
TURTLE_SKILL_PCT = 30
TURTLE_PASSIVE_PCT = 10
DEER_SKILL_PCT = 30
DEER_PASSIVE_PCT = 10
BARRIER_DAMAGE_PCT = 30
BARRIER_ROUNDS = 2
CHAIN_KEEP_PCT = 95  # 水链减免 5%
CHAIN_ROUNDS = 2

# 特色技能的目标类型
TARGET_ENEMY = "enemy"  # 任意一名存活敌人
TARGET_ALL_ENEMIES = "all_enemies"  # 所有存活敌人，无需选择
TARGET_UNACTED_ALLY = "unacted_ally"  # 本回合尚未行动的存活队友（不含自己）
TARGET_ALLY = "ally"  # 任意一名存活己方宠物，可以是自己
TARGET_NONE = "none"  # 无需选择目标

BASIC_NAME = "普通攻击"
BASIC_DESC = "不消耗能量豆，对一个敌人造成相当于自身攻击力的伤害"


@dataclass(frozen=True)
class Species:
    key: str
    name: str
    emoji: str
    role: str
    skill_name: str
    skill_cost: int
    skill_target: str
    skill_desc: str
    passive_name: str
    passive_desc: str


SPECIES: dict[str, Species] = {
    s.key: s
    for s in (
        Species(
            "cat", "小猫", "🐱", "单体爆发、收割",
            "猛爪突袭", 3, TARGET_ENEMY,
            "消耗 3 点，对单个敌人造成自身攻击力 200% 的伤害",
            "连环收割",
            "直接伤害击倒目标后，将实际溢出伤害转移给站位在其之后的下一名存活敌人；若再次击倒且仍有溢出，则继续",
        ),
        Species(
            "dog", "小狗", "🐶", "群体输出、协同追击",
            "汪汪冲击", 3, TARGET_ALL_ENEMIES,
            "消耗 3 点，对每个敌人造成自身攻击力 80% 的伤害",
            "协同补刀",
            "其他队友主动普攻后，有 50% 概率对同一目标追加一次普攻",
        ),
        Species(
            "rabbit", "小兔", "🐰", "抢先行动、调整顺序",
            "快快跟上", 2, TARGET_UNACTED_ALLY,
            "消耗 2 点，让一名本回合尚未行动的队友紧接自己行动",
            "抢先一步",
            "开局速度增加 100 点，持续整场战斗",
        ),
        Species(
            "mouse", "小鼠", "🐭", "能量补给",
            "零食补给", 0, TARGET_NONE,
            "不消耗能量，但消耗自身生命上限 10% 的生命，为队伍恢复 3 点能量豆，不超过上限；生命不足时不能释放",
            "提前备粮",
            "开局额外提供 3 点能量豆，使本方初始能量变为 6 点",
        ),
        Species(
            "turtle", "小乌", "🐢", "护盾、防护",
            "壳壳保护", 2, TARGET_ALLY,
            "消耗 2 点，为一名己方宠物（可以是自己）提供其生命上限 30% 的护盾",
            "全队守护",
            "开局为己方所有宠物提供各自生命上限 10% 的护盾",
        ),
        Species(
            "deer", "小鹿", "🦌", "治疗、续航",
            "治愈嫩芽", 2, TARGET_ALLY,
            "消耗 2 点，为一名己方宠物（可以是自己）恢复其生命上限 30% 的生命",
            "自然恢复",
            "自身行动机会结束时，若仍存活，恢复自身生命上限 10% 的生命",
        ),
        Species(
            "snake", "小蛇", "🐍", "结界伤害、冻结控制",
            "寒毒结界", 3, TARGET_NONE,
            "消耗 3 点，生成持续两个大回合的结界；敌方宠物每次行动机会开始前，受到小蛇攻击力 30% 的伤害",
            "寒毒侵袭",
            "结界对当前行动宠物造成实际生命伤害后，按小蛇效果命中率判定冻结；冻结立即生效，该宠物跳过本次操作",
        ),
        Species(
            "fish", "小鱼", "🐟", "提前建立伤害分摊",
            "同心水链", 3, TARGET_NONE,
            "消耗 3 点，连接己方存活宠物，持续两个大回合；受到伤害时先减免 5%，再由连接中的存活宠物均分。水链存在期间不能再次释放",
            "灵动游弋",
            "开局速度增加 100 点，持续整场战斗",
        ),
    )
}

SPECIES_ORDER = tuple(SPECIES)

# 永久属性的四个字段：生命上限、攻击、基础速度、效果命中（整数百分比）
STAT_KEYS = ("max_hp", "atk", "speed", "hit")
STAT_NAMES = {"max_hp": "生命上限", "atk": "攻击", "speed": "基础速度", "hit": "效果命中"}
HIT_CAP = 100


def _table(rows: dict[str, tuple[int, int, int, int]]) -> dict[str, dict[str, int]]:
    return {key: dict(zip(STAT_KEYS, row)) for key, row in rows.items()}


BASE_STATS: dict[str, dict[str, dict[str, int]]] = {
    # 我方：迅捷突击（在最初的数值上每只生命 −3、攻击 −1、速度 −1）
    "me": _table({
        "cat": (72, 32, 73, 0),
        "dog": (82, 26, 61, 0),
        "rabbit": (62, 11, 91, 0),
        "mouse": (72, 9, 81, 0),
        "turtle": (92, 9, 31, 0),
        "deer": (82, 9, 53, 0),
        "snake": (72, 21, 77, 25),
        "fish": (92, 9, 87, 0),
    }),
    # 对手方：坚韧控制。整体强于初始的我方（用 scripts/balance.py 校准）：
    # 生命拉满、辅助宠物也有可观攻击，任何搭配都有威胁；
    # 代价是速度普遍慢于我方、主攻手爆发不如我方。我方需要 PVE 成长或针对性阵容才能稳定取胜。
    # 小猫、小狗、小鼠（智能对手的固定阵容）在最初的数值上攻击各 +2。
    "opp": _table({
        "cat": (100, 28, 68, 0),
        "dog": (100, 24, 60, 0),
        "rabbit": (95, 20, 88, 0),
        "mouse": (100, 26, 78, 0),
        "turtle": (100, 24, 40, 0),
        "deer": (100, 24, 56, 0),
        "snake": (100, 20, 74, 50),
        "fish": (100, 24, 84, 0),
    }),
}


def validate_base_stats(table: dict[str, dict[str, dict[str, int]]] = BASE_STATS) -> None:
    """初始属性的配置约束：生命 (0,100]，攻击、速度、命中 [0,100]，且均为整数。"""
    for side in SIDES:
        if set(table[side]) != set(SPECIES):
            raise ValueError(f"{side} 的宠物配置不完整")
        for key, stats in table[side].items():
            for stat in STAT_KEYS:
                value = stats[stat]
                low = 1 if stat == "max_hp" else 0
                if not isinstance(value, int) or not low <= value <= 100:
                    raise ValueError(f"{side}/{key}/{stat}={value} 超出初始配置范围")


def battle_speed(species: str, base_speed: int) -> int:
    """开战后的最终速度：小兔、小鱼的被动 +100，不写回永久属性。"""
    if species in ("rabbit", "fish"):
        return base_speed + SPEED_PASSIVE_BONUS
    return base_speed


validate_base_stats()
