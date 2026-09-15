#!/usr/bin/env python3
"""端到端实跑验证。

覆盖：
 1. 主数据与多币种精度（JPY=0 位、USD/CNY=2 位）
 2. 跨币种凭证过账（本位币试算平衡、按币种精度取整）
 3. 错误定位到具体分录：缺汇率 / 借贷不平 / 精度不符 / 未知账户 / 借贷同填
 4. 期末外币重估：汇兑差额自动入账、同日重估幂等
 5. 关账阻断：过账、改汇率、重估全部拒绝
 6. 反结账 + 追溯调整：未标记调整被拒、调整凭证自动成链
 7. 再次关账：快照代次、余额/调整分录/审计记录一致
 8. 余额核对：余额表重算、会计恒等式、篡改检测
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import traceback
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doubleentry import (  # noqa: E402
    ConfigurationError,
    ConsistencyError,
    Ledger,
    LineError,
    PeriodClosedError,
    ValidationError,
)
from doubleentry.money import convert, quantize  # noqa: E402

PASS = "✓"
FAIL = "✗"
results: list[tuple[bool, str]] = []


def check(cond: bool, name: str, detail: str = ""):
    results.append((bool(cond), name))
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        check.failed += 1


check.failed = 0


def section(title: str):
    print(f"\n=== {title} ===")


def expect_error(exc_types, fn, label: str, *, contains: str | None = None,
                 line: int | None = None):
    try:
        fn()
    except exc_types as exc:
        ok = True
        if contains is not None and contains not in str(exc):
            ok = False
        if line is not None and getattr(exc, "line", None) != line:
            ok = False
        extra = []
        if line is not None:
            extra.append(f"定位到分录 {getattr(exc, 'line', '?')}")
        extra.append(str(exc))
        check(ok, label, "；".join(extra))
        return exc
    check(False, label, "未抛出预期错误")
    return None


def setup(db_path: str) -> Ledger:
    db = Ledger.create(db_path)
    db.add_currency("USD", "美元", 2)
    db.add_currency("JPY", "日元", 0)          # 日元零位小数
    # 科目：借=资产/费用，贷=负债/权益/收入
    db.add_subject("1001", "库存现金", "D")
    db.add_subject("1002", "银行存款", "D")
    db.add_subject("2001", "短期借款", "C")
    db.add_subject("4001", "实收资本", "C")
    db.add_subject("5001", "主营业务成本", "D")
    db.add_subject("6001", "主营业务收入", "C")
    db.add_subject("6701", "财务费用-汇兑损益", "D")
    # 账户
    db.add_account("1001-CNY", "现金", "1001", "CNY")
    db.add_account("1002-CNY", "人民币户", "1002", "CNY")
    db.add_account("1002-USD", "美元户", "1002", "USD")
    db.add_account("1002-JPY", "日元户", "1002", "JPY")
    db.add_account("2001-USD", "美元借款", "2001", "USD")
    db.add_account("4001-CNY", "实收资本", "4001", "CNY")
    db.add_account("5001-CNY", "主营成本", "5001", "CNY")
    db.add_account("6001-CNY", "主营收入", "6001", "CNY")
    db.add_account("6701-CNY", "汇兑损益", "6701", "CNY")
    db.configure_fx("6701", "6701-CNY")
    # 汇率（1 外币 = X 本位币）
    db.set_rate("USD", "2026-01-05", "7.10")
    db.set_rate("USD", "2026-01-31", "7.00")
    db.set_rate("JPY", "2026-01-08", "0.0500")
    db.set_rate("JPY", "2026-01-31", "0.0520")
    return db


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ledger-e2e-")
    db_path = os.path.join(tmp, "books.db")
    print(f"账本文件: {db_path}")

    section("1. 初始化与币种精度")
    db = setup(db_path)
    check(quantize(Decimal("123.456"), 2) == Decimal("123.46"), "CNY 四舍五入 2 位")
    check(quantize(Decimal("123.456"), 0) == Decimal("123"), "JPY 四舍五入 0 位")
    check(convert(Decimal("10000"), Decimal("0.0500"), 2) == Decimal("500.00"),
          "JPY 10000 × 0.05 = CNY 500.00")
    check(convert(Decimal("100"), Decimal("7.10"), 2) == Decimal("710.00"),
          "USD 100 × 7.10 = CNY 710.00")

    section("2. 跨币种凭证过账（同一张凭证含 CNY/USD/JPY 三种币种）")
    # 2.1 纯人民币：投资款
    v1 = db.post_voucher(
        "2026-01-02",
        [{"account": "1002-CNY", "currency": "CNY", "debit": "1000000", "credit": "0"},
         {"account": "4001-CNY", "currency": "CNY", "debit": "0", "credit": "1000000"}],
        memo="股东投资",
    )
    check(v1["base_total"] == Decimal("1000000"), "人民币凭证过账", v1["voucher_no"])

    # 2.2 跨币种：用人民币购汇 USD，借贷按本位币平衡（含取整尾差由 CNY 行承担）
    usd_dr = convert(Decimal("10000"), Decimal("7.10"), 2)  # 71000.00
    v2 = db.post_voucher(
        "2026-01-05",
        [{"account": "1002-USD", "currency": "USD", "debit": "10000", "credit": "0"},
         {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": str(usd_dr)}],
        memo="购汇 USD",
    )
    check(v2["base_total"] == usd_dr, f"USD 购汇凭证按当日汇率折算 {usd_dr}")
    check(Decimal(v2["lines"][0]["rate"]) == Decimal("7.10"),
          "分录冻结过账时实际使用的汇率（7.10）")

    # 2.3 JPY：零精度币种 + 折算 2 位
    jpy_base = convert(Decimal("200000"), Decimal("0.0500"), 2)  # 10000
    v3 = db.post_voucher(
        "2026-01-08",
        [{"account": "1002-JPY", "currency": "JPY", "debit": "200000", "credit": "0"},
         {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": str(jpy_base)}],
        memo="购汇 JPY",
    )
    check(v3["lines"][0]["debit"] == "200000" and v3["lines"][0]["credit"] == "0",
          "JPY 金额保持整数（0 位精度）")

    # 2.4 跨币种借款（USD 借款存入 USD 户）+ 人民币零星费用
    v4 = db.post_voucher(
        "2026-01-10",
        [{"account": "1002-USD", "currency": "USD", "debit": "50000", "credit": "0"},
         {"account": "2001-USD", "currency": "USD", "debit": "0", "credit": "50000"}],
        memo="借入美元",
    )
    check(v4["base_total"] == Decimal("355000.00"), "USD 借/贷双腿折算一致（355000.00）")
    db.post_voucher(
        "2026-01-15",
        [{"account": "5001-CNY", "currency": "CNY", "debit": "20000", "credit": "0"},
         {"account": "6001-CNY", "currency": "CNY", "debit": "0", "credit": "20000"}],
        memo="结转成本(演示)",
    )

    section("3. 过账错误必须定位到具体分录")
    # 3.1 第 2 条分录缺汇率（用一个无任何汇率的日期和币种 EUR 组合：先加 EUR 币种与账户）
    db.add_currency("EUR", "欧元", 2)
    db.add_account("1002-EUR", "欧元户", "1002", "EUR")
    expect_error(
        LineError,
        lambda: db.post_voucher(
            "2026-01-12",
            [{"account": "1002-CNY", "currency": "CNY", "debit": "7100", "credit": "0"},
             {"account": "1002-EUR", "currency": "EUR", "debit": "0", "credit": "1000"}],
            memo="缺EUR汇率",
        ),
        "缺汇率：定位到第 2 条分录", contains="汇率", line=2,
    )
    # 3.2 JPY 精度不符（写了小数）→ 定位第 1 条
    expect_error(
        LineError,
        lambda: db.post_voucher(
            "2026-01-12",
            [{"account": "1002-JPY", "currency": "JPY", "debit": "100.5", "credit": "0"},
             {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": "5.03"}],
            memo="日元带小数",
        ),
        "JPY 精度不符：定位到第 1 条分录", contains="精度", line=1,
    )
    # 3.3 借贷同填 → 定位第 1 条
    expect_error(
        LineError,
        lambda: db.post_voucher(
            "2026-01-12",
            [{"account": "1002-CNY", "currency": "CNY", "debit": "100", "credit": "100"},
             {"account": "1001-CNY", "currency": "CNY", "debit": "0", "credit": "0"}],
        ),
        "借贷同填：定位到第 1 条分录", contains="借贷", line=1,
    )
    # 3.4 未知账户 → 定位第 3 条
    expect_error(
        LineError,
        lambda: db.post_voucher(
            "2026-01-12",
            [{"account": "1002-CNY", "currency": "CNY", "debit": "100", "credit": "0"},
             {"account": "1001-CNY", "currency": "CNY", "debit": "0", "credit": "50"},
             {"account": "9999-XX", "currency": "CNY", "debit": "0", "credit": "50"}],
        ),
        "未知账户：定位到第 3 条分录", contains="账户", line=3,
    )
    # 3.5 账户币种不匹配 → 第 1 条
    expect_error(
        LineError,
        lambda: db.post_voucher(
            "2026-01-12",
            [{"account": "1002-CNY", "currency": "USD", "debit": "100", "credit": "0"},
             {"account": "1002-USD", "currency": "USD", "debit": "0", "credit": "100"}],
        ),
        "账户币种不符：定位到第 1 条分录", contains="币种", line=1,
    )
    # 3.6 借贷不平（本位币差额）→ 凭证级错误并给出差额
    exc = expect_error(
        ValidationError,
        lambda: db.post_voucher(
            "2026-01-12",
            [{"account": "1002-USD", "currency": "USD", "debit": "100", "credit": "0"},
             {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": "700"}],
        ),
        "借贷不平被拒（差额 10.00 CNY）", contains="差额 10.00",
    )
    # 3.7 配置缺失：未配置汇兑损益的新账本该在重估时报 ConfigurationError
    db_bare = Ledger.create(os.path.join(tmp, "bare.db"))
    db_bare.add_subject("1002", "银行", "D")
    db_bare.add_currency("USD", "美元", 2)
    db_bare.add_account("a", "美元户", "1002", "USD")
    db_bare.set_rate("USD", "2026-01-01", "7.10")
    db_bare.set_rate("USD", "2026-01-31", "7.00")
    # 手工塞一笔余额（直接借 USD / 贷权益类账户），再重估应报配置错
    db_bare.add_subject("4001", "资本", "C")
    db_bare.add_account("b", "资本户", "4001", "CNY")
    db_bare.post_voucher("2026-01-02",
                         [{"account": "a", "currency": "USD", "debit": "100", "credit": "0"},
                          {"account": "b", "currency": "CNY", "debit": "0", "credit": "710"}])
    expect_error(ConfigurationError, lambda: db_bare.revalue("2026-01-31"),
                 "未配置汇兑损益账户时拒绝重估", contains="汇兑损益")
    db_bare.close()

    section("4. 期末外币重估（2026-01-31，USD 7.10→7.00，JPY 0.05→0.052）")
    # 重估前余额
    bal_before = {b["account"]: b for b in db.balances()}
    usd_fx = bal_before["1002-USD"]["fx_amount"]
    jpy_fx = bal_before["1002-JPY"]["fx_amount"]
    loan_fx = bal_before["2001-USD"]["fx_amount"]
    check(usd_fx == Decimal("60000.00"), f"USD 存款外币余额 {usd_fx}（10000+50000）")
    check(jpy_fx == Decimal("200000"), f"JPY 存款外币余额 {jpy_fx}")
    check(loan_fx == Decimal("-50000.00"), f"USD 借款外币余额 {loan_fx}（贷方）")

    # 缺当日汇率（EUR 账户余额为 0 不会被扫到，因此这里直接验证 USD 在 01-30 无当日汇率）
    expect_error(ValidationError,
                 lambda: db.revalue("2026-01-30", ["1002-USD"]),
                 "重估日无当日汇率被拒（不取历史最近值）", contains="当天汇率")

    rev = db.revalue("2026-01-31")
    # 预期差额：
    # 1002-USD 60000*(7.00-7.10)=-6000（损失，借费用/贷美元户）
    # 1002-JPY 200000*(0.052-0.05)=+400（收益，借日元户/贷费用）
    # 2001-USD -50000*(7.00-7.10)=+5000（借款本位币负债应减少 → 收益，借借款户/贷费用）
    by_acct = {p["account"]: p for p in rev["posted"]}
    check(set(by_acct) == {"1002-USD", "1002-JPY", "2001-USD"},
          "3 个外币账户全部生成重估凭证",
          f"实际: {sorted(by_acct)}")
    check(Decimal(by_acct["1002-USD"]["diff"]) == Decimal("-6000.00"),
          "USD 存款汇兑损失 -6000.00", by_acct["1002-USD"]["diff"])
    check(Decimal(by_acct["1002-JPY"]["diff"]) == Decimal("400.00"),
          "JPY 存款汇兑收益 +400.00", by_acct["1002-JPY"]["diff"])
    check(Decimal(by_acct["2001-USD"]["diff"]) == Decimal("5000.00"),
          "USD 借款重估收益 +5000.00（负债减少）", by_acct["2001-USD"]["diff"])
    check(all(not p["is_adjustment"] for p in rev["posted"]),
          "首次关账前的重估凭证不是调整凭证")

    # 重估后：外币余额不变，本位币余额 = 外币 × 当日汇率
    bal_after = {b["account"]: b for b in db.balances()}
    check(bal_after["1002-USD"]["fx_amount"] == usd_fx
          and bal_after["1002-JPY"]["fx_amount"] == jpy_fx
          and bal_after["2001-USD"]["fx_amount"] == loan_fx,
          "重估不改变外币余额")
    check(bal_after["1002-USD"]["base_amount"] == Decimal("420000.00"),
          "USD 存款本位币 420000.00 = 60000×7.00")
    check(bal_after["1002-JPY"]["base_amount"] == Decimal("10400.00"),
          "JPY 存款本位币 10400.00 = 200000×0.052")
    check(bal_after["2001-USD"]["base_amount"] == Decimal("-350000.00"),
          "USD 借款本位币 -350000.00 = -50000×7.00")
    check(bal_after["6701-CNY"]["base_amount"] == Decimal("600.00"),
          "汇兑损益费用净额 600.00（-6000+400+5000 中费用方向净额）",
          str(bal_after["6701-CNY"]["base_amount"]))

    # 同日重复重估：所有账户差额均为 0 → 跳过，不新增凭证
    rev2 = db.revalue("2026-01-31")
    check(rev2["posted"] == [] and len(rev2["skipped"]) == 3,
          "同一汇率日再次重估：0 张凭证、3 个账户跳过（幂等）")

    section("5. 关账阻断")
    eq = db.accounting_equation()
    check(eq["balanced"], f"关账前会计恒等式平衡（差额 {eq['difference']}）")
    closed = db.close_period("2026-01")
    check(closed["generation"] == 1 and closed["entry_count"] == 16,
          f"第 1 代快照固化 {closed['entry_count']} 条分录（5 业务凭证+3 重估凭证）",
          f"generation={closed['generation']}, entries={closed['entry_count']}")
    check(closed["debit_total"] == closed["credit_total"], "本期借贷发生额相等")

    expect_error(PeriodClosedError,
                 lambda: db.post_voucher(
                     "2026-01-20",
                     [{"account": "1002-CNY", "currency": "CNY", "debit": "100", "credit": "0"},
                      {"account": "4001-CNY", "currency": "CNY", "debit": "0", "credit": "100"}]),
                 "关账后过账被拒", line=None)
    expect_error(PeriodClosedError,
                 lambda: db.set_rate("USD", "2026-01-10", "7.20"),
                 "关账后修改该期汇率被拒")
    expect_error(PeriodClosedError,
                 lambda: db.revalue("2026-01-31"),
                 "关账后重估被拒")
    expect_error(PeriodClosedError,
                 lambda: db.close_period("2026-01"),
                 "重复关账被拒")

    # 关账期之外（2 月）仍可正常过账
    db.set_rate("USD", "2026-02-05", "7.05")
    v_feb = db.post_voucher(
        "2026-02-05",
        [{"account": "1002-USD", "currency": "USD", "debit": "1000", "credit": "0"},
         {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": "7050.00"}],
        memo="2 月正常业务",
    )
    check(v_feb["period"] == "2026-02", "下一期间（2 月）业务不受 1 月关账影响")

    section("6. 反结账与追溯调整")
    expect_error(ValidationError,
                 lambda: db.reopen_period("2026-01", "   "),
                 "反结账必须填写原因")
    db.reopen_period("2026-01", "审计调整：补记漏记手续费")

    # 反结账后补记但未标调整 → 拒绝
    expect_error(
        ValidationError,
        lambda: db.post_voucher(
            "2026-01-25",
            [{"account": "6701-CNY", "currency": "CNY", "debit": "300", "credit": "0"},
             {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": "300"}],
            memo="漏记手续费（未标调整）",
        ),
        "反结账后补记未标记调整：拒绝", contains="调整",
    )
    # 未关账过的期间不能标调整
    expect_error(
        ValidationError,
        lambda: db.post_voucher(
            "2026-02-06",
            [{"account": "1002-CNY", "currency": "CNY", "debit": "10", "credit": "0"},
             {"account": "4001-CNY", "currency": "CNY", "debit": "0", "credit": "10"}],
            is_adjustment=True,
        ),
        "从未关账的期间不能标记调整凭证", contains="调整",
    )

    # 正确做法：显式调整凭证（跨币种：补记一笔 USD 手续费）
    adj = db.post_voucher(
        "2026-01-25",
        [{"account": "6701-CNY", "currency": "CNY", "debit": "710", "credit": "0"},
         {"account": "1002-USD", "currency": "USD", "debit": "0", "credit": "100"}],
        memo="追溯调整：补记 1 月美元手续费",
        is_adjustment=True,
    )
    check(adj["is_adjustment"] and not adj["is_system"], "调整凭证已过账并打标 [adjustment]")

    # 追溯调整后再次按 1 月 31 日重估：USD 存款外币余额 60000-100=59900
    # 调整按 1/25 最近汇率 7.10 折算（base -710 → 419290），月末目标 59900×7.00=419300，补差 +10
    rev3 = db.revalue("2026-01-31")
    check(len(rev3["posted"]) == 1 and rev3["posted"][0]["account"] == "1002-USD"
          and Decimal(rev3["posted"][0]["diff"]) == Decimal("10.00"),
          "调整后补重估：仅 USD 户补差 +10.00（月末 7.00 与调整日 7.10 的汇率差）",
          str([(p["account"], p["diff"]) for p in rev3["posted"]]))
    check(rev3["posted"][0]["is_adjustment"] is True,
          "反结账期间的系统重估凭证自动标记为调整")
    # 再跑一次同日重估，应再次幂等
    check(db.revalue("2026-01-31")["posted"] == [], "调整后同日重估再次幂等")

    section("7. 再次关账：余额、调整分录、审计记录一致")
    vcount_before = len(db.list_vouchers("2026-01"))
    closed2 = db.close_period("2026-01")
    check(closed2["generation"] == 2, f"生成第 2 代快照（实际 {closed2['generation']}）")
    check(closed2["entry_count"] == 20,
          f"第 2 代快照含 {closed2['entry_count']} 条分录（16+人工调整2条+补重估2条）",
          f"entry_count={closed2['entry_count']}")

    # 最新快照与当前账面逐笔一致（余额 + 分录哈希 + 调整标记）
    match = db.verify_latest_snapshot_matches_books("2026-01")
    check(match["matched"] and match["generation"] == 2,
          "第 2 代快照余额/分录/调整标记与账面完全一致")

    # 快照链、审计链、分录哈希、余额表、会计恒等式全量核对
    stat = db.verify_all()
    check(True, f"全量一致性核对通过：{stat['vouchers']} 凭证 / {stat['entries']} 分录 / "
          f"{stat['audit']} 审计记录 / {stat['snapshots']} 快照")
    check(stat["equation"]["balanced"], "会计恒等式持续成立")

    # 关账后再次被阻断（且调整入口也被阻断）
    expect_error(PeriodClosedError,
                 lambda: db.post_voucher(
                     "2026-01-28",
                     [{"account": "6701-CNY", "currency": "CNY", "debit": "1", "credit": "0"},
                      {"account": "1002-CNY", "currency": "CNY", "debit": "0", "credit": "1"}],
                     is_adjustment=True),
                 "再次关账后，调整凭证同样被阻断")

    # 1 月凭证清单：人工调整凭证与系统调整凭证均可识别
    jan = db.list_vouchers("2026-01")
    check(len(jan) == vcount_before, "凭证数量与关账前一致（无静默改写）")
    adj_vouchers = [v for v in jan if v["is_adjustment"]]
    check(len(adj_vouchers) == 2,
          f"1 月共有 2 张调整凭证（1 人工 + 1 系统重估），实际 {len(adj_vouchers)}",
          str([(v["voucher_no"], v["is_system"]) for v in adj_vouchers]))

    # 审计记录可追溯：关账/反结账/调整过账事件齐备且成链
    trail = db.audit_trail(200)
    actions = {r["action"] for r in trail}
    check({"voucher.post", "fx.revalue", "period.close", "period.reopen"} <= actions,
          "审计链包含过账/重估/关账/反结账全部事件")
    reopen_rec = next(r for r in trail if r["action"] == "period.reopen")
    check("审计调整" in reopen_rec["detail_json"], "反结账原因留痕在审计链中")

    section("8. 余额核对与篡改检测")
    db.verify_balances()
    check(True, "余额表逐账户与分录重算一致（外币 + 本位币）")
    tb = db.trial_balance("2026-01")
    check(tb["balanced"], f"1 月试算平衡（借 {tb['total_debit']} = 贷 {tb['total_credit']}）")

    # 篡改 1：直接改数据库里的余额表 → verify 必须报错
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE ledger_balance SET base_amount='999999' WHERE account='1002-CNY'")
    raw.commit()
    raw.close()
    expect_error(ConsistencyError, db.verify_balances,
                 "直接篡改余额表被核对发现", contains="1002-CNY")
    # 还原余额表（从分录重算重建）
    db2 = Ledger(db_path)
    recomputed = db2.recompute_balances()
    raw = sqlite3.connect(db_path)
    for acc, v in recomputed.items():
        raw.execute("UPDATE ledger_balance SET fx_amount=?, base_amount=? WHERE account=?",
                    (str(v["fx"]), str(v["base"]), acc))
    raw.commit()
    raw.close()
    db2.verify_balances()

    # 篡改 2：删除一条审计记录 → 审计链断裂
    raw = sqlite3.connect(db_path)
    raw.execute("DELETE FROM audit_log WHERE id=(SELECT MIN(id) FROM audit_log)")
    raw.commit()
    raw.close()
    expect_error(ConsistencyError, db2.verify_audit_chain,
                 "删除审计记录导致哈希链断裂被发现", contains="断")

    # 篡改 3：篡改分录金额 → 分录哈希不符
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE entry SET debit='1' WHERE id=1")
    raw.commit()
    raw.close()
    expect_error(ConsistencyError, db2.verify_entry_hashes,
                 "篡改分录金额被行哈希发现", contains="哈希")
    db2.close()

    # 汇总
    print("\n" + "=" * 56)
    total = len(results)
    failed = sum(1 for ok, _ in results if not ok)
    print(f"验证完成：{total - failed}/{total} 通过，{failed} 失败")
    if failed:
        print("\n失败项：")
        for ok, name in results:
            if not ok:
                print(f"  {FAIL} {name}")
        return 1
    print("全部场景实跑通过：跨币种 / 同日重估 / 关账阻断 / 反结账 / 追溯调整 / 余额核对")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)
