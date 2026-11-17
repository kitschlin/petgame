"""事实校验：LLM 写给玩家的文字里，数字只能来自工具结果或上下文里程序给出的数字。

只核对两类数字：百分数（胜率、命中率）和不小于 MIN_CHECKED 的数（伤害、生命这类）。
回合数、能量豆、号位这类小整数随处可见，核对它们只会误伤。
百分数允许 ±PCT_TOLERANCE 个点的误差（写成「约 62%」很正常）。写成「个百分点」的是差距，
可以是两个已知百分数之差（「比第一套低 3 个百分点」）；写成「%」的必须是已知的百分数本身——
否则几乎任何数都能凑成两个胜率之差，等于不校验。
"""

from __future__ import annotations

import re
from typing import Iterable

PCT_TOLERANCE = 0.5
MIN_CHECKED = 11
PAIR_LIMIT = 80  # 已知百分数超过这么多个时不再枚举两两之差（太宽松，等于不校验）

_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)\s*(%|％|个百分点|个点)?")


PLAIN, PCT, POINTS = "plain", "pct", "points"


def numbers(text: str) -> list[tuple[float, str]]:
    """文字里的数：(数值, 种类)。种类：PCT 百分数、POINTS 百分点（差距）、PLAIN 其他。
    编号里的数字（E3、T2、opp1 …）不算。"""
    found = []
    for match in _NUMBER.finditer(text or ""):
        start = match.start()
        if start > 0 and text[start - 1] in "ETet":  # E3 / T2 这类编号
            continue
        unit = match.group(2)
        kind = PLAIN if unit is None else PCT if unit in "%％" else POINTS
        found.append((float(match.group(1)), kind))
    return found


class Facts:
    """一组已知数字（从上下文和工具结果里抽出来）。"""

    def __init__(self, sources: Iterable[str] = ()) -> None:
        self.plain: set[float] = set()
        self.pct: set[float] = set()
        for text in sources:
            self.add(text)

    def add(self, text: str) -> None:
        for value, kind in numbers(text):
            if kind != PLAIN:
                self.pct.add(value)
            else:
                self.plain.add(value)
                if value <= 1:  # 0.552 这样的小数也可能被写成 55.2%
                    self.pct.add(round(value * 100, 1))

    def add_pct(self, values: Iterable[float]) -> None:
        """直接加入百分数（如 0.552 → 55.2）。"""
        for value in values:
            self.pct.add(round(value * 100, 1))

    def knows_pct(self, value: float, gap: bool = False) -> bool:
        known = sorted(self.pct)
        if any(abs(value - other) <= PCT_TOLERANCE for other in known):
            return True
        if gap and len(known) <= PAIR_LIMIT:
            return any(abs(value - abs(a - b)) <= PCT_TOLERANCE for i, a in enumerate(known) for b in known[i + 1:])
        return False

    def knows(self, value: float) -> bool:
        return any(abs(value - other) <= 0.011 for other in self.plain)

    def problems(self, text: str, field: str) -> list[str]:
        found = []
        for value, kind in numbers(text):
            shown = f"{value:g}" + {PLAIN: "", PCT: "%", POINTS: " 个百分点"}[kind]
            if kind != PLAIN and not self.knows_pct(value, gap=kind == POINTS):
                found.append(f"{field} 里的 {shown} 不在工具结果或上下文里：数字只能照抄，不能自己估算")
            elif kind == PLAIN and value >= MIN_CHECKED and not self.knows(value) and not self.knows_pct(value):
                found.append(f"{field} 里的 {shown} 不在工具结果或上下文里：数字只能照抄，不能自己计算")
        return found
