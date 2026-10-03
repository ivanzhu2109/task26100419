# 施工企业工资扣税更正库

纯 Python + SQLite 实现的工资扣税更正账本（2024 年规则），由上层代码直接调用，无接口层。

## 功能

- **发薪入账** `post_payroll(worker_id, pay_date, gross)`：同一工人按支付日期递增入账，每工人每支付日期一笔原始发薪。
- **毛薪更正** `correct_gross(worker_id, pay_date, new_gross)`：作用于该支付位置当前有效版本，自动冲销受影响支票并生成替代支票，重排该工人后续支票的社保（6.2%，年度封顶 168,600.00）、普通 Medicare（1.45%）与附加 Medicare（超 200,000.00 部分 0.9%）归属；其他工人不受影响。相同毛薪的重复更正为无操作。
- **账本查询**：
  - `current_checks(worker_id=None)` 当前有效支票集；
  - `history(worker_id=None)` 完整不可变历史（原始 / 冲销 / 替代）；
  - `effective_before(correction_id)` / `effective_after(correction_id)` 还原任一次更正前后的有效支票集。

## 使用

```python
from payroll_ledger import PayrollLedger

with PayrollLedger("ledger.db") as led:
    led.post_payroll("W001", "2024-01-05", "25000.00")
    led.post_payroll("W001", "2024-01-19", "25000.00")

    result = led.correct_gross("W001", "2024-01-05", "20000.00")
    print(result.correction_id, result.changed_paycheck_ids)

    for check in led.current_checks("W001"):
        print(check.pay_date, check.amounts)

    before = led.effective_before(result.correction_id, "W001")
```

金额可传 `str` 或 `Decimal`（美元），返回值为 `Decimal`，精确到美分；扣税按累计目标四舍五入规则计算（累计目标分别保留两位后取差），不做逐笔独立舍入。

## 设计要点

- 账目只增不改：原始、冲销、替代条目均不可变；冲销金额是被冲销版本的精确相反数；冲销与替代通过 `correction_id` 与 `supersedes_entry_id` 追溯更正来源与被替代版本。
- 一次更正的全部写入在单个 SQLite 事务中完成，失败整体回滚。
- 金额在库中以整数美分存储，关闭重开后当前结果与历史完全一致。

## 测试

```bash
python3 -m pytest tests -q
```

覆盖：跨社保封顶与附加 Medicare 门槛后调减早期毛薪的扣税重分配、连续更正 / 改为 0 / 改回原金额、美分级累计舍入差异、多工人隔离、更正原子性、持久化重开一致性、50 工人 × 53 笔发薪与 2000 次更正规模，以及每次更正后与独立重算结果逐项一致和全账本正负金额守恒。
