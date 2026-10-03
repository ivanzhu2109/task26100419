"""账本行为测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from payroll_ledger import PayrollLedger
from payroll_ledger.tax import cumulative_targets, to_cents, to_dollars


def recompute(grosses: list[str]) -> list[tuple[int, int, int, int, int]]:
    """仅取当前毛薪、按支付日期从头独立核算的参照结果（美分）。"""
    out = []
    prev_cum = 0
    for g in grosses:
        gross = to_cents(g)
        prev = cumulative_targets(prev_cum)
        cur = cumulative_targets(prev_cum + gross)
        taxes = tuple(c - p for c, p in zip(cur, prev))
        out.append((gross, *taxes, gross - sum(taxes)))
        prev_cum += gross
    return out


def effective_tuples(led: PayrollLedger, worker_id: str):
    return [
        (
            to_cents(c.amounts.gross),
            to_cents(c.amounts.social_security),
            to_cents(c.amounts.medicare),
            to_cents(c.amounts.additional_medicare),
            to_cents(c.amounts.net),
        )
        for c in led.current_checks(worker_id)
    ]


def assert_conservation(led: PayrollLedger, worker_id: str) -> None:
    """全部条目正负汇总必须等于当前有效支票汇总。"""
    totals = [0] * 5
    for e in led.history(worker_id):
        for i, v in enumerate(
            (
                e.amounts.gross,
                e.amounts.social_security,
                e.amounts.medicare,
                e.amounts.additional_medicare,
                e.amounts.net,
            )
        ):
            totals[i] += to_cents(v)
    eff = [0] * 5
    for t in effective_tuples(led, worker_id):
        for i, v in enumerate(t):
            eff[i] += v
    assert totals == eff


@pytest.fixture()
def led():
    with PayrollLedger(":memory:") as ledger:
        yield ledger


def test_cross_both_thresholds_reduce_early_gross(led: PayrollLedger) -> None:
    # 10 笔 25000：第 7 笔跨过社保封顶 168600，第 8 笔跨过附加 Medicare 门槛 200000。
    dates = [f"2024-01-{5 + i:02d}" for i in range(10)]
    for d in dates:
        led.post_payroll("W1", d, "25000.00")
    before = effective_tuples(led, "W1")
    assert before == recompute(["25000.00"] * 10)
    # 社保在第 7 笔只按部分毛薪计：累计 175000，封顶部分 168600-150000=18600。
    assert before[6][1] == to_cents("1153.20")  # 18600 * 6.2%
    assert before[7][1] == 0  # 第 8 笔起社保为 0
    assert before[7][3] == 0  # 第 8 笔累计恰为 200000，未超门槛
    assert before[8][3] == to_cents("225.00")  # 第 9 笔：25000 * 0.9%

    # 调减第 1 笔为 10000：整体累计下移，后续支票扣税归属重新分配。
    led.correct_gross("W1", dates[0], "10000.00")
    after = effective_tuples(led, "W1")
    assert after == recompute(["10000.00"] + ["25000.00"] * 9)
    # 现在第 8 笔才触及社保封顶（8600 计入），第 9 笔才产生附加 Medicare。
    assert after[7][1] == to_cents("533.20")  # 8600 * 6.2%
    assert after[7][3] == 0
    assert after[8][1] == 0
    assert after[8][3] == to_cents("90.00")  # 10000 * 0.9%
    assert_conservation(led, "W1")


def test_consecutive_zero_and_restore(led: PayrollLedger) -> None:
    dates = ["2024-02-02", "2024-02-16", "2024-03-01"]
    for d in dates:
        led.post_payroll("W1", d, "5000.00")
    history_len = len(led.history("W1"))

    r1 = led.correct_gross("W1", dates[1], "6000.00")
    r2 = led.correct_gross("W1", dates[1], "0.00")
    r3 = led.correct_gross("W1", dates[1], "5000.00")  # 改回最初金额
    assert effective_tuples(led, "W1") == recompute(["5000.00"] * 3)

    hist = led.history("W1")
    # 每次更正都形成新历史，已冲销条目不复活。
    assert len(hist) == history_len + 3 * 2  # 整数金额下每次更正只影响被更正支票本身
    # 改回最初金额后，被更正支票的当前有效条目是新生成的替代条目，而非复活旧版本。
    entry_by_id = {e.entry_id: e for e in hist}
    for c in led.current_checks("W1"):
        if c.pay_date == dates[1]:
            assert entry_by_id[c.entry_id].kind == "replacement"
    # 每个被冲销版本只被冲销一次。
    reversals = [e for e in hist if e.kind == "reversal"]
    assert len(reversals) == len({e.supersedes_entry_id for e in reversals})
    # 冲销金额是被冲销版本的精确相反数。
    by_id = {e.entry_id: e for e in hist}
    for rev in reversals:
        src = by_id[rev.supersedes_entry_id]
        for a, b in zip(
            (rev.amounts.gross, rev.amounts.social_security, rev.amounts.medicare,
             rev.amounts.additional_medicare, rev.amounts.net),
            (src.amounts.gross, src.amounts.social_security, src.amounts.medicare,
             src.amounts.additional_medicare, src.amounts.net),
        ):
            assert a == -b
    # 冲销与替代均可追溯到导致变化的更正。
    for e in hist:
        if e.kind in ("reversal", "replacement"):
            assert e.correction_id in (r1.correction_id, r2.correction_id, r3.correction_id)
    # 可还原每次更正前后的有效支票集。
    assert [to_cents(c.amounts.gross) for c in led.effective_before(r2.correction_id, "W1")] == [
        to_cents("5000.00"), to_cents("6000.00"), to_cents("5000.00")]
    assert [to_cents(c.amounts.gross) for c in led.effective_after(r2.correction_id, "W1")] == [
        to_cents("5000.00"), to_cents("0.00"), to_cents("5000.00")]
    assert_conservation(led, "W1")


def test_zero_gross_keeps_position(led: PayrollLedger) -> None:
    led.post_payroll("W1", "2024-01-05", "1000.00")
    led.post_payroll("W1", "2024-01-19", "1000.00")
    led.correct_gross("W1", "2024-01-05", "0.00")
    checks = led.current_checks("W1")
    assert len(checks) == 2  # 支付位置保留
    assert checks[0].amounts.gross == Decimal("0.00")
    assert effective_tuples(led, "W1") == recompute(["0.00", "1000.00"])
    assert_conservation(led, "W1")


def test_repeat_same_gross_is_noop(led: PayrollLedger) -> None:
    led.post_payroll("W1", "2024-01-05", "1000.00")
    res = led.correct_gross("W1", "2024-01-05", "1000.00")
    assert res.correction_id is None
    assert len(led.history("W1")) == 1


def test_cent_level_cumulative_rounding(led: PayrollLedger) -> None:
    grosses = ["3333.33"] * 6
    for i, g in enumerate(grosses):
        led.post_payroll("W1", f"2024-01-{5 + i:02d}", g)
    assert effective_tuples(led, "W1") == recompute(grosses)
    # 美分级更正触发后续支票的累计舍入差异。
    led.correct_gross("W1", "2024-01-05", "3333.34")
    grosses[0] = "3333.34"
    assert effective_tuples(led, "W1") == recompute(grosses)
    assert_conservation(led, "W1")


def test_multi_worker_isolation(led: PayrollLedger) -> None:
    for w in ("W1", "W2"):
        for i in range(3):
            led.post_payroll(w, f"2024-01-{5 + i:02d}", "2000.00")
    led.correct_gross("W1", "2024-01-05", "1500.00")
    assert effective_tuples(led, "W1") == recompute(["1500.00", "2000.00", "2000.00"])
    assert effective_tuples(led, "W2") == recompute(["2000.00"] * 3)
    assert len(led.history("W2")) == 3  # W2 无任何更正条目
    assert_conservation(led, "W1")
    assert_conservation(led, "W2")


def test_persistence_across_reopen(tmp_path) -> None:
    db = str(tmp_path / "ledger.db")
    with PayrollLedger(db) as led:
        led.post_payroll("W1", "2024-01-05", "25000.00")
        led.post_payroll("W1", "2024-01-19", "25000.00")
        led.correct_gross("W1", "2024-01-05", "20000.00")
        expected_current = effective_tuples(led, "W1")
        expected_history = len(led.history("W1"))
    with PayrollLedger(db) as led2:
        assert effective_tuples(led2, "W1") == expected_current
        assert len(led2.history("W1")) == expected_history
        assert_conservation(led2, "W1")


def test_scale_50_workers_53_checks() -> None:
    with PayrollLedger(":memory:") as led:
        for w in range(50):
            wid = f"W{w:02d}"
            for i in range(53):
                led.post_payroll(wid, f"2024-01-01T{i:02d}", "1000.00")
        assert len(led.current_checks()) == 50 * 53
        assert effective_tuples(led, "W00") == recompute(["1000.00"] * 53)


def test_scale_2000_corrections() -> None:
    with PayrollLedger(":memory:") as led:
        led.post_payroll("W1", "2024-01-05", "1000.00")
        led.post_payroll("W1", "2024-01-19", "1000.00")
        for i in range(2000):
            led.correct_gross("W1", "2024-01-05", f"{900 + (i % 200)}.{i % 100:02d}")
        assert len(led.history("W1")) == 2 + 2000 * 2
        assert_conservation(led, "W1")


def test_correction_is_atomic(led: PayrollLedger) -> None:
    led.post_payroll("W1", "2024-01-05", "1000.00")
    led.post_payroll("W1", "2024-01-19", "1000.00")
    # 人为让替代条目插入失败，验证整次更正回滚、不留部分结果。
    led._conn.execute(
        "CREATE TRIGGER fail_replacement BEFORE INSERT ON entries"
        " WHEN NEW.kind = 'replacement'"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(Exception, match="boom"):
        led.correct_gross("W1", "2024-01-05", "500.00")
    led._conn.execute("DROP TRIGGER fail_replacement")
    assert len(led.history("W1")) == 2  # 无冲销、无替代、无更正残留
    assert led._conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    assert effective_tuples(led, "W1") == recompute(["1000.00", "1000.00"])


def test_correct_unknown_paycheck_raises(led: PayrollLedger) -> None:
    led.post_payroll("W1", "2024-01-05", "1000.00")
    with pytest.raises(ValueError):
        led.correct_gross("W1", "2024-02-02", "500.00")
    with pytest.raises(ValueError):
        led.correct_gross("W1", "2024-01-05", "-1.00")
    assert len(led.history("W1")) == 1
