"""Cumulative payroll tax targets for 2024 (all amounts in integer cents)."""

from decimal import Decimal, ROUND_HALF_UP

SS_WAGE_BASE_CENTS: int = 16_860_000  # $168,600.00
SS_RATE = Decimal("0.062")
MEDICARE_RATE = Decimal("0.0145")
ADDL_MEDICARE_THRESHOLD_CENTS: int = 20_000_000  # $200,000.00
ADDL_MEDICARE_RATE = Decimal("0.009")


def _round_cent(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ss_target_cents(cumulative_gross_cents: int) -> int:
    base = min(cumulative_gross_cents, SS_WAGE_BASE_CENTS)
    return _round_cent(Decimal(base) * SS_RATE)


def medicare_target_cents(cumulative_gross_cents: int) -> int:
    return _round_cent(Decimal(cumulative_gross_cents) * MEDICARE_RATE)


def addl_medicare_target_cents(cumulative_gross_cents: int) -> int:
    excess = max(0, cumulative_gross_cents - ADDL_MEDICARE_THRESHOLD_CENTS)
    return _round_cent(Decimal(excess) * ADDL_MEDICARE_RATE)
