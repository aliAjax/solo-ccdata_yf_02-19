# 离线复式记账与期末关账台

纯标准库（`sqlite3` + `decimal.Decimal`）实现的离线多币种复式记账系统：
维护科目、币种、汇率、账户和含多条借贷分录的凭证；缺汇率、借贷不平一律拒绝过账
并把错误定位到具体分录；期末按指定汇率日重估外币余额、汇兑差额自动入账；
关账后全链路只读，反结账后的补记强制标记为调整，再次关账时余额、调整分录与
审计记录保持一致。

无第三方依赖，Python 3.10+ 即可运行。账本是单个 SQLite 文件，可离线拷贝/备份。

## 目录结构

```
ledger/
├── doubleentry/
│   ├── money.py      # Decimal 金额、按币种精度 ROUND_HALF_UP、外币折算
│   ├── errors.py     # 业务错误（LineError 携带分录序号）
│   ├── ledger.py     # 核心引擎：主数据/过账/重估/关账/反结账/核对/审计链
│   ├── cli.py        # 命令行
│   ├── __main__.py   # python -m doubleentry 入口
│   └── __init__.py
├── verify.py         # 端到端实跑验证（61 项断言）
└── README.md
```

## 快速开始

```bash
cd ledger
python3 -m doubleentry.cli --db books.db init

# 主数据：币种（带精度）、科目（D=借方科目 资产/费用，C=贷方科目 负债/权益/收入）、账户
python3 -m doubleentry.cli --db books.db currency add USD 美元 2
python3 -m doubleentry.cli --db books.db currency add JPY 日元 0      # 日元零位小数
python3 -m doubleentry.cli --db books.db subject add 1002 银行存款 D
python3 -m doubleentry.cli --db books.db account add 1002-USD 美元户 1002 USD

# 汇率：1 单位外币兑换多少本位币；过账取"截至凭证日最近"的汇率
python3 -m doubleentry.cli --db books.db rate set USD 2026-01-05 7.10

# 过账：--line 账户:币种:借方:贷方（可重复，任意多条）
python3 -m doubleentry.cli --db books.db voucher post 2026-01-05 \
    --line 1002-USD:USD:10000:0 --line 1002-CNY:CNY:0:71000.00 --memo 购汇

# 期末重估：只认重估日当天汇率；同日可重复执行（幂等）
python3 -m doubleentry.cli --db books.db revalue 2026-01-31

# 关账 / 反结账（必须给原因）
python3 -m doubleentry.cli --db books.db period close 2026-01
python3 -m doubleentry.cli --db books.db period reopen 2026-01 --reason "审计补记"

# 反结账后补记必须显式 --adjustment；调整后可再按月末汇率补重估、再关账
python3 -m doubleentry.cli --db books.db voucher post 2026-01-25 \
    --line 6701-CNY:CNY:300:0 --line 1002-CNY:CNY:0:300 --adjustment --memo 补手续费
python3 -m doubleentry.cli --db books.db revalue 2026-01-31
python3 -m doubleentry.cli --db books.db period close 2026-01

# 报表与核对
python3 -m doubleentry.cli --db books.db balances
python3 -m doubleentry.cli --db books.db trial 2026-01
python3 -m doubleentry.cli --db books.db verify --snapshot-period 2026-01
python3 -m doubleentry.cli --db books.db audit
```

`--db` 也可换成环境变量 `LEDGER_DB`。作为库使用时：

```python
from doubleentry import Ledger
from doubleentry.money import Decimal

db = Ledger.create("books.db")          # 或 Ledger("books.db")
db.post_voucher("2026-01-05", [
    {"account": "1002-USD", "currency": "USD", "debit": "10000", "credit": "0"},
    {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": "71000.00"},
], memo="购汇")
```

## 数据模型与业务规则

| 表 | 说明 |
|---|---|
| `currency` | 币种 + 精度（CNY/USD=2，JPY=0，可 0..18） |
| `subject` | 科目，方向 `D`（资产/费用）或 `C`（负债/权益/收入） |
| `account` | 账户挂科目、绑定唯一币种 |
| `rate` | `(币种, 日期)` 唯一的汇率；过账取截至日最近值，重估只认当日值 |
| `voucher` / `entry` | 凭证 + 多行分录；分录冻结过账时的原币金额、本位币金额与实际汇率 |
| `ledger_balance` | 账户当前余额（原币净额 + 本位币净额，借正贷负），可随时从分录重算 |
| `period` | 期间状态 `open/closed`，另记 `ever_closed`（曾关账 → 补记必须标调整） |
| `period_snapshot` | 每次关账一代快照：余额全量 + 本期分录哈希列表 + 哈希前链 |
| `audit_log` | 所有写操作留痕，按前一条记录 SHA-256 成链 |

关键规则：

1. **金额精度**：全程 `Decimal`，拒绝 `float`；按币种精度 `ROUND_HALF_UP`。
   日元写 `100.5` 直接以 `LineError` 拒在对应分录；外币折算后按本位币精度取整。
2. **过账校验**（按分录顺序逐行检查，先错先报）：
   账户/币种存在、账户币种一致、金额非负且借贷恰有一方为正、金额符合币种精度、
   外币行必须有截至凭证日的可用汇率——任一不过都报 `分录 N: …`（退出码 2）。
3. **借贷平衡按本位币**：跨币种凭证也必须 `Σ借方本位币 = Σ贷方本位币`。
   不平衡时抛 `UnbalancedVoucherError`（退出码 2），错误信息给出双方合计、差额，
   并**逐条列出对应分录**的账户、币种、所用汇率与本位币借贷额，例如：
   `分录 1（1002-USD/USD，汇率 7.1）：借 710.00 / 贷 0.00；分录 2（1002-CNY/CNY）：借 0.00 / 贷 700.00`。
   尾差由用户在本位币腿承担，系统不静默调账。
4. **期末重估（整批原子）**：
   - 两阶段执行：先只读聚合 `凭证日期 <= 重估日` 的余额（未来期间业务不串入），
     逐个账户校验**当天**汇率、计算差额、构建并校验全部系统凭证；
     任一账户缺当日汇率或任一凭证不平衡，整批直接中止且**零写入**；
     预检通过后，全部系统凭证、余额变动和一条批次审计记录在**同一个 SQLite 事务**
     内提交，中途任何异常整批回滚——不会留下半批系统凭证或半条审计记录；
   - 因为预检不消耗凭证号、同日重估本身幂等，**修复汇率后重试的结果与一次成功完全一致**
     （凭证号、账户、差额、余额、审计记录都相同）；
   - 差额 = 原币净额×当日汇率 − 当前本位币净额，自动生成平衡系统凭证
     （外币账户出本位币调整腿，对方腿进配置好的汇兑损益本位币账户）；
   - 原币余额不变；差额为 0 的账户跳过，全批无差额时不产生任何凭证和审计记录。
5. **关账**：关账前强制通过余额重算、会计恒等式（资产+费用=负债+权益+收入）、
   审计链校验，然后固化一代快照（代次递增；快照之间哈希成链，链头锚点存于
   `meta.snapshot_head`）。关账后：该期凭证过账、重估、该期汇率修改全部拒绝；
   下期业务不受影响。
6. **反结账与追溯调整**：反结账必须填写原因并留痕，期间回到开放但保留
   `ever_closed`；此后该期补记**必须**带调整标记（人工凭证），重估产生的系统凭证
   会自动带上调整标记；从未关账的期间不允许标调整。再次关账生成新一代快照，
   `verify --snapshot-period` 逐账户、逐分录（含调整标记）核对快照与当前账面完全一致。
7. **防篡改**：每条分录有内容哈希；审计日志、快照各自成 SHA-256 链，且两条链的
   链头锚点（`meta.audit_head` / `meta.snapshot_head`）记录"最后一条"的哈希——
   因此不仅能发现中间断链，**删除最后一条审计记录或最后一代快照**同样会被
   `verify` 定位报错；直接改 SQLite 里的余额、分录也会被对应校验发现。

## 验证结果

`python3 verify.py` 在临时账本上实跑全部业务场景，最近一次运行 **82/82 通过**，覆盖：

- 跨币种凭证（CNY/USD/JPY 同账）、JPY 零位精度、按精度取整与本位币平衡；
- 错误定位到分录：缺汇率（分录 2）、JPY 精度（分录 1）、借贷同填、未知账户（分录 3）、
  账户币种不符；借贷不平给出差额并逐条列出每条分录的本位币借贷金额（分录 1、2）；
- 期末重估：美元存款损失 −6000、日元收益 +400、美元借款（负债减少）收益 +5000
  自动入账，外币余额不变；同一汇率日重复重估 0 凭证（幂等）；重估日缺当天汇率被拒；
- **批量原子性**（独立三账本对照）：EUR 缺当日汇率时整批中止，无系统凭证、无审计记录、
  无余额改写、凭证号不被消耗；补齐汇率重试与"一次成功"账本的凭证号/账户/差额/余额逐项相同；
  注入落库中途异常时第一张已写凭证随事务回滚，重试结果仍与一次成功一致；
- 关账阻断过账 / 改汇率 / 重估 / 重复关账；下期业务正常；
- 反结账需原因；不标调整的补记、给未关账期间标调整都被拒；
  人工调整凭证与调整后系统重估凭证均带调整标记；
- 再次关账生成第 2 代快照，快照余额/分录/调整标记与账面逐笔一致；
- 余额表与分录重算一致、试算平衡、会计恒等式成立；
  篡改余额表、删最旧审计记录（前链断裂）、**删最后一条审计记录（链头锚点）**、
  **删最后一代快照（快照锚点）**、改分录金额五类篡改均被对应校验发现。
