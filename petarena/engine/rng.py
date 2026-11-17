"""战斗随机数：mulberry32。

状态只有一个 32 位整数，因此可以随战斗状态一起克隆和存档；
同一个种子按相同顺序抽取，结果完全一致，这是回放的基础。
"""

from __future__ import annotations

_MASK = 0xFFFFFFFF


class Rng:
    __slots__ = ("state", "draws")

    def __init__(self, seed: int, draws: int = 0) -> None:
        self.state = seed & _MASK
        self.draws = draws

    def copy(self) -> "Rng":
        return Rng(self.state, self.draws)

    def _next_u32(self) -> int:
        self.state = (self.state + 0x6D2B79F5) & _MASK
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & _MASK)) & _MASK
        self.draws += 1
        return (t ^ (t >> 14)) & _MASK

    def below(self, n: int) -> int:
        """均匀抽取 [0, n) 内的整数。"""
        return (self._next_u32() * n) >> 32

    def roll100(self) -> int:
        """抽取 0–99 的整数；概率 p% 的判定成功条件为 roll < p。"""
        return self.below(100)
