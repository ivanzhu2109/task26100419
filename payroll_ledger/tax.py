"""累计扣税目标计算（2024 年规则）。

所有金额在模块内部以整数美分表示，对外接口使用 Decimal（美元）。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

SS_RATE = Decimal("0.062")
SS_WAGE_CAP = Decimal("168600.00")
MEDICARE_RATE = Decimal("0.0145")
ADDL_MEDICARE_RATE = Decimal("0.009")
ADDL_MEDICARE_THRESHOLD = Decimal("200000.00")


def to_cents(amount: Decimal | str | int) -> int:
    """把美元金额转换为整数美分，按四舍五入保留两位。"""
    value = Decimal(amount) if not isinstance(amount, Decimal) else amount
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


def to_dollars(cents: int) -> Decimal:
    """把整数美分转换为 Decimal 美元。"""
    return (Decimal(cents) / 100).quantize(CENT)


def cumulative_targets(cum_gross_cents: int) -> tuple[int, int, int]:
    """给定有效累计毛薪（美分），返回三项累计扣税目标（美分）。

    顺序为（社保, 普通 Medicare, 附加 Medicare），各目标分别四舍五入到分。
    """
    cum = to_dollars(cum_gross_cents)
    ss_base = min(cum, SS_WAGE_CAP)
    ss = (ss_base * SS_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
    medicare = (cum * MEDICARE_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
    excess = max(Decimal("0.00"), cum - ADDL_MEDICARE_THRESHOLD)
    addl = (excess * ADDL_MEDICARE_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
    return to_cents(ss), to_cents(medicare), to_cents(addl)
