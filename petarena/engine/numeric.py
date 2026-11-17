"""定点数运算。

生命、伤害、治疗、护盾在引擎内部一律用「分」（1/100）的整数表示，
这样「每一步乘除立即四舍五入到两位小数」可以用纯整数算术精确实现，
不受浮点误差影响。所有数值都是非负的。
"""

from __future__ import annotations

SCALE = 100


def cents(value: int) -> int:
    """整数属性值 → 分。"""
    return value * SCALE


def mul_pct(amount: int, pct: int) -> int:
    """amount（分）× pct%，结果四舍五入到分。例：30.00 × 95% = 28.50。"""
    return (amount * pct + 50) // 100


def div_round(amount: int, n: int) -> int:
    """amount（分）÷ n，结果四舍五入到分。例：10.00 ÷ 3 = 3.33。"""
    return (2 * amount + n) // (2 * n)


def num(amount: int) -> int | float:
    """分 → 对外输出的数值：整数保持整数，否则是至多两位小数的浮点数。"""
    return amount // SCALE if amount % SCALE == 0 else amount / SCALE


def fmt(amount: int) -> str:
    """分 → 展示文本：整数不带小数，否则固定两位小数。"""
    if amount % SCALE == 0:
        return str(amount // SCALE)
    return f"{amount // SCALE}.{amount % SCALE:02d}"
