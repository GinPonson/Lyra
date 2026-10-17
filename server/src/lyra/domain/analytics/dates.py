"""分析层的日期与账户范围规则。

日期一律按 **UTC 日历日**处理，且解析必须是严格的：JavaScript 的 ``Date.UTC``
会把 ``02/30`` 自动进位到三月，Python 的 ``datetime.date`` 则会直接报错，
两者行为本已不同；这里统一采用"非法日期必须失败"的语义并显式校验。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .types import AccountScope

# 一天的秒数；与 TypeScript 侧同为"日历日"语义。
SECONDS_PER_DAY = 86_400

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_iso_date(value: str) -> date:
    """严格解析 ISO 日历日期。

    @param value: ``YYYY-MM-DD``。
    @returns 可做日期算术的 ``date``。
    @raises ValueError: 格式或实际日历日期非法。
    """
    match = _ISO_DATE.fullmatch(value)
    if match is None:
        raise ValueError(f"无法解析日期: {value}")
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError as error:
        raise ValueError(f"无法解析日期: {value}") from error


def format_iso_date(value: date) -> str:
    """把日期格式化为 ISO 文本。"""
    return value.isoformat()


def shift_days(value: date, days: int) -> date:
    """按日历日平移，对应 TypeScript 的 ``setUTCDate`` 语义。"""
    return value + timedelta(days=days)


def days_between(later: date, earlier: date) -> int:
    """返回两个日历日之间的整数天数差。"""
    return (later - earlier).days


def validate_date_range(from_date: str | None, to_date: str | None) -> None:
    """校验可选日期范围，确保起始日不晚于截止日。

    @raises ValueError: 任一端非法，或起始日晚于截止日。
    """
    if from_date is not None:
        parse_iso_date(from_date)
    if to_date is not None:
        parse_iso_date(to_date)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError("起始日期不能晚于截止日期")


def is_date_in_range(value: str, from_date: str | None = None, to_date: str | None = None) -> bool:
    """判断 ISO 日期是否位于可选闭区间内。"""
    return (
        (from_date is None or value >= from_date)
        and (to_date is None or value <= to_date)
    )


def is_account_in_scope(account: str, scope: AccountScope) -> bool:
    """判断一条账户数据是否属于调用方显式指定的范围。"""
    return scope.kind == "all" or scope.account == account


def all_accounts_scope() -> AccountScope:
    """构造"全部账户"范围。

    仅用于内部调用点（例如工具运行时已把账户范围解析完毕后），业务入口不应
    直接使用它来省略范围。
    """
    return AccountScope(kind="all")
