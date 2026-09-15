"""金额与币种精度处理。

所有金额一律使用 :class:`decimal.Decimal`，按币种的小数位做 ROUND_HALF_UP
（会计常用的四舍五入）。金额单位为 10**-precision，可做精确比较。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, localcontext


def D(value) -> Decimal:
    """宽松地把字符串/int/Decimal 转成 Decimal，拒绝 float 以免二进制误差混入。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("金额不允许使用 float，请用 str 或 Decimal 传入")
    try:
        return Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"无法解析金额: {value!r}") from exc


def quantize(amount: Decimal, precision: int) -> Decimal:
    """按币种精度四舍五入。precision 为小数位数（如日元=0、人民币=2）。"""
    if precision < 0 or precision > 18:
        raise ValueError(f"币种精度超出范围(0..18): {precision}")
    quantum = Decimal(1).scaleb(-precision)
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def is_quantized(amount: Decimal, precision: int) -> bool:
    return amount == quantize(amount, precision)


def rate_to_decimal(value) -> Decimal:
    """汇率解析，保留完整有效位（汇率本身不按币种精度截断）。"""
    rate = D(value)
    if rate <= 0:
        raise ValueError("汇率必须为正数")
    return rate


def convert(
    amount: Decimal, rate: Decimal, target_precision: int
) -> Decimal:
    """外币金额 × 汇率后按目标币种（本位币）精度取整。"""
    with localcontext() as ctx:
        ctx.prec = 40
        return quantize(amount * rate, target_precision)
