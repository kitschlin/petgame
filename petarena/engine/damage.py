"""伤害、治疗、护盾的统一结算管线。

一次伤害的处理顺序：水链减免 5% → 链上存活宠物均分 → 各自先扣护盾再扣生命
→ 处理阵亡 → 检查胜负。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .numeric import div_round, mul_pct, num
from .pets import CHAIN_KEEP_PCT
from .state import Pet

if TYPE_CHECKING:
    from .battle import Battle


@dataclass(slots=True)
class Outcome:
    """一次伤害对「直接目标」的结果。"""

    hp_lost: int  # 直接目标实际损失的生命（分）
    overflow: int  # 直接目标被击倒后剩余的溢出伤害（分）


def _absorb(pet: Pet, amount: int) -> tuple[int, int, int]:
    """先扣护盾再扣生命。返回（护盾抵扣，生命损失，溢出）。"""
    absorbed = min(pet.shield, amount)
    pet.shield -= absorbed
    rest = amount - absorbed
    lost = min(pet.hp, rest)
    pet.hp -= lost
    return absorbed, lost, rest - lost


def _apply(b: "Battle", target: Pet, amount: int, source: str | None, kind: str) -> Outcome:
    """把一次伤害落到宠物身上并记录事件，但不处理阵亡。"""
    chain = b.teams[target.side].chain
    if chain is not None and target.id in chain.members:
        # 每次伤害只减免一次；每一份单独四舍五入，不补差。
        # 群体伤害结算期间不处理阵亡，所以这里的 members 就是技能开始时的链成员，
        # 中途生命归零的宠物仍然参与均分。
        reduced = mul_pct(amount, CHAIN_KEEP_PCT)
        share = div_round(reduced, len(chain.members))
        receivers = [b.pets[i] for i in chain.members]
    else:
        reduced = None
        share = amount
        receivers = [target]

    outcome = Outcome(0, 0)
    parts = []
    for pet in receivers:
        absorbed, lost, excess = _absorb(pet, share)
        if pet is target:
            outcome = Outcome(lost, excess)
        parts.append({
            "pet": pet.id,
            "amount": num(share),
            "absorbed": num(absorbed),
            "lost": num(lost),
            "hp": num(pet.hp),
            "shield": num(pet.shield),
        })
    b.emit(
        "damage",
        kind=kind,
        source=source,
        target=target.id,
        amount=num(amount),
        reduced=None if reduced is None else num(reduced),
        parts=parts,
    )
    return outcome


def settle(b: "Battle") -> None:
    """处理阵亡，然后立即检查胜负。"""
    for pet in b.pets.values():
        if pet.alive and pet.hp <= 0:
            b.knock_out(pet)
    b.check_end()


def strike(b: "Battle", target: Pet, amount: int, *, source: str | None, kind: str) -> Outcome:
    """对单个目标结算一次伤害。"""
    outcome = _apply(b, target, amount, source, kind)
    settle(b)
    return outcome


def strike_all(b: "Battle", targets: list[Pet], amount: int, *, source: str, kind: str) -> None:
    """群体伤害：按站位顺序逐个计算，全部结算完再统一处理阵亡。"""
    for target in targets:
        _apply(b, target, amount, source, kind)
    settle(b)


def heal(b: "Battle", pet: Pet, amount: int, *, source: str, skill: str) -> None:
    """恢复生命，不超过上限。"""
    actual = min(amount, pet.max_hp - pet.hp)
    pet.hp += actual
    b.emit(
        "heal", pet=pet.id, source=source, skill=skill,
        amount=num(amount), actual=num(actual), hp=num(pet.hp),
    )


def grant_shield(b: "Battle", pet: Pet, amount: int, *, source: str, skill: str) -> None:
    """护盾不叠加：保留当前剩余护盾与新护盾中较高的一份。"""
    replaced = amount > pet.shield
    if replaced:
        pet.shield = amount
    b.emit(
        "shield", pet=pet.id, source=source, skill=skill,
        amount=num(amount), shield=num(pet.shield), replaced=replaced,
    )
