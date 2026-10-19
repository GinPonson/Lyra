"""Schwab 字段解析。

镜像 TypeScript 版 ``parsers.ts``。这里的每条规则都直接决定业务哈希，
因此必须逐条对齐，不能"顺手写得更严格或更宽松"。
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .errors import IngestError
from .types import LocalDate, ParsedDateField, ParsedOptionSymbol

# 与 Schwab 期权文本代码一致的格式；标的可含点号但不含空白。
_OPTION_SYMBOL_PATTERN = re.compile(r"^(\S+) (\d{2}/\d{2}/\d{4}) ([0-9]+(?:\.[0-9]+)?) ([CP])$")
# 从导出文件名提取掩码账户标识，例如 ``XXX276``。
_ACCOUNT_PATTERN = re.compile(r"Individual_([^_]+)_")
_MONTH_OR_DAY = re.compile(r"^\d{1,2}$")
_YEAR = re.compile(r"^\d{4}$")


def parse_money(raw: str) -> Decimal | None:
    """解析金额、数量、价格或费用字段。

    与 TypeScript 版一致：去掉美元符号与千位逗号、保留正负号，纯空白返回
    ``None``。无法构造 Decimal 时抛出 :class:`IngestError`，调用方必须中止
    当前文件而不是跳过该字段。

    @param raw: 原始单元格文本。
    @returns 精确 Decimal；空字段返回 ``None``。
    @raises IngestError: 文本不是合法十进制数。
    """
    normalized = raw.strip().replace("$", "").replace(",", "")
    if normalized == "":
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation as error:
        raise IngestError(f"无法解析金额: {json.dumps(raw)}") from error


def parse_date_field(raw: str) -> ParsedDateField:
    """解析 Schwab 的 ``Date`` 列。

    支持 ``MM/DD/YYYY`` 与 ``MM/DD/YYYY as of MM/DD/YYYY``；后者首段是记录
    日期，``as of`` 之后是交易生效日期。

    @param raw: ``Date`` 列原始文本。
    @returns 记录日期与生效日期。
    @raises IngestError: 分段数、格式或日历日期非法。
    """
    text = raw.strip()
    parts = text.split(" as of ")
    if len(parts) > 2:
        raise IngestError(f"无法解析日期: {json.dumps(raw)}")

    record_raw = parts[0]
    transaction_raw = parts[1] if len(parts) > 1 else parts[0]
    try:
        return ParsedDateField(
            record_date=_parse_us_date(record_raw),
            transaction_date=_parse_us_date(transaction_raw),
        )
    except ValueError as error:
        raise IngestError(f"无法解析日期: {json.dumps(raw)}") from error


def parse_option_symbol(symbol: str) -> ParsedOptionSymbol | None:
    """尝试解析 Schwab 文本期权代码。

    普通股票代码不是错误，返回 ``None`` 交由交易动作决定资产类型；只有当
    调用方确认动作必须是期权时，未命中格式才升级为导入错误。

    @param symbol: ``Symbol`` 字段。
    @returns 期权合约字段；非期权格式返回 ``None``。
    @raises IngestError: 命中期权格式但内部字段无法解析。
    """
    match = _OPTION_SYMBOL_PATTERN.fullmatch(symbol.strip())
    if match is None:
        return None

    underlying, expiry_raw, strike_raw, option_type = match.groups()
    if option_type not in ("C", "P"):
        # 正则已保证取值；显式失败可防止规则未来被放宽后带病运行。
        raise IngestError(f"期权 Symbol 格式无法解析: {json.dumps(symbol)}")
    try:
        return ParsedOptionSymbol(
            underlying=underlying,
            expiry=_parse_us_date(expiry_raw),
            strike=Decimal(strike_raw),
            option_type=option_type,
        )
    except (ValueError, InvalidOperation) as error:
        raise IngestError(f"期权 Symbol 格式无法解析: {json.dumps(symbol)}") from error


def parse_account(file_name: str) -> str:
    """从 Schwab 官方导出文件名提取账户标识。

    @param file_name: 仅文件名，不应传入完整路径。
    @returns ``Individual_`` 与下一个下划线之间的账户标识。
    @raises IngestError: 文件名不符合已确认的 Schwab 命名格式。
    """
    match = _ACCOUNT_PATTERN.search(file_name)
    if match is None:
        raise IngestError(
            f"无法从文件名解析账户: {json.dumps(file_name)}(期望形如 Individual_XXX276_...)"
        )
    return match.group(1)


def _parse_us_date(raw: str) -> LocalDate:
    """把美式日历日期转换为无时区 ISO 日期。

    返回文本取自**原始分段**而不是解析出的整数：与 TypeScript 版一样保留
    零填充形式，使 ``1/2/2026`` 与 ``01/02/2026`` 产生同一个哈希。

    @param raw: ``M/D/YYYY`` 或 ``MM/DD/YYYY`` 文本。
    @returns ``YYYY-MM-DD``。
    @raises ValueError: 分段、位数或日历日期非法。
    """
    parts = raw.split("/")
    if len(parts) != 3:
        raise ValueError("invalid US date format")

    month_raw, day_raw, year_raw = parts
    if (
        _MONTH_OR_DAY.fullmatch(month_raw) is None
        or _MONTH_OR_DAY.fullmatch(day_raw) is None
        or _YEAR.fullmatch(year_raw) is None
    ):
        raise ValueError("invalid US date digits")

    # date() 会拒绝 02/30 这类不存在的日期，无需像 JS Date.UTC 那样反向比对。
    date(int(year_raw), int(month_raw), int(day_raw))
    return f"{year_raw}-{month_raw.zfill(2)}-{day_raw.zfill(2)}"
