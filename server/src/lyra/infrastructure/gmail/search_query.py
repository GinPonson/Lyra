"""Gmail 有界搜索查询。

镜像 TypeScript 版 ``infrastructure/gmail/search_query.ts``。

**"有界"是这里的核心**：查询必须带 ``after:`` 时间下界与消息数上限，否则一次同步
会把邮箱里的全部历史邮件拉下来——既慢又贵，而且绝大多数与账本无关。

一处细节：主题**必须加引号**。``Schwab eConfirms`` 含空格，不加引号会被 Gmail 当成
两个独立关键词，从而匹配到主题里分别含 "Schwab" 和 "eConfirms" 的无关邮件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date

from ...domain.analytics.dates import parse_iso_date
from ...domain.gmail.constants import SCHWAB_ECONFIRM_SENDER, SCHWAB_ECONFIRM_SUBJECT


@dataclass(frozen=True)
class GmailBoundedSearchPlan:
    """一次有界扫描的查询与边界。"""

    query: str
    after_date: str
    maximum_messages: int


def build_schwab_econfirm_search(
    after_date: str, maximum_messages: int
) -> GmailBoundedSearchPlan:
    """构造 Schwab eConfirm 邮件的有界搜索查询。

    @param after_date: ISO 日期下界（含该日之后的邮件）。
    @param maximum_messages: 本次最多处理的消息数。
    @returns 查询与边界。
    @raises ValueError: 上限不是正整数，或日期格式非法。
    """
    if (
        not isinstance(maximum_messages, int)
        or isinstance(maximum_messages, bool)
        or maximum_messages <= 0
    ):
        raise ValueError("Gmail 有界扫描上限必须是正安全整数")
    parsed = parse_iso_date(after_date)
    after_epoch_seconds = int(_epoch_millis(parsed) // 1000)
    return GmailBoundedSearchPlan(
        query=(
            f"from:{SCHWAB_ECONFIRM_SENDER} "
            # 主题含空格，**必须加引号**，否则会被当成两个独立关键词。
            f'subject:"{SCHWAB_ECONFIRM_SUBJECT}" '
            f"after:{after_epoch_seconds}"
        ),
        after_date=after_date,
        maximum_messages=maximum_messages,
    )


def _epoch_millis(value: date) -> int:
    """把日期折算为 UTC 零点毫秒。"""
    from datetime import datetime

    return int(
        datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()
        * 1000
    )
