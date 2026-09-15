"""业务错误类型。错误必须能定位到具体分录时使用 :class:`LineError`。"""

from __future__ import annotations


class LedgerError(Exception):
    """所有可预期的业务错误基类。"""

    code = "E_GENERAL"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class ValidationError(LedgerError):
    """凭证级错误（关账期、整体不平、配置缺失等）。"""

    code = "E_VALIDATION"


class LineError(LedgerError):
    """可定位到具体分录行的错误。line 为分录序号（从 1 开始）。"""

    code = "E_LINE"

    def __init__(self, line: int, message: str, *, code: str | None = "E_LINE"):
        super().__init__(f"分录 {line}: {message}", code=code)
        self.line = line
        self.line_message = message


class PeriodClosedError(LedgerError):
    code = "E_PERIOD_CLOSED"


class NotFoundError(LedgerError):
    code = "E_NOT_FOUND"


class ConsistencyError(LedgerError):
    """重算结果与存储余额/审计链不一致，或检测到篡改。"""

    code = "E_CONSISTENCY"


class ConfigurationError(LedgerError):
    code = "E_CONFIG"
