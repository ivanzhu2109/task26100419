"""工资扣税更正库主入口：发薪入账、毛薪更正、账本查询。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from payroll_ledger.tax import cumulative_targets, to_cents, to_dollars

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paychecks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    pay_date TEXT NOT NULL,
    seq INTEGER NOT NULL,
    UNIQUE (worker_id, pay_date)
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paycheck_id INTEGER NOT NULL REFERENCES paychecks (id),
    new_gross_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paycheck_id INTEGER NOT NULL REFERENCES paychecks (id),
    kind TEXT NOT NULL CHECK (kind IN ('original', 'reversal', 'replacement')),
    correction_id INTEGER REFERENCES corrections (id),
    supersedes_entry_id INTEGER REFERENCES entries (id),
    gross_cents INTEGER NOT NULL,
    ss_cents INTEGER NOT NULL,
    medicare_cents INTEGER NOT NULL,
    addl_medicare_cents INTEGER NOT NULL,
    net_cents INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paychecks_worker ON paychecks (worker_id);
CREATE INDEX IF NOT EXISTS idx_entries_paycheck ON entries (paycheck_id);
CREATE INDEX IF NOT EXISTS idx_entries_supersedes ON entries (supersedes_entry_id);
"""

_EFFECTIVE_SQL = """
SELECT e.id, p.id, p.worker_id, p.pay_date,
       e.gross_cents, e.ss_cents, e.medicare_cents, e.addl_medicare_cents, e.net_cents
FROM entries e
JOIN paychecks p ON p.id = e.paycheck_id
WHERE e.kind IN ('original', 'replacement')
  AND NOT EXISTS (
      SELECT 1 FROM entries r
      WHERE r.kind = 'reversal' AND r.supersedes_entry_id = e.id
  )
"""


@dataclass(frozen=True)
class CheckAmounts:
    """一张有效支票的金额（美元）。"""

    gross: Decimal
    social_security: Decimal
    medicare: Decimal
    additional_medicare: Decimal
    net: Decimal


@dataclass(frozen=True)
class EffectiveCheck:
    """某一时刻某支付位置上的有效支票。"""

    paycheck_id: int
    worker_id: str
    pay_date: str
    entry_id: int
    amounts: CheckAmounts


@dataclass(frozen=True)
class LedgerEntry:
    """账本中的一条不可变条目（原始 / 冲销 / 替代）。"""

    entry_id: int
    paycheck_id: int
    worker_id: str
    pay_date: str
    kind: str  # 'original' | 'reversal' | 'replacement'
    correction_id: int | None
    supersedes_entry_id: int | None
    amounts: CheckAmounts


@dataclass(frozen=True)
class CorrectionResult:
    """一次更正的结果；无变化时 correction_id 为 None。"""

    correction_id: int | None
    changed_paycheck_ids: tuple[int, ...]


class PayrollLedger:
    """基于 SQLite 的工资扣税更正账本（单进程顺序调用）。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PayrollLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def post_payroll(self, worker_id: str, pay_date: str, gross: Decimal | str) -> EffectiveCheck:
        """发薪入账：同一工人的支付日期必须递增。"""
        gross_cents = to_cents(gross)
        if gross_cents < 0:
            raise ValueError("毛薪必须为非负")
        row = self._conn.execute(
            "SELECT MAX(pay_date), COUNT(*) FROM paychecks WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        if row[0] is not None and pay_date <= row[0]:
            raise ValueError("同一工人的发薪必须按支付日期递增入账")
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO paychecks (worker_id, pay_date, seq) VALUES (?, ?, ?)",
                (worker_id, pay_date, row[1] + 1),
            )
            paycheck_id = cur.lastrowid
            prev_cum = self._effective_cum_gross_cents(worker_id)
            amounts = self._compute_amounts(prev_cum, gross_cents)
            cur = self._conn.execute(
                "INSERT INTO entries (paycheck_id, kind, correction_id, supersedes_entry_id,"
                " gross_cents, ss_cents, medicare_cents, addl_medicare_cents, net_cents)"
                " VALUES (?, 'original', NULL, NULL, ?, ?, ?, ?, ?)",
                (paycheck_id, *amounts),
            )
        return EffectiveCheck(
            paycheck_id=paycheck_id,
            worker_id=worker_id,
            pay_date=pay_date,
            entry_id=cur.lastrowid,
            amounts=self._to_amounts(amounts),
        )

    def correct_gross(
        self, worker_id: str, pay_date: str, new_gross: Decimal | str
    ) -> CorrectionResult:
        """更正指定原始发薪的毛薪，重排该工人后续支票的扣税。"""
        new_gross_cents = to_cents(new_gross)
        if new_gross_cents < 0:
            raise ValueError("毛薪必须为非负")
        rows = self._conn.execute(
            _EFFECTIVE_SQL + " AND p.worker_id = ? ORDER BY p.seq",
            (worker_id,),
        ).fetchall()
        target = None
        for r in rows:
            if r[3] == pay_date:
                target = r
                break
        if target is None:
            raise ValueError(f"找不到工人 {worker_id} 在 {pay_date} 的原始发薪")

        changes: list[tuple[tuple, tuple[int, int, int, int, int]]] = []
        prev_cum = 0
        for r in rows:
            gross = new_gross_cents if r is target else r[4]
            amounts = self._compute_amounts(prev_cum, gross)
            if amounts != (r[4], r[5], r[6], r[7], r[8]):
                changes.append((r, amounts))
            prev_cum += gross
        if not changes:
            return CorrectionResult(correction_id=None, changed_paycheck_ids=())

        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO corrections (paycheck_id, new_gross_cents) VALUES (?, ?)",
                (target[1], new_gross_cents),
            )
            correction_id = cur.lastrowid
            for r, amounts in changes:
                self._conn.execute(
                    "INSERT INTO entries (paycheck_id, kind, correction_id, supersedes_entry_id,"
                    " gross_cents, ss_cents, medicare_cents, addl_medicare_cents, net_cents)"
                    " VALUES (?, 'reversal', ?, ?, ?, ?, ?, ?, ?)",
                    (r[1], correction_id, r[0], -r[4], -r[5], -r[6], -r[7], -r[8]),
                )
                self._conn.execute(
                    "INSERT INTO entries (paycheck_id, kind, correction_id, supersedes_entry_id,"
                    " gross_cents, ss_cents, medicare_cents, addl_medicare_cents, net_cents)"
                    " VALUES (?, 'replacement', ?, ?, ?, ?, ?, ?, ?)",
                    (r[1], correction_id, r[0], *amounts),
                )
        return CorrectionResult(
            correction_id=correction_id,
            changed_paycheck_ids=tuple(r[1] for r, _ in changes),
        )

    def current_checks(self, worker_id: str | None = None) -> list[EffectiveCheck]:
        """当前有效支票集。"""
        sql = _EFFECTIVE_SQL
        params: tuple = ()
        if worker_id is not None:
            sql += " AND p.worker_id = ?"
            params = (worker_id,)
        sql += " ORDER BY p.worker_id, p.seq"
        return [self._to_effective(r) for r in self._conn.execute(sql, params)]

    def history(self, worker_id: str | None = None) -> list[LedgerEntry]:
        """完整账本历史（原始 / 冲销 / 替代全部条目）。"""
        sql = (
            "SELECT e.id, p.id, p.worker_id, p.pay_date, e.kind, e.correction_id,"
            " e.supersedes_entry_id, e.gross_cents, e.ss_cents, e.medicare_cents,"
            " e.addl_medicare_cents, e.net_cents"
            " FROM entries e JOIN paychecks p ON p.id = e.paycheck_id"
        )
        params: tuple = ()
        if worker_id is not None:
            sql += " WHERE p.worker_id = ?"
            params = (worker_id,)
        sql += " ORDER BY e.id"
        return [
            LedgerEntry(
                entry_id=r[0],
                paycheck_id=r[1],
                worker_id=r[2],
                pay_date=r[3],
                kind=r[4],
                correction_id=r[5],
                supersedes_entry_id=r[6],
                amounts=self._to_amounts((r[7], r[8], r[9], r[10], r[11])),
            )
            for r in self._conn.execute(sql, params)
        ]

    def effective_before(
        self, correction_id: int, worker_id: str | None = None
    ) -> list[EffectiveCheck]:
        """还原某次更正之前的有效支票集。"""
        boundary = self._correction_boundary(correction_id)[0]
        return self._effective_as_of(boundary - 1, worker_id)

    def effective_after(
        self, correction_id: int, worker_id: str | None = None
    ) -> list[EffectiveCheck]:
        """还原某次更正之后的有效支票集。"""
        boundary = self._correction_boundary(correction_id)[1]
        return self._effective_as_of(boundary, worker_id)

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def _correction_boundary(self, correction_id: int) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT MIN(id), MAX(id) FROM entries WHERE correction_id = ?",
            (correction_id,),
        ).fetchone()
        if row[0] is None:
            raise ValueError(f"更正 {correction_id} 不存在或未产生条目")
        return row[0], row[1]

    def _effective_as_of(
        self, max_entry_id: int, worker_id: str | None
    ) -> list[EffectiveCheck]:
        sql = (
            _EFFECTIVE_SQL.replace(
                "r.kind = 'reversal' AND r.supersedes_entry_id = e.id",
                "r.kind = 'reversal' AND r.supersedes_entry_id = e.id AND r.id <= ?",
            )
            + " AND e.id <= ?"
        )
        params: list = [max_entry_id, max_entry_id]
        if worker_id is not None:
            sql += " AND p.worker_id = ?"
            params.append(worker_id)
        sql += " ORDER BY p.worker_id, p.seq"
        return [self._to_effective(r) for r in self._conn.execute(sql, params)]

    def _effective_cum_gross_cents(self, worker_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(e.gross_cents), 0) FROM entries e"
            " JOIN paychecks p ON p.id = e.paycheck_id"
            " WHERE p.worker_id = ? AND e.kind IN ('original', 'replacement')"
            " AND NOT EXISTS (SELECT 1 FROM entries r"
            "                 WHERE r.kind = 'reversal' AND r.supersedes_entry_id = e.id)",
            (worker_id,),
        ).fetchone()
        return row[0]

    @staticmethod
    def _compute_amounts(prev_cum_cents: int, gross_cents: int) -> tuple[int, int, int, int, int]:
        prev_ss, prev_med, prev_addl = cumulative_targets(prev_cum_cents)
        ss, med, addl = cumulative_targets(prev_cum_cents + gross_cents)
        ss_d, med_d, addl_d = ss - prev_ss, med - prev_med, addl - prev_addl
        net = gross_cents - ss_d - med_d - addl_d
        return gross_cents, ss_d, med_d, addl_d, net

    @staticmethod
    def _to_amounts(cents: tuple[int, int, int, int, int]) -> CheckAmounts:
        return CheckAmounts(
            gross=to_dollars(cents[0]),
            social_security=to_dollars(cents[1]),
            medicare=to_dollars(cents[2]),
            additional_medicare=to_dollars(cents[3]),
            net=to_dollars(cents[4]),
        )

    def _to_effective(self, row: tuple) -> EffectiveCheck:
        return EffectiveCheck(
            paycheck_id=row[1],
            worker_id=row[2],
            pay_date=row[3],
            entry_id=row[0],
            amounts=self._to_amounts((row[4], row[5], row[6], row[7], row[8])),
        )
