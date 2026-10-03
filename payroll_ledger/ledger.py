"""SQLite-backed payroll tax-correction ledger (2024 tax year)."""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union

from .tax import (
    addl_medicare_target_cents,
    medicare_target_cents,
    ss_target_cents,
)

MoneyLike = Union[int, str, Decimal]

ENTRY_ORIGINAL = "original"
ENTRY_REVERSAL = "reversal"
ENTRY_REPLACEMENT = "replacement"


@dataclass(frozen=True)
class CheckAmounts:
    """Amounts of one effective check version, in integer cents."""

    gross: int
    social_security: int
    medicare: int
    additional_medicare: int
    net: int


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable ledger entry."""

    entry_id: int
    worker_id: str
    pay_date: str
    entry_type: str
    version: int
    gross: int
    social_security: int
    medicare: int
    additional_medicare: int
    net: int
    correction_id: Optional[int]
    reverses_entry_id: Optional[int]
    reversed_by_entry_id: Optional[int]


class LedgerError(Exception):
    pass


class UnknownPaycheckError(LedgerError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    pay_date TEXT NOT NULL,
    new_gross INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    pay_date TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('original', 'reversal', 'replacement')),
    version INTEGER NOT NULL,
    gross INTEGER NOT NULL,
    social_security INTEGER NOT NULL,
    medicare INTEGER NOT NULL,
    additional_medicare INTEGER NOT NULL,
    net INTEGER NOT NULL,
    correction_id INTEGER REFERENCES corrections(correction_id),
    reverses_entry_id INTEGER REFERENCES entries(entry_id),
    reversed_by_entry_id INTEGER REFERENCES entries(entry_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_worker ON entries(worker_id, pay_date);
"""


def to_cents(amount: MoneyLike) -> int:
    """Convert a dollar amount (int/str/Decimal) to integer cents."""
    cents = Decimal(str(amount)) * 100
    return int(cents.quantize(Decimal("1")))


def _compute_amounts(gross_list: list[int]) -> list[CheckAmounts]:
    """Recompute per-check amounts from grosses in pay-date order."""
    out: list[CheckAmounts] = []
    cumulative = 0
    prev_ss = prev_med = prev_addl = 0
    for gross in gross_list:
        cumulative += gross
        ss = ss_target_cents(cumulative) - prev_ss
        med = medicare_target_cents(cumulative) - prev_med
        addl = addl_medicare_target_cents(cumulative) - prev_addl
        prev_ss += ss
        prev_med += med
        prev_addl += addl
        out.append(
            CheckAmounts(
                gross=gross,
                social_security=ss,
                medicare=med,
                additional_medicare=addl,
                net=gross - ss - med - addl,
            )
        )
    return out


class Ledger:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record_paycheck(self, worker_id: str, pay_date: str, gross: MoneyLike) -> int:
        """Record an original paycheck; returns the new entry id."""
        _validate_worker(worker_id)
        _validate_pay_date(pay_date)
        gross_cents = to_cents(gross)
        if gross_cents < 0:
            raise LedgerError("gross must be non-negative")
        cur = self._conn.execute(
            "SELECT MAX(pay_date) AS max_date FROM entries "
            "WHERE worker_id = ? AND entry_type = ?",
            (worker_id, ENTRY_ORIGINAL),
        )
        max_date = cur.fetchone()["max_date"]
        if max_date is not None and pay_date <= max_date:
            raise LedgerError(
                f"pay_date {pay_date} must be after worker's latest original {max_date}"
            )
        effective = self._effective_entries(worker_id)
        grosses = [e.gross for e in effective] + [gross_cents]
        amounts = _compute_amounts(grosses)[-1]
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO entries (worker_id, pay_date, entry_type, version, gross,"
                " social_security, medicare, additional_medicare, net)"
                " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    worker_id,
                    pay_date,
                    ENTRY_ORIGINAL,
                    amounts.gross,
                    amounts.social_security,
                    amounts.medicare,
                    amounts.additional_medicare,
                    amounts.net,
                ),
            )
        return int(cur.lastrowid)

    def correct_paycheck(self, worker_id: str, pay_date: str, new_gross: MoneyLike) -> Optional[int]:
        """Correct the gross of an original paycheck; returns correction id or None if no-op."""
        _validate_worker(worker_id)
        _validate_pay_date(pay_date)
        new_gross_cents = to_cents(new_gross)
        if new_gross_cents < 0:
            raise LedgerError("gross must be non-negative")
        effective = self._effective_entries(worker_id)
        target = next((e for e in effective if e.pay_date == pay_date), None)
        if target is None:
            raise UnknownPaycheckError(
                f"no effective paycheck for worker {worker_id!r} on {pay_date}"
            )
        if target.gross == new_gross_cents:
            return None
        grosses = [e.gross for e in effective]
        grosses[effective.index(target)] = new_gross_cents
        amounts = _compute_amounts(grosses)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO corrections (worker_id, pay_date, new_gross) VALUES (?, ?, ?)",
                (worker_id, pay_date, new_gross_cents),
            )
            correction_id = int(cur.lastrowid)
            for entry, new_amounts in zip(effective, amounts):
                old = CheckAmounts(
                    gross=entry.gross,
                    social_security=entry.social_security,
                    medicare=entry.medicare,
                    additional_medicare=entry.additional_medicare,
                    net=entry.net,
                )
                if old == new_amounts:
                    continue
                cur = self._conn.execute(
                    "INSERT INTO entries (worker_id, pay_date, entry_type, version, gross,"
                    " social_security, medicare, additional_medicare, net,"
                    " correction_id, reverses_entry_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.worker_id,
                        entry.pay_date,
                        ENTRY_REVERSAL,
                        entry.version,
                        -entry.gross,
                        -entry.social_security,
                        -entry.medicare,
                        -entry.additional_medicare,
                        -entry.net,
                        correction_id,
                        entry.entry_id,
                    ),
                )
                reversal_id = int(cur.lastrowid)
                self._conn.execute(
                    "UPDATE entries SET reversed_by_entry_id = ? WHERE entry_id = ?",
                    (reversal_id, entry.entry_id),
                )
                self._conn.execute(
                    "INSERT INTO entries (worker_id, pay_date, entry_type, version, gross,"
                    " social_security, medicare, additional_medicare, net, correction_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.worker_id,
                        entry.pay_date,
                        ENTRY_REPLACEMENT,
                        entry.version + 1,
                        new_amounts.gross,
                        new_amounts.social_security,
                        new_amounts.medicare,
                        new_amounts.additional_medicare,
                        new_amounts.net,
                        correction_id,
                    ),
                )
        return correction_id

    def current_checks(self, worker_id: Optional[str] = None) -> list[LedgerEntry]:
        if worker_id is None:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE entry_type IN (?, ?)"
                " AND reversed_by_entry_id IS NULL ORDER BY worker_id, pay_date",
                (ENTRY_ORIGINAL, ENTRY_REPLACEMENT),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE worker_id = ? AND entry_type IN (?, ?)"
                " AND reversed_by_entry_id IS NULL ORDER BY pay_date",
                (worker_id, ENTRY_ORIGINAL, ENTRY_REPLACEMENT),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def history(self, worker_id: Optional[str] = None) -> list[LedgerEntry]:
        if worker_id is None:
            rows = self._conn.execute(
                "SELECT * FROM entries ORDER BY entry_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE worker_id = ? ORDER BY entry_id",
                (worker_id,),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def corrections(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM corrections ORDER BY correction_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def effective_checks_at(self, seq: int, worker_id: Optional[str] = None) -> list[LedgerEntry]:
        """Reconstruct the effective check set as of ledger sequence ``seq``."""
        sql = (
            "SELECT * FROM entries WHERE entry_id <= ? AND entry_type IN (?, ?)"
            " AND (reversed_by_entry_id IS NULL OR reversed_by_entry_id > ?)"
        )
        params: list[object] = [seq, ENTRY_ORIGINAL, ENTRY_REPLACEMENT, seq]
        if worker_id is not None:
            sql += " AND worker_id = ?"
            params.append(worker_id)
        sql += " ORDER BY worker_id, pay_date"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def checks_before_correction(
        self, correction_id: int, worker_id: Optional[str] = None
    ) -> list[LedgerEntry]:
        first = self._conn.execute(
            "SELECT MIN(entry_id) AS lo FROM entries WHERE correction_id = ?",
            (correction_id,),
        ).fetchone()["lo"]
        if first is None:
            raise LedgerError(f"unknown correction_id {correction_id}")
        return self.effective_checks_at(first - 1, worker_id)

    def checks_after_correction(
        self, correction_id: int, worker_id: Optional[str] = None
    ) -> list[LedgerEntry]:
        last = self._conn.execute(
            "SELECT MAX(entry_id) AS hi FROM entries WHERE correction_id = ?",
            (correction_id,),
        ).fetchone()["hi"]
        if last is None:
            raise LedgerError(f"unknown correction_id {correction_id}")
        return self.effective_checks_at(last, worker_id)

    def _effective_entries(self, worker_id: str) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT * FROM entries WHERE worker_id = ? AND entry_type IN (?, ?)"
            " AND reversed_by_entry_id IS NULL ORDER BY pay_date",
            (worker_id, ENTRY_ORIGINAL, ENTRY_REPLACEMENT),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]


def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        entry_id=row["entry_id"],
        worker_id=row["worker_id"],
        pay_date=row["pay_date"],
        entry_type=row["entry_type"],
        version=row["version"],
        gross=row["gross"],
        social_security=row["social_security"],
        medicare=row["medicare"],
        additional_medicare=row["additional_medicare"],
        net=row["net"],
        correction_id=row["correction_id"],
        reverses_entry_id=row["reverses_entry_id"],
        reversed_by_entry_id=row["reversed_by_entry_id"],
    )


def _validate_worker(worker_id: str) -> None:
    if not worker_id or not isinstance(worker_id, str):
        raise LedgerError("worker_id must be a non-empty string")


def _validate_pay_date(pay_date: str) -> None:
    try:
        _dt.date.fromisoformat(pay_date)
    except (TypeError, ValueError):
        raise LedgerError(f"pay_date must be ISO format YYYY-MM-DD, got {pay_date!r}")
