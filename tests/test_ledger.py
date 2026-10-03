import os
import tempfile
import unittest
from decimal import Decimal

from payroll_ledger import Ledger, LedgerError, UnknownPaycheckError
from payroll_ledger.ledger import (
    ENTRY_ORIGINAL,
    ENTRY_REPLACEMENT,
    ENTRY_REVERSAL,
)
from payroll_ledger.tax import (
    addl_medicare_target_cents,
    medicare_target_cents,
    ss_target_cents,
)


def recompute(grosses):
    """Independent recomputation: per-check amounts from grosses in date order."""
    out = []
    cumulative = 0
    prev_ss = prev_med = prev_addl = 0
    for gross in grosses:
        cumulative += gross
        ss = ss_target_cents(cumulative) - prev_ss
        med = medicare_target_cents(cumulative) - prev_med
        addl = addl_medicare_target_cents(cumulative) - prev_addl
        prev_ss, prev_med, prev_addl = (
            ss_target_cents(cumulative),
            medicare_target_cents(cumulative),
            addl_medicare_target_cents(cumulative),
        )
        out.append((gross, ss, med, addl, gross - ss - med - addl))
    return out


def amounts_of(entry):
    return (
        entry.gross,
        entry.social_security,
        entry.medicare,
        entry.additional_medicare,
        entry.net,
    )


def assert_matches_independent(testcase, ledger, worker_id, grosses):
    checks = ledger.current_checks(worker_id)
    testcase.assertEqual(len(checks), len(grosses))
    for check, expected in zip(checks, recompute(grosses)):
        testcase.assertEqual(amounts_of(check), expected)


def assert_conservation(testcase, ledger, worker_id):
    totals = [0, 0, 0, 0, 0]
    for e in ledger.history(worker_id):
        for i, v in enumerate(amounts_of(e)):
            totals[i] += v
    current = [0, 0, 0, 0, 0]
    for e in ledger.current_checks(worker_id):
        for i, v in enumerate(amounts_of(e)):
            current[i] += v
    testcase.assertEqual(totals, current)


class ThresholdCorrectionTest(unittest.TestCase):
    """Reduce an early gross after crossing both tax thresholds."""

    def setUp(self):
        self.ledger = Ledger(":memory:")
        # Cumulative: 60k, 120k, 180k (SS capped mid-check), 240k (addl Medicare)
        for i, date in enumerate(
            ["2024-01-05", "2024-02-05", "2024-03-05", "2024-04-05"]
        ):
            self.ledger.record_paycheck("w1", date, "60000.00")

    def tearDown(self):
        self.ledger.close()

    def test_thresholds_crossed_before_correction(self):
        checks = self.ledger.current_checks("w1")
        # Check 3: SS capped at 168600 -> only 48600 of the 60000 is SS-taxable
        self.assertEqual(checks[2].social_security, 4860000 * 62 // 1000)
        # Check 4: additional Medicare on 40000 excess
        self.assertEqual(checks[3].additional_medicare, 36000)

    def test_reduce_early_gross_reallocates_downstream(self):
        cid = self.ledger.correct_paycheck("w1", "2024-01-05", "10000.00")
        self.assertIsNotNone(cid)
        # New cumulative: 10k, 70k, 130k, 190k -> no SS cap, no addl Medicare
        assert_matches_independent(self, self.ledger, "w1",
                                   [1000000, 6000000, 6000000, 6000000])
        checks = self.ledger.current_checks("w1")
        self.assertEqual(checks[3].additional_medicare, 0)
        self.assertEqual(checks[2].social_security, 6000000 * 62 // 1000)
        # Checks 1, 3, 4 changed (check 2's deltas are identical), so exactly
        # those get a reversal + replacement
        history = self.ledger.history("w1")
        reversals = [e for e in history if e.entry_type == ENTRY_REVERSAL]
        replacements = [e for e in history if e.entry_type == ENTRY_REPLACEMENT]
        self.assertEqual(len(reversals), 3)
        self.assertEqual(len(replacements), 3)
        changed_dates = {r.pay_date for r in reversals}
        self.assertEqual(
            changed_dates, {"2024-01-05", "2024-03-05", "2024-04-05"}
        )
        for rev in reversals:
            self.assertEqual(rev.correction_id, cid)
            original = next(
                e for e in history if e.entry_id == rev.reverses_entry_id
            )
            self.assertEqual(amounts_of(rev), tuple(-v for v in amounts_of(original)))
        assert_conservation(self, self.ledger, "w1")


class ConsecutiveCorrectionTest(unittest.TestCase):
    """Consecutive corrections, zero gross, and reverting to the original amount."""

    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.ledger.record_paycheck("w1", "2024-01-05", "5000.00")
        self.ledger.record_paycheck("w1", "2024-01-19", "5000.00")

    def tearDown(self):
        self.ledger.close()

    def test_consecutive_zero_and_revert(self):
        c1 = self.ledger.correct_paycheck("w1", "2024-01-05", "4000.00")
        assert_matches_independent(self, self.ledger, "w1", [400000, 500000])
        c2 = self.ledger.correct_paycheck("w1", "2024-01-05", "0.00")
        assert_matches_independent(self, self.ledger, "w1", [0, 500000])
        # Position retained with gross 0
        checks = self.ledger.current_checks("w1")
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0].gross, 0)
        c3 = self.ledger.correct_paycheck("w1", "2024-01-05", "5000.00")
        assert_matches_independent(self, self.ledger, "w1", [500000, 500000])

        history = self.ledger.history("w1")
        # Reverting created new history; nothing was resurrected
        versions = [
            (e.pay_date, e.version)
            for e in history
            if e.entry_type in (ENTRY_ORIGINAL, ENTRY_REPLACEMENT)
        ]
        self.assertEqual(
            versions,
            [("2024-01-05", 1), ("2024-01-19", 1)]
            + [("2024-01-05", 2)]
            + [("2024-01-05", 3)]
            + [("2024-01-05", 4)],
        )
        # No version reversed twice
        reversed_ids = [
            e.reverses_entry_id for e in history if e.entry_type == ENTRY_REVERSAL
        ]
        self.assertEqual(len(reversed_ids), len(set(reversed_ids)))
        # Every reversal/replacement traces to its correction
        for e in history:
            if e.entry_type in (ENTRY_REVERSAL, ENTRY_REPLACEMENT):
                self.assertIn(e.correction_id, (c1, c2, c3))
        # Only the corrected check changed each time (downstream amounts were
        # identical), so exactly one reversal per correction
        reversals = [e for e in history if e.entry_type == ENTRY_REVERSAL]
        self.assertEqual(len(reversals), 3)
        assert_conservation(self, self.ledger, "w1")

    def test_same_gross_correction_is_noop(self):
        before = len(self.ledger.history())
        result = self.ledger.correct_paycheck("w1", "2024-01-05", "5000.00")
        self.assertIsNone(result)
        self.assertEqual(len(self.ledger.history()), before)
        self.assertEqual(self.ledger.corrections(), [])


class RoundingTest(unittest.TestCase):
    """Cent-level grosses must use cumulative rounding, not per-check rounding."""

    def test_cumulative_rounding(self):
        ledger = Ledger(":memory:")
        # 0.01 * 0.0145 rounds to 0.00 per check; cumulative 0.03 -> 0.00,
        # but cumulative targets must drive the deltas.
        ledger.record_paycheck("w1", "2024-01-05", "0.01")
        ledger.record_paycheck("w1", "2024-01-19", "0.01")
        ledger.record_paycheck("w1", "2024-02-02", "0.01")
        checks = ledger.current_checks("w1")
        # Medicare targets: round(0.01*1.45%)=0, round(0.02*1.45%)=0,
        # round(0.03*1.45%)=0 -> all zero; SS: round(0.01*6.2%)=0 etc.
        self.assertEqual([c.medicare for c in checks], [0, 0, 0])
        self.assertEqual([c.social_security for c in checks], [0, 0, 0])
        # A gross where cumulative rounding differs from per-check multiplication:
        # 33.33 * 0.062 = 2.06646 -> 2.07 per check if naive; cumulative:
        # C=3333 -> 206.646 -> 207; C=6666 -> 413.292 -> 413 (delta 206).
        ledger.record_paycheck("w2", "2024-01-05", "33.33")
        ledger.record_paycheck("w2", "2024-01-19", "33.33")
        w2 = ledger.current_checks("w2")
        self.assertEqual(w2[0].social_security, 207)
        self.assertEqual(w2[1].social_security, 206)
        self.assertEqual(
            amounts_of(w2[0]), recompute([3333, 3333])[0]
        )
        ledger.close()

    def test_cent_level_correction_rounding(self):
        ledger = Ledger(":memory:")
        ledger.record_paycheck("w1", "2024-01-05", "1234.56")
        ledger.record_paycheck("w1", "2024-01-19", "1234.56")
        ledger.correct_paycheck("w1", "2024-01-05", "1234.57")
        assert_matches_independent(self, ledger, "w1", [123457, 123456])
        assert_conservation(self, ledger, "w1")
        ledger.close()


class MultiWorkerTest(unittest.TestCase):
    def test_isolation(self):
        ledger = Ledger(":memory:")
        ledger.record_paycheck("w1", "2024-01-05", "90000.00")
        ledger.record_paycheck("w1", "2024-02-05", "90000.00")
        ledger.record_paycheck("w2", "2024-01-05", "90000.00")
        ledger.record_paycheck("w2", "2024-02-05", "90000.00")
        ledger.correct_paycheck("w1", "2024-01-05", "10000.00")
        # w2 untouched: no new entries, amounts unchanged
        w2_history = ledger.history("w2")
        self.assertEqual(len(w2_history), 2)
        assert_matches_independent(self, ledger, "w2", [9000000, 9000000])
        assert_matches_independent(self, ledger, "w1", [1000000, 9000000])
        assert_conservation(self, ledger, "w1")
        assert_conservation(self, ledger, "w2")
        ledger.close()


class HistoryReplayTest(unittest.TestCase):
    def test_before_after_each_correction(self):
        ledger = Ledger(":memory:")
        dates = ["2024-01-05", "2024-02-05", "2024-03-05"]
        grosses = [1000000, 2000000, 3000000]  # cents
        for d, g in zip(dates, ["10000.00", "20000.00", "30000.00"]):
            ledger.record_paycheck("w1", d, g)
        snapshots = [list(grosses)]
        cids = []
        for new_gross in (500000, 0, 1000000):
            cids.append(
                ledger.correct_paycheck("w1", "2024-02-05", f"{new_gross / 100:.2f}")
            )
            grosses = [1000000, new_gross, 3000000]
            snapshots.append(list(grosses))
        for i, cid in enumerate(cids):
            before = ledger.checks_before_correction(cid, "w1")
            after = ledger.checks_after_correction(cid, "w1")
            self.assertEqual(
                [amounts_of(c) for c in before], recompute(snapshots[i])
            )
            self.assertEqual(
                [amounts_of(c) for c in after], recompute(snapshots[i + 1])
            )
        ledger.close()


class PersistenceTest(unittest.TestCase):
    def test_reopen_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            ledger = Ledger(path)
            ledger.record_paycheck("w1", "2024-01-05", "70000.00")
            ledger.record_paycheck("w1", "2024-02-05", "70000.00")
            ledger.correct_paycheck("w1", "2024-01-05", "65000.00")
            before_history = ledger.history()
            before_current = ledger.current_checks()
            ledger.close()

            reopened = Ledger(path)
            self.assertEqual(reopened.history(), before_history)
            self.assertEqual(reopened.current_checks(), before_current)
            # Corrections still apply to the current version after reopen
            reopened.correct_paycheck("w1", "2024-01-05", "60000.00")
            assert_matches_independent(
                self, reopened, "w1", [6000000, 7000000]
            )
            assert_conservation(self, reopened, "w1")
            reopened.close()


class ValidationTest(unittest.TestCase):
    def test_errors(self):
        ledger = Ledger(":memory:")
        ledger.record_paycheck("w1", "2024-01-05", "100.00")
        with self.assertRaises(LedgerError):
            ledger.record_paycheck("w1", "2024-01-05", "100.00")  # duplicate date
        with self.assertRaises(LedgerError):
            ledger.record_paycheck("w1", "2024-01-01", "100.00")  # out of order
        with self.assertRaises(LedgerError):
            ledger.record_paycheck("w1", "2024-02-01", "-1.00")
        with self.assertRaises(UnknownPaycheckError):
            ledger.correct_paycheck("w1", "2024-03-01", "50.00")
        with self.assertRaises(UnknownPaycheckError):
            ledger.correct_paycheck("nobody", "2024-01-05", "50.00")
        with self.assertRaises(LedgerError):
            ledger.record_paycheck("w1", "not-a-date", "1.00")
        ledger.close()

    def test_decimal_input(self):
        ledger = Ledger(":memory:")
        ledger.record_paycheck("w1", "2024-01-05", Decimal("2500.75"))
        self.assertEqual(ledger.current_checks("w1")[0].gross, 250075)
        ledger.close()


class ScaleTest(unittest.TestCase):
    """50 workers x 53 paychecks, 2000 corrections across the ledger."""

    def test_scale(self):
        import random

        rng = random.Random(2024)
        ledger = Ledger(":memory:")
        workers = [f"worker-{i:02d}" for i in range(50)]
        import datetime

        first = datetime.date(2024, 1, 5)
        dates = [
            (first + datetime.timedelta(weeks=i)).isoformat() for i in range(53)
        ]
        current = {}
        for w in workers:
            current[w] = []
            for d in dates:
                gross = rng.randrange(100_00, 9000_00)
                current[w].append(gross)
                ledger.record_paycheck(w, d, f"{gross / 100:.2f}")
        for _ in range(2000):
            w = rng.choice(workers)
            idx = rng.randrange(53)
            new_gross = rng.randrange(0, 9000_00)
            if new_gross == current[w][idx]:
                continue
            current[w][idx] = new_gross
            ledger.correct_paycheck(w, dates[idx], f"{new_gross / 100:.2f}")
        for w in workers:
            assert_matches_independent(self, ledger, w, current[w])
            assert_conservation(self, ledger, w)
        ledger.close()


if __name__ == "__main__":
    unittest.main()
