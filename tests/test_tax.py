"""累计扣税目标计算测试。"""

from decimal import Decimal

from payroll_ledger.tax import cumulative_targets, to_cents, to_dollars


def test_to_cents_half_up() -> None:
    assert to_cents("10.005") == 1001
    assert to_cents("10.004") == 1000
    assert to_cents(Decimal("0.01")) == 1
    assert to_cents("12345.67") == 1234567


def test_to_dollars_roundtrip() -> None:
    assert to_dollars(1234567) == Decimal("12345.67")
    assert to_dollars(0) == Decimal("0.00")


def test_targets_below_all_thresholds() -> None:
    ss, med, addl = cumulative_targets(to_cents("1000.00"))
    assert ss == to_cents("62.00")
    assert med == to_cents("14.50")
    assert addl == 0


def test_ss_cap() -> None:
    ss, _, _ = cumulative_targets(to_cents("200000.00"))
    assert ss == to_cents("10453.20")  # 168600 * 6.2%


def test_additional_medicare_threshold() -> None:
    _, _, addl_below = cumulative_targets(to_cents("200000.00"))
    assert addl_below == 0
    _, _, addl_above = cumulative_targets(to_cents("210000.00"))
    assert addl_above == to_cents("90.00")  # 10000 * 0.9%


def test_cumulative_rounding_not_per_check() -> None:
    # 两笔 3333.33：累计 6666.66 的 Medicare 目标为 96.67，
    # 第二笔应为 96.67 - 48.33 = 48.34，而非单笔舍入的 48.33。
    t1 = cumulative_targets(to_cents("3333.33"))
    t2 = cumulative_targets(to_cents("6666.66"))
    assert t1[1] == to_cents("48.33")
    assert t2[1] - t1[1] == to_cents("48.34")
