#!/usr/bin/env python3
"""离线复式记账与期末关账台 —— 命令行入口。

数据库通过 --db 或环境变量 LEDGER_DB 指定。凭证分录用可重复的 --line 描述：

    --line 账户:币种:借方:贷方     仅借：1002:CNY:1000:0   仅贷：2001:USD:0:500

示例：
  python -m doubleentry.cli init --db books.db
  python -m doubleentry.cli voucher post 2026-01-10 \
      --line 1002:CNY:7100:0 --line 5001:CNY:0:7100 --memo "购料"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

from .errors import LedgerError, LineError
from .ledger import Ledger
from .money import D


def _db_path(args) -> str:
    path = getattr(args, "db", None) or os.environ.get("LEDGER_DB")
    if not path:
        raise SystemExit("请用 --db 或环境变量 LEDGER_DB 指定账本数据库路径")
    return path


def _open(args) -> Ledger:
    return Ledger(_db_path(args))


def _money(v: Decimal) -> str:
    return format(v, "f")


# ---------- 输出 ----------

def print_table(headers: list[str], rows: list[list[str]]):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            pad = widths[i] - (_display_width(cell) - len(cell))
            cells.append(cell + " " * pad)
        print("  ".join(cells))


def _display_width(s: str) -> int:
    width = 0
    for ch in s:
        width += 2 if ord(ch) > 0x2E80 else 1
    return width


# ---------- 子命令实现 ----------

def cmd_init(args):
    db = Ledger.create(_db_path(args), base_currency=args.base_ccy,
                       base_precision=args.precision)
    db.close()
    print(f"已创建账本 {args.db}，本位币 {args.base_ccy}（{args.precision} 位小数）")


def cmd_currency(args):
    with _open(args) as db:
        if args.action == "add":
            db.add_currency(args.code, args.name, args.precision)
            print(f"币种已添加: {args.code}（{args.name}，精度 {args.precision}）")
        elif args.action == "list":
            rows = db.conn.execute(
                "SELECT c.code code, c.name name, c.precision prec, "
                "CASE WHEN c.code=? THEN '*' ELSE '' END base FROM currency c ORDER BY c.code",
                (db.base_currency,),
            ).fetchall()
            print_table(["代码", "名称", "精度", "本位币"],
                        [[r["code"], r["name"], str(r["prec"]), r["base"]] for r in rows])


def cmd_subject(args):
    with _open(args) as db:
        if args.action == "add":
            db.add_subject(args.code, args.name, args.side)
            side_name = "借方(资产/费用)" if args.side == "D" else "贷方(负债/权益/收入)"
            print(f"科目已添加: {args.code} {args.name} [{side_name}]")
        elif args.action == "list":
            rows = db.conn.execute("SELECT * FROM subject ORDER BY code").fetchall()
            print_table(["代码", "名称", "方向"],
                        [[r["code"], r["name"],
                          "借" if r["side"] == "D" else "贷"] for r in rows])


def cmd_account(args):
    with _open(args) as db:
        if args.action == "add":
            db.add_account(args.code, args.name, args.subject, args.currency)
            print(f"账户已添加: {args.code} {args.name}（科目 {args.subject}，币种 {args.currency}）")
        elif args.action == "list":
            rows = db.conn.execute(
                "SELECT a.code code, a.name name, a.subject_code subj, s.side side, "
                "a.currency ccy FROM account a JOIN subject s ON s.code=a.subject_code "
                "ORDER BY a.code"
            ).fetchall()
            print_table(["账户", "名称", "科目", "方向", "币种"],
                        [[r["code"], r["name"], r["subj"],
                          "借" if r["side"] == "D" else "贷", r["ccy"]] for r in rows])


def cmd_rate(args):
    with _open(args) as db:
        if args.action == "set":
            db.set_rate(args.currency, args.date, args.value)
            print(f"汇率已登记: {args.date} 1 {args.currency} = {args.value} {db.base_currency}")
        elif args.action == "list":
            rows = db.conn.execute(
                "SELECT * FROM rate ORDER BY currency, rate_date"
            ).fetchall()
            print_table(["币种", "汇率日", "汇率(→本位币)"],
                        [[r["currency"], r["rate_date"], r["rate"]] for r in rows])


def cmd_fx_config(args):
    with _open(args) as db:
        db.configure_fx(args.subject, args.account)
        print(f"汇兑损益配置完成：科目 {args.subject}，本位币账户 {args.account}")


def _parse_line(spec: str, line_no: int) -> dict:
    parts = spec.split(":")
    if len(parts) != 4:
        raise SystemExit(
            f"分录 {line_no} 格式错误：{spec!r}，应为 账户:币种:借方:贷方"
        )
    account, currency, debit, credit = parts
    return {"account": account.strip(), "currency": currency.strip().upper(),
            "debit": debit.strip() or "0", "credit": credit.strip() or "0"}


def cmd_voucher(args):
    with _open(args) as db:
        if args.action == "post":
            lines = [_parse_line(s, i) for i, s in enumerate(args.line, 1)]
            result = db.post_voucher(
                args.date, lines, memo=args.memo or "",
                is_adjustment=args.adjustment,
            )
            tag = " [调整]" if result["is_adjustment"] else ""
            print(f"凭证已过账: {result['voucher_no']}{tag}  日期 {result['date']}  "
                  f"本位币合计 {_money(result['base_total'])}")
            print_table(
                ["#", "账户", "币种", "借方(原币)", "贷方(原币)", "汇率", "借方(本位币)", "贷方(本位币)"],
                [[str(l["line"]), l["account"], l["currency"], l["debit"], l["credit"],
                  l["rate"] or "-", l["base_debit"], l["base_credit"]]
                 for l in result["lines"]],
            )
        elif args.action == "list":
            for v in db.list_vouchers(args.period):
                tag = (" [调整]" if v["is_adjustment"] else "") + (
                    " [系统]" if v["is_system"] else "")
                print(f"{v['voucher_no']}  {v['voucher_date']}  "
                      f"{_money(v['base_total'])}{tag}  {v['memo']}")
        elif args.action == "show":
            doc = db.get_voucher(args.no)
            v = doc["voucher"]
            tag = (" [调整]" if v["is_adjustment"] else "") + (
                " [系统]" if v["is_system"] else "")
            print(f"{v['voucher_no']}{tag}  {v['voucher_date']}  期间 {v['period']}  {v['memo']}")
            print_table(
                ["#", "账户", "币种", "借方(原币)", "贷方(原币)", "汇率", "借方(本位币)", "贷方(本位币)"],
                [[str(e["line_no"]), e["account"], e["currency"], e["debit"], e["credit"],
                  e["rate"] or "-", e["base_debit"], e["base_credit"]]
                 for e in doc["entries"]],
            )


def cmd_revalue(args):
    with _open(args) as db:
        result = db.revalue(args.date, args.accounts or None, memo=args.memo or "")
    if not result["posted"]:
        print(f"{args.date} 重估完成：无差额，未生成凭证。")
    for p in result["posted"]:
        tag = " [调整]" if p["is_adjustment"] else ""
        print(f"已过账 {p['voucher_no']}{tag}: 账户 {p['account']}({p['currency']}) "
              f"外币余额 {p['fx_balance']} × {p['rate']} = {p['revalued_base']}，"
              f"汇兑差额 {p['diff']}")
    for s in result["skipped"]:
        print(f"跳过 {s['account']}: {s['reason']}")


def cmd_balances(args):
    with _open(args) as db:
        rows = db.balances(args.as_of)
    data = []
    for r in rows:
        fx = r["fx_amount"]
        base = r["base_amount"]
        data.append([
            r["account"], r["name"], r["currency"],
            _money(fx) if fx != 0 else "-",
            _money(base) if base != 0 else "-",
            "借" if r["side"] == "D" else "贷",
        ])
    print_table(["账户", "名称", "币种", "原币净额(借正贷负)", "本位币净额", "科目方向"], data)


def cmd_trial(args):
    with _open(args) as db:
        tb = db.trial_balance(args.period)
    title = f"试算平衡表（期间 {tb['period'] or '全部'}，本位币）"
    print(title)
    print_table(
        ["科目", "名称", "方向", "借方发生", "贷方发生"],
        [[l["subject"], l["name"], "借" if l["side"] == "D" else "贷",
          _money(l["debit"]), _money(l["credit"])] for l in tb["lines"]],
    )
    status = "平衡 ✓" if tb["balanced"] else f"不平！差额 {tb['total_debit'] - tb['total_credit']}"
    print(f"合计  借方 {_money(tb['total_debit'])}  贷方 {_money(tb['total_credit'])}  {status}")


def cmd_period(args):
    with _open(args) as db:
        if args.action == "list":
            rows = db.list_periods()
            print_table(["期间", "状态", "曾关账"],
                        [[r["code"],
                          "已关账" if r["status"] == "closed" else "开放",
                          "是" if r["ever_closed"] else "否"] for r in rows])
        elif args.action == "close":
            r = db.close_period(args.code)
            print(f"期间 {r['period']} 已关账（第 {r['generation']} 代快照）")
            print(f"  分录数 {r['entry_count']}  借方合计 {_money(r['debit_total'])}  "
                  f"贷方合计 {_money(r['credit_total'])}")
            print(f"  快照哈希 {r['snapshot_hash'][:16]}…")
        elif args.action == "reopen":
            r = db.reopen_period(args.code, args.reason)
            print(f"期间 {r['period']} 已反结账：{r['reason']}。之后补记必须加 --adjustment。")


def cmd_audit(args):
    with _open(args) as db:
        rows = db.audit_trail(args.limit)
    for r in reversed(rows):
        detail = r["detail_json"]
        if len(detail) > 160:
            detail = detail[:157] + "..."
        print(f"#{r['id']} {r['ts']} {r['action']:<14} {r['period'] or '-':<7} {detail}")


def cmd_verify(args):
    with _open(args) as db:
        result = db.verify_all()
        if args.snapshot_period:
            match = db.verify_latest_snapshot_matches_books(args.snapshot_period)
            print(f"期间 {match['period']} 第 {match['generation']} 代快照与当前账面完全一致 ✓")
    print("一致性核对全部通过 ✓")
    print(json.dumps(
        {k: (str(v) if isinstance(v, Decimal) else v) for k, v in result.items()},
        ensure_ascii=False, indent=2, default=str,
    ))


def cmd_equation(args):
    with _open(args) as db:
        eq = db.accounting_equation(args.as_of)
    print(f"资产+费用 = {_money(eq['assets_plus_expense'])}")
    print(f"负债+权益+收入 = {_money(eq['liab_equity_income'])}")
    print(("会计恒等式成立 ✓" if eq["balanced"] else
           f"恒等式不平！差额 {_money(eq['difference'])}"))


# ---------- 参数解析 ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="离线复式记账与期末关账台")
    p.add_argument("--db", help="SQLite 账本路径（也可用 LEDGER_DB 环境变量）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="创建账本")
    sp.add_argument("--base-ccy", default="CNY")
    sp.add_argument("--precision", type=int, default=2)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("currency", help="币种管理")
    sp.add_argument("action", choices=["add", "list"])
    sp.add_argument("code", nargs="?")
    sp.add_argument("name", nargs="?")
    sp.add_argument("precision", nargs="?", type=int)
    sp.set_defaults(func=cmd_currency)

    sp = sub.add_parser("subject", help="科目管理")
    sp.add_argument("action", choices=["add", "list"])
    sp.add_argument("code", nargs="?")
    sp.add_argument("name", nargs="?")
    sp.add_argument("side", nargs="?", choices=["D", "C"])
    sp.set_defaults(func=cmd_subject)

    sp = sub.add_parser("account", help="账户管理")
    sp.add_argument("action", choices=["add", "list"])
    sp.add_argument("code", nargs="?")
    sp.add_argument("name", nargs="?")
    sp.add_argument("subject", nargs="?")
    sp.add_argument("currency", nargs="?")
    sp.set_defaults(func=cmd_account)

    sp = sub.add_parser("rate", help="汇率管理")
    sp.add_argument("action", choices=["set", "list"])
    sp.add_argument("currency", nargs="?")
    sp.add_argument("date", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(func=cmd_rate)

    sp = sub.add_parser("fx-config", help="配置汇兑损益科目与账户")
    sp.add_argument("subject")
    sp.add_argument("account")
    sp.set_defaults(func=cmd_fx_config)

    sp = sub.add_parser("voucher", help="凭证")
    vsub = sp.add_subparsers(dest="action", required=True)
    vp = vsub.add_parser("post", help="过账")
    vp.add_argument("date")
    vp.add_argument("--line", action="append", required=True,
                    help="账户:币种:借方:贷方，可重复")
    vp.add_argument("--memo", default="")
    vp.add_argument("--adjustment", action="store_true", help="标记为反结账后的调整凭证")
    vl = vsub.add_parser("list", help="列表")
    vl.add_argument("--period")
    vshow = vsub.add_parser("show", help="查看一张凭证")
    vshow.add_argument("no")
    sp.set_defaults(func=cmd_voucher)

    sp = sub.add_parser("revalue", help="期末外币重估")
    sp.add_argument("date", help="重估汇率日（必须存在当日汇率）")
    sp.add_argument("--accounts", nargs="*", help="只重估指定账户，默认全部有余额外币户")
    sp.add_argument("--memo", default="")
    sp.set_defaults(func=cmd_revalue)

    sp = sub.add_parser("balances", help="账户余额")
    sp.add_argument("--as-of", dest="as_of")
    sp.set_defaults(func=cmd_balances)

    sp = sub.add_parser("trial", help="科目试算平衡表")
    sp.add_argument("period", nargs="?")
    sp.set_defaults(func=cmd_trial)

    sp = sub.add_parser("equation", help="会计恒等式核对")
    sp.add_argument("--as-of", dest="as_of")
    sp.set_defaults(func=cmd_equation)

    sp = sub.add_parser("period", help="会计期间关账/反结账")
    sp.add_argument("action", choices=["list", "close", "reopen"])
    sp.add_argument("code", nargs="?")
    sp.add_argument("--reason", default="")
    sp.set_defaults(func=cmd_period)

    sp = sub.add_parser("audit", help="审计日志")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("verify", help="余额/审计链/快照一致性核对")
    sp.add_argument("--snapshot-period", dest="snapshot_period",
                    help="同时核对该期间最新快照与当前账面一致")
    sp.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except LineError as exc:
        print(f"[过账失败 {exc.code}] {exc}", file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"[错误 {exc.code}] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
