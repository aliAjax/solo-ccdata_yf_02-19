"""离线复式记账核心引擎。

设计要点
========
* 仅依赖标准库：``sqlite3`` 持久化、``decimal`` 精确金额、``hashlib`` 审计链。
* 金额一律 :class:`Decimal`，按币种精度四舍五入；借贷按本位币（CNY）试算平衡，
  跨币种凭证的每条分录携带过账时实际使用的汇率。
* 缺汇率、金额精度不符、借贷不平等错误一律落到具体分录（:class:`LineError`）。
* 期末重估按指定汇率日的精确汇率执行，汇兑差额自动生成平衡凭证；
  同一汇率日重估时无差额则跳过，因此可安全重复执行。
* 关账后凭证/汇率/重估全部阻断；反结账后补记必须显式标记为调整凭证，
  系统生成的重估凭证会被自动标记。每次关账生成一代余额快照，哈希成链。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date as _date
from decimal import Decimal

from .errors import (
    ConfigurationError,
    ConsistencyError,
    LedgerError,
    LineError,
    NotFoundError,
    PeriodClosedError,
    UnbalancedVoucherError,
    ValidationError,
)
from .money import D, convert, is_quantized, quantize, rate_to_decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS currency(
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    precision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subject(
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('D','C'))      -- D=借方科目(资产/费用), C=贷方科目(负债/权益/收入)
);
CREATE TABLE IF NOT EXISTS account(
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject_code TEXT NOT NULL REFERENCES subject(code),
    currency TEXT NOT NULL REFERENCES currency(code)
);
CREATE TABLE IF NOT EXISTS rate(
    currency TEXT NOT NULL REFERENCES currency(code),
    rate_date TEXT NOT NULL,
    rate TEXT NOT NULL,
    PRIMARY KEY(currency, rate_date)
);
CREATE TABLE IF NOT EXISTS period(
    code TEXT PRIMARY KEY,                          -- YYYY-MM
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    ever_closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS voucher(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT NOT NULL UNIQUE,
    voucher_date TEXT NOT NULL,
    period TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    is_adjustment INTEGER NOT NULL DEFAULT 0,
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id INTEGER NOT NULL REFERENCES voucher(id),
    line_no INTEGER NOT NULL,
    account TEXT NOT NULL REFERENCES account(code),
    currency TEXT NOT NULL REFERENCES currency(code),
    debit TEXT NOT NULL DEFAULT '0',
    credit TEXT NOT NULL DEFAULT '0',
    base_debit TEXT NOT NULL DEFAULT '0',
    base_credit TEXT NOT NULL DEFAULT '0',
    rate TEXT,
    entry_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_balance(
    account TEXT PRIMARY KEY REFERENCES account(code),
    currency TEXT NOT NULL,
    fx_amount TEXT NOT NULL DEFAULT '0',            -- 借正贷负的外币净额
    base_amount TEXT NOT NULL DEFAULT '0'           -- 借正贷负的本位币净额
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    period TEXT,
    detail_json TEXT NOT NULL,
    prev_hash TEXT,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS period_snapshot(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    generation INTEGER NOT NULL,
    closed_at TEXT NOT NULL,
    reopened_at TEXT,
    entry_count INTEGER NOT NULL,
    dr_total TEXT NOT NULL,
    cr_total TEXT NOT NULL,
    balances_json TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    prev_snapshot_hash TEXT,
    hash TEXT NOT NULL,
    UNIQUE(period, generation)
);
"""

ASSET_SIDES = {"D"}  # 借方科目：资产 + 费用；贷方科目：负债 + 权益 + 收入


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def period_of(day: str) -> str:
    _date.fromisoformat(day)
    return day[:7]


class Ledger:
    """账本句柄。用法::

        with Ledger.create("/path/to/books.db", base_currency="CNY") as db:
            db.post_voucher(...)
    """

    # ---------- 构造 ----------

    SCHEMA_VERSION = "2"  # v2：审计链/快照链带链头锚点；v1 账本打开时安全迁移

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self._has_tables():
            # 打开既有账本：幂等补齐新表，并把 v1 无锚点账本安全迁移到 v2。
            # 旧链无法证明连续时这里直接抛 ConsistencyError，账本保持阻断。
            self.conn.executescript(SCHEMA)
            self._ensure_anchors()

    def _has_tables(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currency'"
        ).fetchone()
        return row is not None

    @classmethod
    def create(cls, path: str, base_currency: str = "CNY", base_precision: int = 2):
        db = cls.__new__(cls)
        db.path = path
        db.conn = sqlite3.connect(path)
        db.conn.row_factory = sqlite3.Row
        db.conn.execute("PRAGMA foreign_keys=ON")
        with db.conn:
            db.conn.executescript(SCHEMA)
            db.conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('base_currency',?)",
                (base_currency,),
            )
            db.conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('base_precision',?)",
                (str(base_precision),),
            )
            db.conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
                (cls.SCHEMA_VERSION,),
            )
            db.conn.execute(
                "INSERT OR IGNORE INTO currency(code,name,precision) VALUES(?,?,?)",
                (base_currency, "本位币", base_precision),
            )
        return db

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()

    # ---------- 小工具 ----------

    @property
    def base_currency(self) -> str:
        return self.conn.execute("SELECT value FROM meta WHERE key='base_currency'").fetchone()[0]

    @property
    def base_precision(self) -> int:
        return int(
            self.conn.execute("SELECT value FROM meta WHERE key='base_precision'").fetchone()[0]
        )

    def _now(self) -> str:
        from datetime import datetime

        return datetime.now().isoformat(timespec="seconds")

    def _currency_prec(self, code: str) -> int:
        row = self.conn.execute("SELECT precision FROM currency WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"币种不存在: {code}", code="E_UNKNOWN_CURRENCY")
        return row["precision"]

    def _account(self, code: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM account WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"账户不存在: {code}", code="E_UNKNOWN_ACCOUNT")
        return row

    def _period_row(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM period WHERE code=?", (code,)).fetchone()

    def _ensure_period(self, code: str) -> sqlite3.Row:
        row = self._period_row(code)
        if row is None:
            with self.conn:
                self.conn.execute("INSERT INTO period(code) VALUES(?)", (code,))
            row = self._period_row(code)
        return row

    def _assert_period_open(self, code: str, day: str):
        row = self._ensure_period(code)
        if row["status"] == "closed":
            raise PeriodClosedError(
                f"期间 {code} 已关账，不能对该期间写入（日期 {day}）；如需补记请先反结账。"
            )
        return row

    def _audit(self, action: str, period: str | None, detail: dict) -> int:
        """追加审计记录，按前一条哈希串联成链；同步更新链头锚点。

        调用方必须已经处于写事务中——锚点更新与记录插入必须原子提交，
        否则整批重估回滚时会留下悬空锚点。
        """
        last = self.conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else None
        ts = self._now()
        h = _sha256(f"{prev or ''}|{ts}|{action}|{period or ''}|{_canon(detail)}")
        cur = self.conn.execute(
            "INSERT INTO audit_log(ts,action,period,detail_json,prev_hash,hash)"
            " VALUES(?,?,?,?,?,?)",
            (ts, action, period, _canon(detail), prev, h),
        )
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('audit_head',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (h,),
        )
        return cur.lastrowid

    # ---------- 主数据 ----------

    def add_currency(self, code: str, name: str, precision: int):
        if precision < 0 or precision > 18:
            raise ValidationError("币种精度必须在 0..18 之间")
        with self.conn:
            self.conn.execute(
                "INSERT INTO currency(code,name,precision) VALUES(?,?,?)",
                (code, name, precision),
            )
            self._audit("currency.add", None, {"code": code, "name": name, "precision": precision})

    def add_subject(self, code: str, name: str, side: str):
        if side not in ("D", "C"):
            raise ValidationError("科目方向必须为 D(借方) 或 C(贷方)")
        with self.conn:
            self.conn.execute(
                "INSERT INTO subject(code,name,side) VALUES(?,?,?)",
                (code, name, side),
            )
            self._audit("subject.add", None, {"code": code, "name": name, "side": side})

    def add_account(self, code: str, name: str, subject_code: str, currency: str):
        self._currency_prec(currency)
        subj = self.conn.execute(
            "SELECT code FROM subject WHERE code=?", (subject_code,)
        ).fetchone()
        if subj is None:
            raise NotFoundError(f"科目不存在: {subject_code}", code="E_UNKNOWN_SUBJECT")
        with self.conn:
            self.conn.execute(
                "INSERT INTO account(code,name,subject_code,currency) VALUES(?,?,?,?)",
                (code, name, subject_code, currency),
            )
            self._audit(
                "account.add",
                None,
                {"code": code, "name": name, "subject": subject_code, "currency": currency},
            )

    def set_rate(self, currency: str, rate_date: str, rate):
        """维护某日汇率（1 单位外币兑换多少本位币）。已关账期间的汇率不可改。"""
        if currency == self.base_currency:
            raise ValidationError("本位币不需要汇率")
        self._currency_prec(currency)
        _date.fromisoformat(rate_date)
        rate = rate_to_decimal(rate)
        period = period_of(rate_date)
        prow = self._period_row(period)
        if prow is not None and prow["status"] == "closed":
            raise PeriodClosedError(f"期间 {period} 已关账，不能修改 {rate_date} 的汇率")
        with self.conn:
            self.conn.execute(
                "INSERT INTO rate(currency,rate_date,rate) VALUES(?,?,?) "
                "ON CONFLICT(currency,rate_date) DO UPDATE SET rate=excluded.rate",
                (currency, rate_date, str(rate)),
            )
            self._audit(
                "rate.set", period, {"currency": currency, "date": rate_date, "rate": str(rate)}
            )

    def get_rate(self, currency: str, rate_date: str, *, exact: bool = False) -> Decimal | None:
        """取汇率。exact=False（过账用）取截至该日的最近汇率；exact=True（重估用）只认当日。"""
        op = "=" if exact else "<="
        row = self.conn.execute(
            f"SELECT rate FROM rate WHERE currency=? AND rate_date{op}? "
            "ORDER BY rate_date DESC LIMIT 1",
            (currency, rate_date),
        ).fetchone()
        return D(row["rate"]) if row else None

    def configure_fx(self, gain_loss_subject: str, gain_loss_account: str):
        """设置汇兑损益科目与其本位币账户。"""
        subj = self.conn.execute(
            "SELECT * FROM subject WHERE code=?", (gain_loss_subject,)
        ).fetchone()
        if subj is None:
            raise NotFoundError(f"汇兑损益科目不存在: {gain_loss_subject}")
        if subj["side"] != "D":
            raise ConfigurationError("汇兑损益科目应为损益(费用)类借方科目")
        acct = self._account(gain_loss_account)
        if acct["subject_code"] != gain_loss_subject or acct["currency"] != self.base_currency:
            raise ConfigurationError(
                f"汇兑损益账户 {gain_loss_account} 必须挂在 {gain_loss_subject} 下且为本位币账户"
            )
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('fx_gain_subject',?)",
                (gain_loss_subject,),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('fx_gain_account',?)",
                (gain_loss_account,),
            )
            self._audit(
                "fx.configure",
                None,
                {"subject": gain_loss_subject, "account": gain_loss_account},
            )

    def _fx_config(self) -> tuple[str, str]:
        s = self.conn.execute(
            "SELECT value FROM meta WHERE key='fx_gain_subject'"
        ).fetchone()
        a = self.conn.execute(
            "SELECT value FROM meta WHERE key='fx_gain_account'"
        ).fetchone()
        if not s or not a:
            raise ConfigurationError("尚未配置汇兑损益科目/账户，请先运行 fx-config")
        return s["value"], a["value"]

    # ---------- 凭证过账 ----------

    def _next_voucher_no(self, period: str, prefix: str) -> str:
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM voucher WHERE period=? AND voucher_no LIKE ?",
            (period, f"{prefix}{period}-%"),
        ).fetchone()["c"]
        return f"{prefix}{period}-{n + 1:04d}"

    def post_voucher(
        self,
        voucher_date: str,
        lines: list[dict],
        memo: str = "",
        *,
        is_adjustment: bool = False,
        voucher_no: str | None = None,
        _system: bool = False,
    ) -> dict:
        """过账一张含多条借贷分录的凭证（公共入口，自带事务）。

        每条 line 为::

            {"account": "1002", "currency": "USD",
             "debit": "100", "credit": "0"}        # debit/credit 二选一且 > 0

        系统重估凭证内部使用 ``base_debit/base_credit`` 表达外币为 0、本位币有差额的行。
        错误以 :class:`LineError`（带分录序号）或 :class:`UnbalancedVoucherError`
        （不平，逐条列出对应分录的本位币金额）抛出；校验全部通过后才开启事务。
        """
        prep = self._prepare_voucher(
            voucher_date, lines, memo,
            is_adjustment=is_adjustment, _system=_system,
        )
        with self.conn:
            return self._persist_voucher(prep, voucher_no=voucher_no)

    def _prepare_voucher(
        self,
        voucher_date: str,
        lines: list[dict],
        memo: str,
        *,
        is_adjustment: bool,
        _system: bool,
    ) -> dict:
        """纯校验 + 折算准备，不做任何写入；批量重估用它做事务前预检。"""
        try:
            _date.fromisoformat(voucher_date)
        except ValueError:
            raise ValidationError(f"凭证日期非法: {voucher_date!r}")
        period = period_of(voucher_date)
        prow = self._assert_period_open(period, voucher_date)

        if not isinstance(lines, list) or len(lines) < 2:
            raise ValidationError("凭证至少需要两条借贷分录")
        if prow["ever_closed"] and not is_adjustment:
            raise ValidationError(
                f"期间 {period} 曾经关账，反结账后的补记必须显式标记为调整凭证"
                "（is_adjustment=True / --adjustment）"
            )
        if not prow["ever_closed"] and is_adjustment:
            raise ValidationError(f"期间 {period} 从未关账，不能把凭证标记为调整凭证")

        base_ccy = self.base_currency
        base_prec = self.base_precision
        prepared: list[dict] = []
        total_dr = Decimal("0")
        total_cr = Decimal("0")

        for idx, raw in enumerate(lines, start=1):
            acct_code = raw.get("account")
            ccy = raw.get("currency")
            if not acct_code:
                raise LineError(idx, "缺少账户", code="E_LINE_FIELD")
            if not ccy:
                raise LineError(idx, "缺少币种", code="E_LINE_FIELD")
            try:
                acct = self._account(acct_code)
            except NotFoundError as exc:
                raise LineError(idx, str(exc), code=exc.code) from None
            try:
                prec = self._currency_prec(ccy)
            except NotFoundError as exc:
                raise LineError(idx, str(exc), code=exc.code) from None
            if acct["currency"] != ccy:
                raise LineError(
                    idx,
                    f"账户 {acct_code} 的币种是 {acct['currency']}，分录币种却是 {ccy}",
                    code="E_LINE_CURRENCY",
                )

            # 系统行（重估的外币腿）：外币额为 0，直接给本位币差额
            if _system and ("base_debit" in raw or "base_credit" in raw):
                fx_dr = quantize(D(raw.get("debit", 0)), prec)
                fx_cr = quantize(D(raw.get("credit", 0)), prec)
                b_dr = quantize(D(raw.get("base_debit", 0)), base_prec)
                b_cr = quantize(D(raw.get("base_credit", 0)), base_prec)
                if (b_dr > 0) == (b_cr > 0):
                    raise LineError(idx, "本位币借贷必须且只能有一方为正", code="E_LINE_SIDE")
                rate_used = None
            else:
                try:
                    fx_dr = D(raw.get("debit", 0))
                    fx_cr = D(raw.get("credit", 0))
                except ValueError as exc:
                    raise LineError(idx, str(exc), code="E_LINE_AMOUNT") from None
                if fx_dr < 0 or fx_cr < 0:
                    raise LineError(idx, "借贷金额不能为负", code="E_LINE_AMOUNT")
                if (fx_dr > 0) == (fx_cr > 0):
                    raise LineError(
                        idx, "借贷必须且只能有一方填写正数金额", code="E_LINE_SIDE"
                    )
                if not is_quantized(fx_dr, prec) or not is_quantized(fx_cr, prec):
                    raise LineError(
                        idx,
                        f"金额不符合币种 {ccy} 的精度要求（{prec} 位小数）",
                        code="E_LINE_PRECISION",
                    )
                fx_dr = quantize(fx_dr, prec)
                fx_cr = quantize(fx_cr, prec)

                if ccy == base_ccy:
                    rate_used = None
                    b_dr, b_cr = fx_dr, fx_cr
                else:
                    rate_used = self.get_rate(ccy, voucher_date)
                    if rate_used is None:
                        raise LineError(
                            idx,
                            f"币种 {ccy} 缺少截至 {voucher_date} 的汇率，无法折算本位币",
                            code="E_LINE_NO_RATE",
                        )
                    b_dr = convert(fx_dr, rate_used, base_prec)
                    b_cr = convert(fx_cr, rate_used, base_prec)

            total_dr += b_dr
            total_cr += b_cr
            prepared.append(
                dict(
                    line_no=idx,
                    account=acct_code,
                    currency=ccy,
                    debit=fx_dr,
                    credit=fx_cr,
                    base_debit=b_dr,
                    base_credit=b_cr,
                    rate=rate_used,
                )
            )

        diff = quantize(total_dr - total_cr, base_prec)
        if diff != 0:
            raise UnbalancedVoucherError(
                base_ccy,
                quantize(total_dr, base_prec),
                quantize(total_cr, base_prec),
                diff,
                [
                    {
                        "line": p["line_no"],
                        "account": p["account"],
                        "currency": p["currency"],
                        "base_debit": str(p["base_debit"]),
                        "base_credit": str(p["base_credit"]),
                        "rate": p["rate"],
                    }
                    for p in prepared
                ],
            )

        return {
            "voucher_date": voucher_date,
            "period": period,
            "memo": memo,
            "is_adjustment": is_adjustment,
            "is_system": _system,
            "prepared": prepared,
            "total_dr": total_dr,
            "total_cr": total_cr,
        }

    def _persist_voucher(self, prep: dict, *, voucher_no: str | None = None) -> dict:
        """把已通过校验的凭证写入库。调用方必须已持有事务；本方法不自行提交。"""
        voucher_date = prep["voucher_date"]
        period = prep["period"]
        memo = prep["memo"]
        is_adjustment = prep["is_adjustment"]
        _system = prep["is_system"]
        prepared = prep["prepared"]
        total_dr = prep["total_dr"]

        no = voucher_no or self._next_voucher_no(period, "R-" if _system else "V-")
        ts = self._now()
        detail_lines = [
            {
                "line": p["line_no"],
                "account": p["account"],
                "currency": p["currency"],
                "debit": str(p["debit"]),
                "credit": str(p["credit"]),
                "base_debit": str(p["base_debit"]),
                "base_credit": str(p["base_credit"]),
                "rate": str(p["rate"]) if p["rate"] is not None else None,
            }
            for p in prepared
        ]
        cur = self.conn.execute(
            "INSERT INTO voucher(voucher_no,voucher_date,period,memo,"
            "is_adjustment,is_system,created_at) VALUES(?,?,?,?,?,?,?)",
            (no, voucher_date, period, memo, int(is_adjustment), int(_system), ts),
        )
        vid = cur.lastrowid
        for p in prepared:
            eh = _sha256(
                "|".join(
                    [
                        no,
                        voucher_date,
                        str(int(is_adjustment)),
                        str(int(_system)),
                        str(p["line_no"]),
                        p["account"],
                        p["currency"],
                        str(p["debit"]),
                        str(p["credit"]),
                        str(p["base_debit"]),
                        str(p["base_credit"]),
                        str(p["rate"]) if p["rate"] is not None else "",
                    ]
                )
            )
            self.conn.execute(
                "INSERT INTO entry(voucher_id,line_no,account,currency,debit,credit,"
                "base_debit,base_credit,rate,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    vid,
                    p["line_no"],
                    p["account"],
                    p["currency"],
                    str(p["debit"]),
                    str(p["credit"]),
                    str(p["base_debit"]),
                    str(p["base_credit"]),
                    str(p["rate"]) if p["rate"] is not None else None,
                    eh,
                ),
            )
            net_fx = p["debit"] - p["credit"]
            net_base = p["base_debit"] - p["base_credit"]
            existing = self.conn.execute(
                "SELECT fx_amount, base_amount FROM ledger_balance WHERE account=?",
                (p["account"],),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO ledger_balance(account,currency,fx_amount,base_amount) "
                    "VALUES(?,?,?,?)",
                    (p["account"], p["currency"], str(net_fx), str(net_base)),
                )
            else:
                self.conn.execute(
                    "UPDATE ledger_balance SET fx_amount=?, base_amount=? WHERE account=?",
                    (
                        str(D(existing["fx_amount"]) + net_fx),
                        str(D(existing["base_amount"]) + net_base),
                        p["account"],
                    ),
                )
        audit_id = self._audit(
            "voucher.post",
            period,
            {
                "voucher_no": no,
                "date": voucher_date,
                "memo": memo,
                "adjustment": bool(is_adjustment),
                "system": bool(_system),
                "base_total": str(total_dr),
                "lines": detail_lines,
            },
        )

        return {
            "voucher_id": vid,
            "voucher_no": no,
            "date": voucher_date,
            "period": period,
            "base_total": total_dr,
            "is_adjustment": is_adjustment,
            "is_system": _system,
            "lines": detail_lines,
            "audit_id": audit_id,
        }

    # ---------- 期末外币重估 ----------

    def revalue(self, rate_date: str, accounts: list[str] | None = None, *, memo: str = "") -> dict:
        """按指定汇率日**整批**重估外币账户余额，汇兑差额自动入账。

        原子性保证（两阶段）：
          1. 预检阶段（只读）：聚合截至重估日余额，逐个账户校验当日汇率、
             计算差额、构建并校验全部系统凭证。任一账户缺汇率或任一凭证不平衡，
             直接抛出——此时尚未写入任何东西；
          2. 提交阶段：全部系统凭证、余额变动与一条批次审计记录在**同一个事务**
             内落库，中途任何异常整批回滚，不会留下半批系统凭证。
        因此失败后修好汇率重试，结果与一次成功完全一致（同日重估本就幂等）。

        其它规则：
        * 只统计 ``voucher_date <= rate_date`` 的分录（后续期间业务不影响本次重估）；
        * 汇率必须在 ``rate_date`` 当天存在（不用历史最近汇率）；
        * 差额为 0 的账户跳过；目标期间曾关账时系统凭证自动标为调整凭证。
        """
        _date.fromisoformat(rate_date)
        period = period_of(rate_date)
        prow = self._assert_period_open(period, rate_date)
        ever_closed = bool(prow["ever_closed"])
        _, fx_account = self._fx_config()

        # ---------- 阶段 1：只读预检 + 构建全部凭证（不产生任何写入）----------
        agg = self._revalue_balances(rate_date, accounts)
        plans: list[dict] = []
        skips: list[dict] = []
        posted_meta: list[dict] = []
        for acct_code, x in sorted(agg.items()):
            ccy = x["currency"]
            fx_bal = x["fx"]
            base_bal = x["base"]
            if fx_bal == 0:
                skips.append({"account": acct_code, "reason": "截至重估日外币余额为 0"})
                continue
            rate = self.get_rate(ccy, rate_date, exact=True)
            if rate is None:
                # 预检失败：本方法尚未执行任何 INSERT/UPDATE，调用方账本无残留
                raise ValidationError(
                    f"整批重估中止：账户 {acct_code}（{ccy}）在重估日 {rate_date} "
                    "没有当天汇率；已取消本批全部重估凭证（无任何写入），补齐汇率后请重试"
                )
            target = convert(fx_bal, rate, self.base_precision)
            diff = quantize(target - base_bal, self.base_precision)
            if diff == 0:
                skips.append(
                    {"account": acct_code, "currency": ccy, "reason": "重估差额为 0，无需入账"}
                )
                continue

            # 差额 >0：借外币账户 / 贷汇兑损益（收益）；<0 反向（损失）
            if diff > 0:
                fx_line = {"account": acct_code, "currency": ccy, "base_debit": str(diff)}
                gl_line = {"account": fx_account, "currency": self.base_currency,
                           "credit": str(diff)}
            else:
                fx_line = {"account": acct_code, "currency": ccy, "base_credit": str(-diff)}
                gl_line = {"account": fx_account, "currency": self.base_currency,
                           "debit": str(-diff)}
            plan = self._prepare_voucher(
                rate_date,
                [fx_line, gl_line],
                memo or f"期末汇率重估 {acct_code} @ {rate_date}（汇率 {rate}）",
                is_adjustment=ever_closed,
                _system=True,
            )
            plans.append(plan)
            posted_meta.append(
                {
                    "account": acct_code,
                    "currency": ccy,
                    "fx_balance": str(fx_bal),
                    "rate": str(rate),
                    "revalued_base": str(target),
                    "diff": str(diff),
                    "is_adjustment": ever_closed,
                }
            )

        if not plans:
            # 全是跳过项：不产生系统凭证，也不产生批次审计记录（无可追踪的写入）
            return {"date": rate_date, "period": period, "posted": [], "skipped": skips,
                    "audit_id": None}

        # 预分配系统凭证号，避免落库阶段再做计数查询
        existing = self.conn.execute(
            "SELECT COUNT(*) c FROM voucher WHERE period=? AND voucher_no LIKE ?",
            (period, f"R-{period}-%"),
        ).fetchone()["c"]
        for i, plan in enumerate(plans, start=1):
            plan["_voucher_no"] = f"R-{period}-{existing + i:04d}"

        # ---------- 阶段 2：单事务提交整批 ----------
        posted: list[dict] = []
        try:
            with self.conn:
                for plan, meta in zip(plans, posted_meta):
                    v = self._persist_voucher(plan, voucher_no=plan["_voucher_no"])
                    posted.append({"voucher_no": v["voucher_no"], **meta})
                audit_id = self._audit(
                    "fx.revalue",
                    period,
                    {"date": rate_date, "posted": posted, "skipped": skips,
                     "batch_size": len(posted)},
                )
        except Exception:
            # with 块已回滚全部凭证/余额/审计；防御性确认无该批凭证残留
            raise

        return {"date": rate_date, "period": period, "posted": posted, "skipped": skips,
                "audit_id": audit_id}

    def _revalue_balances(self, rate_date: str, accounts: list[str] | None) -> dict[str, dict]:
        """聚合截至重估日各外币账户的原币/本位币净额（Decimal，Python 侧求和）。"""
        base_prec = self.base_precision
        sql = (
            "SELECT e.account account, a.name name, e.currency currency, "
            "e.debit dr, e.credit cr, e.base_debit bdr, e.base_credit bcr "
            "FROM entry e JOIN voucher v ON v.id=e.voucher_id "
            "JOIN account a ON a.code=e.account "
            "WHERE v.voucher_date<=? AND e.currency<>?"
        )
        params: list = [rate_date, self.base_currency]
        if accounts:
            sql += " AND e.account IN (" + ",".join("?" * len(accounts)) + ")"
            params += list(accounts)
        sql += " ORDER BY e.id"
        agg: dict[str, dict] = {}
        for r in self.conn.execute(sql, params).fetchall():
            x = agg.setdefault(
                r["account"],
                {"name": r["name"], "currency": r["currency"],
                 "fx": Decimal("0"), "base": Decimal("0")},
            )
            x["fx"] += D(r["dr"]) - D(r["cr"])
            x["base"] += D(r["bdr"]) - D(r["bcr"])
        for x in agg.values():
            x["base"] = quantize(x["base"], base_prec)
        return agg

    # ---------- 余额 / 试算 / 恒等式 ----------

    def balances(self, as_of: str | None = None) -> list[dict]:
        """各账户余额（借正贷负）。as_of 给定时按分录重算截至日余额。"""
        if as_of is None:
            rows = self.conn.execute(
                "SELECT b.account, a.name, b.currency, b.fx_amount, b.base_amount, "
                "a.subject_code, s.side "
                "FROM ledger_balance b JOIN account a ON a.code=b.account "
                "JOIN subject s ON s.code=a.subject_code ORDER BY b.account"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT e.account, a.name, e.currency, e.debit, e.credit, "
                "e.base_debit, e.base_credit, a.subject_code, s.side "
                "FROM entry e JOIN voucher v ON v.id=e.voucher_id "
                "JOIN account a ON a.code=e.account JOIN subject s ON s.code=a.subject_code "
                "WHERE v.voucher_date<=? ORDER BY e.id",
                (as_of,),
            ).fetchall()
            agg: dict[str, dict] = {}
            for r in rows:
                x = agg.setdefault(
                    r["account"],
                    {"name": r["name"], "currency": r["currency"],
                     "subject_code": r["subject_code"], "side": r["side"],
                     "fx_amount": Decimal("0"), "base_amount": Decimal("0")},
                )
                x["fx_amount"] += D(r["debit"]) - D(r["credit"])
                x["base_amount"] += D(r["base_debit"]) - D(r["base_credit"])
            return [
                {
                    "account": k,
                    "name": v["name"],
                    "subject": v["subject_code"],
                    "currency": v["currency"],
                    "fx_amount": v["fx_amount"],
                    "base_amount": v["base_amount"],
                    "side": v["side"],
                }
                for k, v in sorted(agg.items())
            ]
        return [
            {
                "account": r["account"],
                "name": r["name"],
                "subject": r["subject_code"],
                "currency": r["currency"],
                "fx_amount": D(r["fx_amount"]),
                "base_amount": D(r["base_amount"]),
                "side": r["side"],
            }
            for r in rows
        ]

    def _entries_through(self, period_end: str):
        return self.conn.execute(
            "SELECT e.*, v.voucher_no, v.voucher_date, v.is_adjustment, v.is_system, "
            "a.subject_code, s.side "
            "FROM entry e JOIN voucher v ON v.id=e.voucher_id "
            "JOIN account a ON a.code=e.account JOIN subject s ON s.code=a.subject_code "
            "WHERE v.voucher_date<=? ORDER BY e.id",
            (period_end,),
        ).fetchall()

    def recompute_balances(self, through: str | None = None) -> dict[str, dict]:
        """从分录重算全部账户余额，返回 {account: {currency,fx,base}}。

        聚合一律在 Python 用 Decimal 完成——SQLite 没有真正的 DECIMAL 类型，
        ``CAST AS DECIMAL`` 会退化成浮点 REAL，不能用于金额。
        """
        if through is None:
            rows = self.conn.execute(
                "SELECT account, currency, debit, credit, base_debit, base_credit "
                "FROM entry"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT e.account account, e.currency currency, e.debit debit, "
                "e.credit credit, e.base_debit base_debit, e.base_credit base_credit "
                "FROM entry e JOIN voucher v ON v.id=e.voucher_id "
                "WHERE v.voucher_date<=?",
                (through,),
            ).fetchall()
        agg: dict[str, dict] = {}
        for r in rows:
            x = agg.setdefault(
                r["account"],
                {"currency": r["currency"], "fx": Decimal("0"), "base": Decimal("0")},
            )
            x["fx"] += D(r["debit"]) - D(r["credit"])
            x["base"] += D(r["base_debit"]) - D(r["base_credit"])
        return agg

    def trial_balance(self, period: str | None = None) -> dict:
        """科目汇总试算表（本位币）。period 给定时只含该期间分录。"""
        if period:
            rows = self.conn.execute(
                "SELECT a.subject_code code, s.name name, s.side side, "
                "e.base_debit bdr, e.base_credit bcr "
                "FROM entry e JOIN voucher v ON v.id=e.voucher_id "
                "JOIN account a ON a.code=e.account JOIN subject s ON s.code=a.subject_code "
                "WHERE v.period=? ORDER BY a.subject_code, e.id",
                (period,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT a.subject_code code, s.name name, s.side side, "
                "e.base_debit bdr, e.base_credit bcr "
                "FROM entry e JOIN account a ON a.code=e.account "
                "JOIN subject s ON s.code=a.subject_code ORDER BY a.subject_code, e.id"
            ).fetchall()
        agg: dict[str, dict] = {}
        for r in rows:
            x = agg.setdefault(
                r["code"],
                {"name": r["name"], "side": r["side"],
                 "debit": Decimal("0"), "credit": Decimal("0")},
            )
            x["debit"] += D(r["bdr"])
            x["credit"] += D(r["bcr"])
        lines = [
            {"subject": code, "name": v["name"], "side": v["side"],
             "debit": v["debit"], "credit": v["credit"]}
            for code, v in sorted(agg.items())
        ]
        td = sum((x["debit"] for x in lines), Decimal("0"))
        tc = sum((x["credit"] for x in lines), Decimal("0"))
        return {"period": period, "lines": lines, "total_debit": td, "total_credit": tc,
                "balanced": quantize(td - tc, self.base_precision) == 0}

    def accounting_equation(self, as_of: str | None = None) -> dict:
        """资产+费用(借方科目净额) = 负债+权益+收入(贷方科目净额)。"""
        if as_of:
            rows = self.conn.execute(
                "SELECT s.side side, e.base_debit bdr, e.base_credit bcr "
                "FROM entry e JOIN voucher v ON v.id=e.voucher_id "
                "JOIN account a ON a.code=e.account JOIN subject s ON s.code=a.subject_code "
                "WHERE v.voucher_date<=?",
                (as_of,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT s.side side, e.base_debit bdr, e.base_credit bcr "
                "FROM entry e JOIN account a ON a.code=e.account "
                "JOIN subject s ON s.code=a.subject_code"
            ).fetchall()
        net_by_side = {"D": Decimal("0"), "C": Decimal("0")}
        for r in rows:
            net_by_side[r["side"]] += D(r["bdr"]) - D(r["bcr"])
        debit_side = net_by_side["D"]
        credit_side = -net_by_side["C"]
        diff = quantize(debit_side - credit_side, self.base_precision)
        return {
            "as_of": as_of,
            "assets_plus_expense": debit_side,
            "liab_equity_income": credit_side,
            "difference": diff,
            "balanced": diff == 0,
        }

    # ---------- 关账 / 反结账 ----------

    def _period_end(self, period: str) -> str:
        import calendar

        y, m = int(period[:4]), int(period[5:7])
        return _date(y, m, calendar.monthrange(y, m)[1]).isoformat()

    def close_period(self, period: str) -> dict:
        """关账：先做余额重算/会计恒等式/审计链核对，再固化一代快照。"""
        prow = self._period_row(period)
        if prow is None:
            raise NotFoundError(f"期间不存在: {period}")
        if prow["status"] == "closed":
            raise PeriodClosedError(f"期间 {period} 已经是关账状态")

        # 1) 存储余额表必须与分录重算一致
        self.verify_balances()
        # 2) 会计恒等式成立
        eq = self.accounting_equation()
        if not eq["balanced"]:
            raise ConsistencyError(f"会计恒等式不平，差额 {eq['difference']}，禁止关账")
        # 3) 审计链完整
        self.verify_audit_chain()

        end = self._period_end(period)
        rows = self._entries_through(end)
        # 仅本期间的分录纳入本期快照
        in_period = [r for r in rows if r["voucher_date"][:7] == period]
        dr_total = sum((D(r["base_debit"]) for r in in_period), Decimal("0"))
        cr_total = sum((D(r["base_credit"]) for r in in_period), Decimal("0"))
        if quantize(dr_total - cr_total, self.base_precision) != 0:
            raise ConsistencyError(f"期间 {period} 借贷发生额不平，禁止关账")

        balances = self.recompute_balances(end)
        bal_payload = {
            k: {"currency": v["currency"], "fx": str(v["fx"]), "base": str(v["base"])}
            for k, v in sorted(balances.items())
        }
        entries_payload = [
            {
                "entry_id": r["id"],
                "voucher_no": r["voucher_no"],
                "date": r["voucher_date"],
                "line": r["line_no"],
                "account": r["account"],
                "hash": r["entry_hash"],
                "adjustment": bool(r["is_adjustment"]),
                "system": bool(r["is_system"]),
            }
            for r in in_period
        ]
        payload_hash = _sha256(_canon({"balances": bal_payload, "entries": entries_payload}))

        generation = (
            self.conn.execute(
                "SELECT COALESCE(MAX(generation),0) g FROM period_snapshot WHERE period=?",
                (period,),
            ).fetchone()["g"]
            + 1
        )
        prev_snap = self.conn.execute(
            "SELECT hash FROM period_snapshot ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_snap_hash = prev_snap["hash"] if prev_snap else None
        closed_at = self._now()
        snap_hash = _sha256(
            "|".join(
                [
                    prev_snap_hash or "",
                    period,
                    str(generation),
                    closed_at,
                    str(len(in_period)),
                    str(dr_total),
                    str(cr_total),
                    payload_hash,
                ]
            )
        )

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO period_snapshot(period,generation,closed_at,entry_count,"
                "dr_total,cr_total,balances_json,entries_json,prev_snapshot_hash,hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    period,
                    generation,
                    closed_at,
                    len(in_period),
                    str(dr_total),
                    str(cr_total),
                    _canon(bal_payload),
                    _canon(entries_payload),
                    prev_snap_hash,
                    snap_hash,
                ),
            )
            snap_id = cur.lastrowid
            self.conn.execute(
                "UPDATE period SET status='closed', ever_closed=1 WHERE code=?", (period,)
            )
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('snapshot_head',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (snap_hash,),
            )
            audit_id = self._audit(
                "period.close",
                period,
                {
                    "generation": generation,
                    "snapshot_id": snap_id,
                    "entry_count": len(in_period),
                    "dr_total": str(dr_total),
                    "cr_total": str(cr_total),
                    "payload_hash": payload_hash,
                    "snapshot_hash": snap_hash,
                },
            )
        return {
            "period": period,
            "generation": generation,
            "entry_count": len(in_period),
            "debit_total": dr_total,
            "credit_total": cr_total,
            "snapshot_hash": snap_hash,
            "audit_id": audit_id,
        }

    def reopen_period(self, period: str, reason: str) -> dict:
        """反结账：打开期间并记录原因。补记将被要求标记为调整凭证。"""
        prow = self._period_row(period)
        if prow is None or prow["status"] != "closed":
            raise ValidationError(f"期间 {period} 不是关账状态，无需反结账")
        if not reason or not reason.strip():
            raise ValidationError("反结账必须填写原因")
        with self.conn:
            self.conn.execute(
                "UPDATE period SET status='open' WHERE code=?", (period,)
            )  # ever_closed 保留为 1
            snap = self.conn.execute(
                "SELECT id FROM period_snapshot WHERE period=? ORDER BY generation DESC LIMIT 1",
                (period,),
            ).fetchone()
            ts = self._now()
            self.conn.execute(
                "UPDATE period_snapshot SET reopened_at=? WHERE id=?", (ts, snap["id"])
            )
            audit_id = self._audit(
                "period.reopen", period, {"reason": reason.strip(), "snapshot_id": snap["id"]}
            )
        return {"period": period, "reason": reason.strip(), "audit_id": audit_id}

    # ---------- 一致性核对 ----------

    @staticmethod
    def _audit_row_hash(r) -> str:
        return _sha256(
            f"{r['prev_hash'] or ''}|{r['ts']}|{r['action']}|{r['period'] or ''}|{r['detail_json']}"
        )

    def _audit_tail(self) -> tuple[object, str | None]:
        """纯计算审计链：逐行验证连续性/哈希，返回 (行集, 末条哈希)。不碰锚点。"""
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        prev = None
        for r in rows:
            if r["prev_hash"] != prev:
                raise ConsistencyError(f"审计记录 {r['id']}（{r['action']}）前链断裂")
            if self._audit_row_hash(r) != r["hash"]:
                raise ConsistencyError(f"审计记录 {r['id']}（{r['action']}）哈希不符")
            prev = r["hash"]
        return rows, prev

    @staticmethod
    def _snapshot_row_hash(r) -> str:
        payload_hash = _sha256(
            _canon(
                {
                    "balances": json.loads(r["balances_json"]),
                    "entries": json.loads(r["entries_json"]),
                }
            )
        )
        return _sha256(
            "|".join(
                [
                    r["prev_snapshot_hash"] or "",
                    r["period"],
                    str(r["generation"]),
                    r["closed_at"],
                    str(r["entry_count"]),
                    r["dr_total"],
                    r["cr_total"],
                    payload_hash,
                ]
            )
        )

    def _snapshot_tail(self) -> tuple[object, str | None]:
        """纯计算快照链：逐代验证连续性/哈希/分录数，返回 (行集, 末代哈希)。不碰锚点。"""
        rows = self.conn.execute("SELECT * FROM period_snapshot ORDER BY id").fetchall()
        prev = None
        for r in rows:
            if r["prev_snapshot_hash"] != prev:
                raise ConsistencyError(
                    f"第 {r['generation']} 代快照（{r['period']}）前链断裂"
                )
            if self._snapshot_row_hash(r) != r["hash"]:
                raise ConsistencyError(
                    f"第 {r['generation']} 代快照（{r['period']}）哈希不符"
                )
            entries = json.loads(r["entries_json"])
            if len(entries) != r["entry_count"]:
                raise ConsistencyError(
                    f"快照 {r['period']}#{r['generation']} 分录数不一致"
                )
            prev = r["hash"]
        return rows, prev

    def _audit_business_evidence(self) -> None:
        """审计链与业务表的交叉证据核对（v1 旧账本迁移时使用）。

        纯 prev_hash 链无法区分"链本来就这么短"与"末条被删除后重新成链"。
        业务凭证/快照只能由审计过的写操作产生，因此用两边集合相等补强：

        * 每条 voucher.post 审计里的凭证号集合 == voucher 表凭证号集合；
        * 每条 period.close 审计登记的 snapshot_id 集合 == 快照表 id 集合；
        * period.reopen / fx.revalue 引用的凭证、快照必须真实存在。

        任一不符说明有审计记录（含末条）被抹掉，迁移必须阻断。
        """
        import json as _json

        posted_nos: set[str] = set()
        closed_ids: set[int] = set()
        rows = self.conn.execute(
            "SELECT id, action, detail_json FROM audit_log ORDER BY id"
        ).fetchall()
        for r in rows:
            try:
                detail = _json.loads(r["detail_json"])
            except (ValueError, TypeError):
                raise ConsistencyError(f"审计记录 {r['id']}（{r['action']}）内容损坏")
            if r["action"] == "voucher.post":
                no = detail.get("voucher_no")
                if not no:
                    raise ConsistencyError(f"审计记录 {r['id']} 缺少凭证号")
                posted_nos.add(no)
            elif r["action"] == "period.close":
                sid = detail.get("snapshot_id")
                if sid is None:
                    raise ConsistencyError(f"审计记录 {r['id']} 缺少快照 id")
                closed_ids.add(int(sid))
            elif r["action"] == "period.reopen":
                sid = detail.get("snapshot_id")
                if sid is None or not self.conn.execute(
                    "SELECT 1 FROM period_snapshot WHERE id=?", (sid,)
                ).fetchone():
                    raise ConsistencyError(
                        f"审计记录 {r['id']} 引用的快照 {sid} 不存在（反结账记录与快照表不符）"
                    )
            elif r["action"] == "fx.revalue":
                for p in detail.get("posted", []):
                    if not self.conn.execute(
                        "SELECT 1 FROM voucher WHERE voucher_no=?", (p.get("voucher_no"),)
                    ).fetchone():
                        raise ConsistencyError(
                            f"审计记录 {r['id']} 引用的系统凭证 {p.get('voucher_no')} 不存在"
                        )

        voucher_nos = {
            r["voucher_no"]
            for r in self.conn.execute("SELECT voucher_no FROM voucher").fetchall()
        }
        if posted_nos != voucher_nos:
            missing = sorted(voucher_nos - posted_nos)
            orphan = sorted(posted_nos - voucher_nos)
            raise ConsistencyError(
                "审计链与凭证表不一致，拒绝迁移："
                + (f"凭证缺审计记录 {missing}；" if missing else "")
                + (f"审计记录指向不存在的凭证 {orphan}" if orphan else "")
            )
        snapshot_ids = {
            r["id"] for r in self.conn.execute("SELECT id FROM period_snapshot").fetchall()
        }
        if closed_ids != snapshot_ids:
            raise ConsistencyError(
                "审计链关账记录与快照表不一致（疑似末条关账审计被删除），拒绝迁移："
                f"审计快照集 {sorted(closed_ids)}，快照表 {sorted(snapshot_ids)}"
            )

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _ensure_anchors(self) -> dict:
        """保证两条链都有链头锚点；v1 旧账本在这里做一次性安全迁移。

        规则：
        * 锚点已存在 → 用它严格比对现存末条（锚点指向的记录不在库中即判定
          末条被删），不符则抛 :class:`ConsistencyError`，账本阻断；
        * 锚点缺失但 schema_version>=2（迁移后账本）→ 视为锚点被人为抹掉，阻断；
        * 锚点缺失且没有版本标记（v1 旧账本）→ 先纯计算证明整条链连续：
          能证明就在同一事务内补写锚点并留痕 ``schema.migrate``；不能证明则
          阻断，绝不写入锚点——锚点只记录"链的真实终点"，不能掩盖断裂。
        """
        migrated: list[str] = []
        legacy = self._meta("schema_version") is None

        def is_legacy_chain(head_key: str) -> bool:
            return self._meta(head_key) is None and legacy

        def adopt(head_key: str, tail: str):
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (head_key, tail),
            )

        with self.conn:
            # v1 旧账本：先用业务表交叉证据证明"没有审计记录被抹掉"，
            # 再证明两条哈希链连续；任一不过直接阻断，绝不写入锚点。
            if legacy:
                self._audit_business_evidence()

            # 审计链
            audit_rows, audit_tail = self._audit_tail()
            audit_anchor = self._meta("audit_head")
            if audit_anchor is not None:
                self._check_anchor("audit_head", audit_anchor, audit_rows, audit_tail,
                                   kind="audit")
            elif is_legacy_chain("audit_head"):
                if audit_rows:
                    adopt("audit_head", audit_tail)
                    migrated.append(f"audit_head={audit_tail[:12]}…")
            elif audit_rows:
                # 已是 v2、链上有记录却没有锚点：只可能是锚点被人为抹掉
                raise ConsistencyError(
                    "已迁移账本缺少 audit_head 链头锚点，疑似被人为删除，拒绝继续"
                )
            # 无锚点且链为空 = 尚未发生写入的全新账本，合法

            # 快照链（用同一事务，保证两条链一起迁移或一起不动）
            snap_rows, snap_tail = self._snapshot_tail()
            snap_anchor = self._meta("snapshot_head")
            if snap_anchor is not None:
                self._check_anchor("snapshot_head", snap_anchor, snap_rows, snap_tail,
                                   kind="snapshot")
            elif is_legacy_chain("snapshot_head"):
                if snap_rows:
                    adopt("snapshot_head", snap_tail)
                    migrated.append(f"snapshot_head={snap_tail[:12]}…")
            elif snap_rows:
                raise ConsistencyError(
                    "已迁移账本缺少 snapshot_head 链头锚点，疑似被人为删除，拒绝继续"
                )

            if legacy:
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (self.SCHEMA_VERSION,),
                )
            if migrated:
                # 有旧链可继承：写一条迁移留痕（成为新链尾），再把审计锚点指向它
                self._audit(
                    "schema.migrate",
                    None,
                    {"from": "1", "to": self.SCHEMA_VERSION, "anchors": migrated},
                )
                new_tail = self.conn.execute(
                    "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
                ).fetchone()["hash"]
                adopt("audit_head", new_tail)
        return {"migrated": bool(migrated), "legacy": legacy, "anchors": migrated}

    def _check_anchor(self, key: str, anchor, rows, tail: str | None, *, kind: str) -> None:
        label = "审计记录" if kind == "audit" else "关账快照"
        if not rows:
            raise ConsistencyError(
                f"{label}表为空但链头锚点 {key} 仍存在，{label}疑似被整体删除"
            )
        if anchor is None:
            raise ConsistencyError(f"{kind} 链头锚点 {key} 缺失但链上存在 {label}，拒绝继续")
        if anchor != tail:
            referenced = self.conn.execute(
                "SELECT id FROM audit_log WHERE hash=?" if kind == "audit"
                else "SELECT id FROM period_snapshot WHERE hash=?",
                (anchor,),
            ).fetchone()
            hint = "（锚点指向的记录不在库中，最后一条记录疑似被删除）" if referenced is None else ""
            raise ConsistencyError(f"{kind} 链头锚点与现存末条记录不符{hint}")

    def verify_entry_hashes(self) -> None:
        rows = self.conn.execute(
            "SELECT e.*, v.voucher_no, v.voucher_date, v.is_adjustment, v.is_system "
            "FROM entry e JOIN voucher v ON v.id=e.voucher_id ORDER BY e.id"
        ).fetchall()
        for r in rows:
            eh = _sha256(
                "|".join(
                    [
                        r["voucher_no"],
                        r["voucher_date"],
                        str(r["is_adjustment"]),
                        str(r["is_system"]),
                        str(r["line_no"]),
                        r["account"],
                        r["currency"],
                        r["debit"],
                        r["credit"],
                        r["base_debit"],
                        r["base_credit"],
                        r["rate"] or "",
                    ]
                )
            )
            if eh != r["entry_hash"]:
                raise ConsistencyError(
                    f"分录 {r['id']}（凭证 {r['voucher_no']} 第 {r['line_no']} 行）哈希不符，"
                    "数据可能被直接篡改",
                )

    def verify_balances(self) -> None:
        """余额表与分录重算逐账户核对（含外币与本位币）。"""
        rows = self.conn.execute(
            "SELECT account, currency, fx_amount, base_amount FROM ledger_balance"
        ).fetchall()
        stored = {
            r["account"]: (r["currency"], D(r["fx_amount"]), D(r["base_amount"])) for r in rows
        }
        recomputed = self.recompute_balances()
        accounts = set(stored) | set(recomputed)
        for acc in sorted(accounts):
            s = stored.get(acc)
            r = recomputed.get(acc)
            if s is None:
                # 重算有值而余额表无行：只在值非零时才是问题
                if r["fx"] != 0 or r["base"] != 0:
                    raise ConsistencyError(f"账户 {acc} 余额表缺失，重算为 {r}")
                continue
            if r is None:
                if s[1] != 0 or s[2] != 0:
                    raise ConsistencyError(f"账户 {acc} 余额表有余额但分录重算为 0")
                continue
            if s[0] != r["currency"] or s[1] != r["fx"] or s[2] != r["base"]:
                raise ConsistencyError(
                    f"账户 {acc} 余额不一致：账面 {s[1]} {s[0]} / {s[2]} {self.base_currency}，"
                    f"重算 {r['fx']} / {r['base']}"
                )

    def verify_audit_chain(self) -> None:
        # 锚点由打开账本时的 _ensure_anchors 建立/校验；这里再做一次严格比对，
        # 覆盖"本次会话打开后锚点才被外部破坏"的情形。
        rows, tail = self._audit_tail()
        anchor = self._meta("audit_head")
        if not rows:
            if anchor is not None:
                raise ConsistencyError("审计表为空但链头锚点仍存在，审计记录疑似被整体删除")
            return  # 空链无尾可丢
        self._check_anchor("audit_head", anchor, rows, tail, kind="audit")

    def verify_snapshots(self) -> None:
        rows, tail = self._snapshot_tail()
        anchor = self._meta("snapshot_head")
        if rows and anchor is None:
            raise ConsistencyError("快照链头锚点缺失，关账快照可能被删除")
        if rows:
            self._check_anchor("snapshot_head", anchor, rows, tail, kind="snapshot")
        elif anchor is not None:
            raise ConsistencyError("快照表为空但链头锚点仍存在，快照疑似被整体删除")

    def verify_latest_snapshot_matches_books(self, period: str) -> dict:
        """再关账场景核对：指定期间最新一代快照必须与当前账上分录/余额完全一致。"""
        r = self.conn.execute(
            "SELECT * FROM period_snapshot WHERE period=? ORDER BY generation DESC LIMIT 1",
            (period,),
        ).fetchone()
        if r is None:
            raise NotFoundError(f"期间 {period} 没有快照")
        end = self._period_end(period)
        balances = self.recompute_balances(end)
        bal_payload = {
            k: {"currency": v["currency"], "fx": str(v["fx"]), "base": str(v["base"])}
            for k, v in sorted(balances.items())
        }
        rows = self._entries_through(end)
        in_period = [x for x in rows if x["voucher_date"][:7] == period]
        entries_payload = [
            {
                "entry_id": x["id"],
                "voucher_no": x["voucher_no"],
                "date": x["voucher_date"],
                "line": x["line_no"],
                "account": x["account"],
                "hash": x["entry_hash"],
                "adjustment": bool(x["is_adjustment"]),
                "system": bool(x["is_system"]),
            }
            for x in in_period
        ]
        snap_bal = json.loads(r["balances_json"])
        snap_ent = json.loads(r["entries_json"])
        if snap_bal != bal_payload:
            raise ConsistencyError(f"期间 {period} 最新快照余额与当前账面不符")
        if snap_ent != entries_payload:
            raise ConsistencyError(f"期间 {period} 最新快照分录（含调整标记）与当前账面不符")
        return {"period": period, "generation": r["generation"], "matched": True}

    def verify_all(self) -> dict:
        self.verify_entry_hashes()
        self.verify_balances()
        self._audit_business_evidence()
        self.verify_audit_chain()
        self.verify_snapshots()
        eq = self.accounting_equation()
        if not eq["balanced"]:
            raise ConsistencyError(f"会计恒等式不平：差额 {eq['difference']}")
        return {
            "entries": self.conn.execute("SELECT COUNT(*) c FROM entry").fetchone()["c"],
            "vouchers": self.conn.execute("SELECT COUNT(*) c FROM voucher").fetchone()["c"],
            "audit": self.conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"],
            "snapshots": self.conn.execute("SELECT COUNT(*) c FROM period_snapshot").fetchone()[
                "c"
            ],
            "equation": eq,
        }

    # ---------- 查询辅助 ----------

    def list_vouchers(self, period: str | None = None) -> list[dict]:
        sql = (
            "SELECT v.*, e.base_debit bdr, e.base_credit bcr "
            "FROM voucher v JOIN entry e ON e.voucher_id=v.id"
        )
        args = ()
        if period:
            sql += " WHERE v.period=?"
            args = (period,)
        sql += " ORDER BY v.voucher_date, v.id, e.line_no"
        agg: dict[int, dict] = {}
        for r in self.conn.execute(sql, args).fetchall():
            x = agg.setdefault(
                r["id"],
                {"id": r["id"], "voucher_no": r["voucher_no"], "voucher_date": r["voucher_date"],
                 "period": r["period"], "memo": r["memo"], "is_adjustment": r["is_adjustment"],
                 "is_system": r["is_system"], "created_at": r["created_at"],
                 "base_total": Decimal("0")},
            )
            x["base_total"] += D(r["bdr"])
        return list(agg.values())

    def get_voucher(self, voucher_no: str) -> dict:
        v = self.conn.execute("SELECT * FROM voucher WHERE voucher_no=?", (voucher_no,)).fetchone()
        if v is None:
            raise NotFoundError(f"凭证不存在: {voucher_no}")
        entries = self.conn.execute(
            "SELECT * FROM entry WHERE voucher_id=? ORDER BY line_no", (v["id"],)
        ).fetchall()
        return {"voucher": dict(v), "entries": [dict(e) for e in entries]}

    def list_periods(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM period ORDER BY code")]

    def audit_trail(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id,ts,action,period,detail_json FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
