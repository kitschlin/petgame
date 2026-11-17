"""战斗中的可变状态。生命、护盾以「分」为单位（见 numeric.py）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Pet:
    id: str  # 例如 "me1"、"opp3"：阵营 + 站位
    side: str
    slot: int  # 1–3
    species: str
    max_hp: int  # 分
    atk: int
    base_speed: int
    speed: int  # 开战后的最终速度
    hit: int  # 效果命中，整数百分比
    hp: int  # 分
    shield: int = 0  # 分
    alive: bool = True
    frozen: bool = False

    def copy(self) -> "Pet":
        return Pet(
            self.id, self.side, self.slot, self.species, self.max_hp, self.atk,
            self.base_speed, self.speed, self.hit, self.hp, self.shield,
            self.alive, self.frozen,
        )


@dataclass(slots=True)
class Barrier:
    """寒毒结界：数值在施放时记录，作用于敌方。"""

    atk: int
    hit: int
    expires: int  # 在第几个大回合结束时消失


@dataclass(slots=True)
class Chain:
    """同心水链：members 是仍连接着的存活宠物 id，按站位顺序。"""

    members: list[str]
    expires: int


@dataclass(slots=True)
class Team:
    side: str
    beans: int = 0
    barrier: Barrier | None = None
    chain: Chain | None = None

    def copy(self) -> "Team":
        barrier = self.barrier
        chain = self.chain
        return Team(
            self.side,
            self.beans,
            Barrier(barrier.atk, barrier.hit, barrier.expires) if barrier else None,
            Chain(list(chain.members), chain.expires) if chain else None,
        )
