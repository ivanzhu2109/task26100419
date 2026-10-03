# payroll-ledger：施工企业工资扣税更正库（2024 税年）

纯 Python + SQLite 的库，由上层直接调用，无接口层、无服务进程。提供三类操作：
发薪入账、毛薪更正、账本查询。所有账目不可变地存入 SQLite 文件。

## 安装与运行

无第三方依赖，Python 3.10+：

```bash
python3 -m unittest discover -s tests -v   # 运行测试
```

## 快速上手

```python
from payroll_ledger import Ledger

ledger = Ledger("payroll.db")          # 或 ":memory:"；重复打开同一文件即恢复账本

# 1) 发薪入账：同一工人按支付日期递增入账，每个支付日期一笔原始发薪
ledger.record_paycheck("emp-001", "2024-01-05", "60000.00")
ledger.record_paycheck("emp-001", "2024-02-05", "60000.00")

# 2) 毛薪更正：作用于该支付日期当前有效版本，支付日期不变；
#    自动冲销所有金额变化的后续有效支票并生成替代支票（同事务，整体成功或失败）。
#    返回 correction_id；毛薪未变化时返回 None 且不产生任何条目。
cid = ledger.correct_paycheck("emp-001", "2024-01-05", "55000.00")

# 3) 查询
ledger.current_checks("emp-001")        # 当前有效支票（省略 worker_id 查全部工人）
ledger.history("emp-001")               # 完整历史：原始 + 冲销 + 替代，按入账顺序
ledger.corrections()                    # 全部更正事件
ledger.checks_before_correction(cid)    # 还原某次更正前的有效支票集
ledger.checks_after_correction(cid)     # 还原某次更正后的有效支票集
ledger.effective_checks_at(seq)         # 按全局入账序号还原任一时点

ledger.close()
```

金额参数接受 `int` / `str` / `Decimal` 的美元值（精确到美分）；返回的
`LedgerEntry` 中所有金额字段（`gross`、`social_security`、`medicare`、
`additional_medicare`、`net`）均为整数美分。

## 核算规则

- 设某工人截至某笔支票的有效累计毛薪为 C，三个累计目标分别四舍五入到美分：
  - 社保：`min(C, 168600.00) × 6.2%`
  - 普通 Medicare：`C × 1.45%`
  - 附加 Medicare：`max(0, C − 200000.00) × 0.9%`
- 每张支票的扣税 = 本笔与前一笔累计目标之差（首笔前值为 0）；净薪 = 毛薪 − 三项扣税。
- 更正后从被更正支票起重算该工人全部后续支票；金额未变的支票不产生条目。
- 已入账条目永不修改或删除：变化的支票生成精确相反数的冲销条目和新版本替代条目，
  二者均记录 `correction_id`；冲销条目记录 `reverses_entry_id` 指向被冲销版本。

## 数据模型

单文件 SQLite，两张表：

- `corrections`：每次更正事件（工人、支付日期、新毛薪）。
- `entries`：全部账目条目。`entry_type` 为 `original` / `reversal` / `replacement`；
  `version` 标识同一支付位置的版本链；`reversed_by_entry_id IS NULL` 的
  `original`/`replacement` 即当前有效支票。`entry_id` 自增，兼作全局时序。

## 项目结构

- `payroll_ledger/tax.py` — 2024 年累计扣税目标（社保封顶、附加 Medicare 门槛）
- `payroll_ledger/ledger.py` — `Ledger` 类：入账、更正、查询、SQLite 持久化
- `tests/test_ledger.py` — 自动化测试（门槛跨越、连续更正、舍入、多工人隔离、
  历史还原、持久化重开、50 工人 × 53 笔 × 2000 次更正规模测试）
