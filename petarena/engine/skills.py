"""普通攻击、8 个特色技能，以及与出手相关的被动（连环收割、协同补刀）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .damage import grant_shield, heal, strike, strike_all
from .numeric import cents, mul_pct, num
from .pets import (
    BARRIER_ROUNDS,
    CAT_SKILL_PCT,
    CHAIN_ROUNDS,
    DEER_SKILL_PCT,
    DOG_PURSUIT_CHANCE,
    DOG_SKILL_PCT,
    ENERGY_CAP,
    MOUSE_SKILL_GAIN,
    MOUSE_SKILL_HP_PCT,
    SPECIES,
    TURTLE_SKILL_PCT,
)
from .state import Barrier, Chain, Pet

if TYPE_CHECKING:
    from .battle import Battle


def basic_attack(b: "Battle", pet: Pet, target: Pet) -> None:
    amount = cents(pet.atk)
    if pet.species == "cat":
        _cat_strike(b, pet, target, amount, "basic")
    else:
        strike(b, target, amount, source=pet.id, kind="basic")
    _pursuit(b, pet, target)


def _cat_strike(b: "Battle", cat: Pet, target: Pet, amount: int, kind: str) -> None:
    """小猫的直接伤害 + 连环收割。

    溢出按直接目标经过减免、分摊、护盾之后的实际剩余伤害计算；转移后以新目标
    为直接目标重新走完整的伤害管线。被水链分摊致死的其他宠物不产生溢出。
    """
    outcome = strike(b, target, amount, source=cat.id, kind=kind)
    while outcome.overflow > 0 and not b.finished:
        nxt = b.next_alive_after(target)
        if nxt is None:
            break
        target = nxt
        outcome = strike(b, target, outcome.overflow, source=cat.id, kind="overflow")


def _pursuit(b: "Battle", attacker: Pet, target: Pet) -> None:
    """协同补刀：其他队友主动普攻后，存活的小狗 50% 概率对同一目标追加一次普攻。"""
    if b.finished or attacker.species == "dog" or not target.alive:
        return
    dog = b.find(attacker.side, "dog")
    if dog is None or not dog.alive:
        return
    roll = b.rng.roll100()
    success = roll < DOG_PURSUIT_CHANCE
    b.emit(
        "pursuit_check", pet=dog.id, target=target.id,
        chance=DOG_PURSUIT_CHANCE, roll=roll, success=success,
    )
    if success:
        strike(b, target, cents(dog.atk), source=dog.id, kind="pursuit")


def _cat(b: "Battle", pet: Pet, target: Pet | None) -> None:
    _cat_strike(b, pet, target, mul_pct(cents(pet.atk), CAT_SKILL_PCT), "skill")


def _dog(b: "Battle", pet: Pet, target: Pet | None) -> None:
    amount = mul_pct(cents(pet.atk), DOG_SKILL_PCT)
    strike_all(b, b.alive(b.enemy(pet.side)), amount, source=pet.id, kind="aoe")


def _rabbit(b: "Battle", pet: Pet, target: Pet | None) -> None:
    # 小兔的行动机会随后立即结束，被拉的队友排到队首，紧接着开始自己的行动机会。
    b.queue.remove(target.id)
    b.queue.insert(0, target.id)
    b.emit("pull", pet=pet.id, target=target.id)


def mouse_skill_cost(pet: Pet) -> int:
    """零食补给消耗的生命（分）：生命上限的 10%，四舍五入到两位小数。"""
    return mul_pct(pet.max_hp, MOUSE_SKILL_HP_PCT)


def _mouse(b: "Battle", pet: Pet, target: Pet | None) -> None:
    # 先扣自身生命：直接扣生命，不经过护盾、水链，也不算伤害（不触发冻结、溢出等）。
    # 释放条件保证扣完后仍然存活，所以这里不会倒下。
    cost = mouse_skill_cost(pet)
    pet.hp -= cost
    b.emit("hp_cost", pet=pet.id, amount=num(cost), hp=num(pet.hp), skill=SPECIES["mouse"].skill_name)
    team = b.teams[pet.side]
    before = team.beans
    team.beans = min(ENERGY_CAP, team.beans + MOUSE_SKILL_GAIN)
    b.emit("energy", side=pet.side, gain=team.beans - before, beans=team.beans)


def _turtle(b: "Battle", pet: Pet, target: Pet | None) -> None:
    grant_shield(
        b, target, mul_pct(target.max_hp, TURTLE_SKILL_PCT),
        source=pet.id, skill=SPECIES["turtle"].skill_name,
    )


def _deer(b: "Battle", pet: Pet, target: Pet | None) -> None:
    heal(
        b, target, mul_pct(target.max_hp, DEER_SKILL_PCT),
        source=pet.id, skill=SPECIES["deer"].skill_name,
    )


def _snake(b: "Battle", pet: Pet, target: Pet | None) -> None:
    team = b.teams[pet.side]
    refreshed = team.barrier is not None
    # 重复释放：刷新持续时间并改用本次施放时的数值，不叠加。
    team.barrier = Barrier(pet.atk, pet.hit, b.round + BARRIER_ROUNDS - 1)
    b.emit(
        "barrier_cast", side=pet.side, pet=pet.id,
        expires=team.barrier.expires, refreshed=refreshed,
    )


def _fish(b: "Battle", pet: Pet, target: Pet | None) -> None:
    team = b.teams[pet.side]
    members = [p.id for p in b.alive(pet.side)]
    team.chain = Chain(members, b.round + CHAIN_ROUNDS - 1)
    b.emit(
        "chain_cast", side=pet.side, pet=pet.id,
        members=list(members), expires=team.chain.expires,
    )


SPECIALS: dict[str, Callable[["Battle", Pet, Pet | None], None]] = {
    "cat": _cat,
    "dog": _dog,
    "rabbit": _rabbit,
    "mouse": _mouse,
    "turtle": _turtle,
    "deer": _deer,
    "snake": _snake,
    "fish": _fish,
}
