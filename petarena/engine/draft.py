"""选将：对手方先手，一人一只轮流选择，共 6 次；选择顺序即站位。"""

from __future__ import annotations

from .pets import SIDE_NAMES, SIDES, SPECIES, TEAM_SIZE

PICK_ORDER = ("opp", "me", "opp", "me", "opp", "me")


class DraftError(ValueError):
    """不合法的选将操作。"""


class Draft:
    def __init__(self, picks: list[list[str]] | None = None) -> None:
        self.picks: list[list[str]] = []  # [[side, species], ...]，确认后不能撤回
        for side, species in picks or []:
            self.pick(side, species)

    @property
    def complete(self) -> bool:
        return len(self.picks) == len(PICK_ORDER)

    @property
    def turn(self) -> str | None:
        """当前该哪一方选；选完返回 None。"""
        return None if self.complete else PICK_ORDER[len(self.picks)]

    def lineup(self, side: str) -> list[str]:
        """某一方已选的宠物，按站位顺序。"""
        return [species for s, species in self.picks if s == side]

    def pick(self, side: str, species: str) -> None:
        if self.complete:
            raise DraftError("选将已经完成")
        if side != self.turn:
            raise DraftError(f"现在轮到{SIDE_NAMES[self.turn]}选择")
        if species not in SPECIES:
            raise DraftError(f"未知宠物：{species}")
        if species in self.lineup(side):
            raise DraftError(f"{SIDE_NAMES[side]}已经选过{SPECIES[species].name}")
        self.picks.append([side, species])

    def to_dict(self) -> dict:
        return {
            "order": list(PICK_ORDER),
            "step": len(self.picks),
            "turn": self.turn,
            "complete": self.complete,
            "picks": [list(p) for p in self.picks],
            "lineups": {side: self.lineup(side) for side in SIDES},
            "team_size": TEAM_SIZE,
        }
