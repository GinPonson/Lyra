"""Gmail 有界搜索查询测试。

"有界"是核心：查询必须带时间下界与消息数上限，否则一次同步会把邮箱里的全部历史
邮件拉下来——既慢又贵，而且绝大多数与账本无关。

一处细节：**主题必须加引号**，否则含空格的主题会被当成两个独立关键词，
匹配到无关邮件。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.constants import SCHWAB_ECONFIRM_SENDER, SCHWAB_ECONFIRM_SUBJECT
from lyra.infrastructure.gmail.search_query import build_schwab_econfirm_search


def test_builds_a_bounded_query() -> None:
    """查询包含发件人、带引号的主题与时间下界。"""
    plan = build_schwab_econfirm_search("2026-01-05", 50)

    assert f"from:{SCHWAB_ECONFIRM_SENDER}" in plan.query
    # **主题带引号**：不加引号会被当成两个关键词。
    assert f'subject:"{SCHWAB_ECONFIRM_SUBJECT}"' in plan.query
    assert "after:" in plan.query
    assert plan.after_date == "2026-01-05"
    assert plan.maximum_messages == 50


def test_after_uses_epoch_seconds() -> None:
    """时间下界是 Unix 秒，不是毫秒也不是日期文本。"""
    plan = build_schwab_econfirm_search("2026-01-05", 10)
    # 2026-01-05T00:00:00Z = 1767571200
    assert plan.query.endswith("after:1767571200")


def test_epoch_is_floored_not_rounded() -> None:
    """取整方向不影响结果——日期本身就在整秒边界上。"""
    plan = build_schwab_econfirm_search("2026-01-05", 10)
    assert "after:1767571200" in plan.query
    assert "after:1767571200.0" not in plan.query


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "50"])
def test_rejects_invalid_limit(limit: object) -> None:
    """上限必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        build_schwab_econfirm_search("2026-01-05", limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "2026-1-5", "not-a-date", "2026/01/05"])
def test_rejects_invalid_dates(value: str) -> None:
    """日期必须是 ISO 格式。"""
    with pytest.raises(Exception):
        build_schwab_econfirm_search(value, 10)


def test_subject_is_quoted_even_though_it_has_spaces() -> None:
    """主题含空格时必须被引号包住——这是本模块最容易漏的一处。"""
    assert " " in SCHWAB_ECONFIRM_SUBJECT

    plan = build_schwab_econfirm_search("2026-01-05", 10)

    assert f'"{SCHWAB_ECONFIRM_SUBJECT}"' in plan.query
