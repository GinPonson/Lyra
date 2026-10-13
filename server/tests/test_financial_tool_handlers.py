"""工具处理器的详情命令覆盖测试。

**这条测试的由来**：静态检查（`ruff` 的 F821）发现 ``handle_ledger_detail`` 的
``audit`` 分支引用了一个**未导入的名字** ``project_ledger_audit``——该分支一旦被调用
就会抛 ``NameError``。

而它此前**逃过了 2107 项测试**，因为 ``handle_ledger_detail`` 完全没有测试覆盖
（``audit`` 只被分析层的测试覆盖过，那是另一条代码路径）。

因此这里**遍历全部详情命令**，确保每个分支都被真正执行一次。这类结构性测试的价值在于
它能抓住"某个分支引用了不存在的东西"——那是逐分支写用例时最容易漏的一类。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lyra.application.financial_tool_handlers import (
    LEDGER_DETAIL_COMMANDS,
    handle_ledger_detail,
)
from lyra.domain.analytics.types import AccountScope
from lyra.domain.ledger.types import ParsedTransaction
from lyra.infrastructure.db.ledger_repository import LedgerAccount, LedgerSnapshot

ACCOUNT = LedgerAccount(
    id="11111111-1111-1111-1111-111111111111",
    legacy_account_key="TST001", base_currency="USD",
)


def _transaction(symbol: str = "INTC") -> ParsedTransaction:
    """构造一笔买入。"""
    return ParsedTransaction(
        account="TST001", source_type="csv", source_reference="f.csv",
        record_date="2026-01-05", transaction_date="2026-01-05", action="Buy",
        raw_symbol=symbol, description="INTEL CORP", asset_type="stock",
        underlying=symbol, expiry=None, strike=None, option_type=None,
        quantity=Decimal(10), price=Decimal(40), fees=Decimal(0),
        amount=Decimal(-400), source_hash="h" * 64, sequence=1, occurrence=1,
        business_hash="b" * 64, transaction_hash=f"t-{symbol}",
    )


def _snapshot() -> LedgerSnapshot:
    """构造含一笔交易的快照。"""
    return LedgerSnapshot(
        accounts=(ACCOUNT,), transactions=(_transaction(),),
        open_lots=(), realized_records=(),
    )


def _arguments(command: str) -> dict:
    """按命令给出**必需的参数**。

    ``expiring`` 必须带 ``asOf``：缺参考日期时"即将到期"没有意义，实现会直接
    ``KeyError``——那是刻意的，不是遗漏。
    """
    return {"asOf": "2026-06-30"} if command == "expiring" else {}


@pytest.mark.parametrize("command", sorted(LEDGER_DETAIL_COMMANDS))
def test_every_detail_command_runs(command: str) -> None:
    """**每条详情命令都能被真正执行一次。**

    断言的重点不是返回值内容，而是**它没有抛异常**——未导入的名字、拼错的函数、
    参数不匹配都会在这里暴露。
    """
    result = handle_ledger_detail(
        command, _snapshot(), AccountScope(kind="all"), _arguments(command)
    )

    assert isinstance(result, dict)


@pytest.mark.parametrize("command", sorted(LEDGER_DETAIL_COMMANDS))
def test_every_detail_command_returns_camel_case_keys(command: str) -> None:
    """公开响应不含 snake_case 键（递归检查）。"""
    result = handle_ledger_detail(
        command, _snapshot(), AccountScope(kind="all"), _arguments(command)
    )

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert "_" not in key, f"{command}: 键 {key!r} 含下划线"
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(result)


def test_unknown_detail_command_is_rejected() -> None:
    """未注册的命令名报错，而不是返回空结果。"""
    with pytest.raises(KeyError, match="未注册的详情命令"):
        handle_ledger_detail("nope", _snapshot(), AccountScope(kind="all"), {})


def test_expiring_requires_a_reference_date() -> None:
    """**``expiring`` 缺 ``asOf`` 时报错。**

    缺参考日期时"即将到期"没有意义——静默用一个默认日期会让用户以为系统算的是
    他想要的那个时点。
    """
    with pytest.raises(KeyError):
        handle_ledger_detail(
            "expiring", _snapshot(), AccountScope(kind="all"), {}
        )


def test_the_audit_command_projects_ledger_audit() -> None:
    """``audit`` 分支调用的是领域函数与投影函数（此前引用了一个未导入的名字）。"""
    result = handle_ledger_detail(
        "audit", _snapshot(), AccountScope(kind="all"), {}
    )

    assert isinstance(result, dict)
    # 该命令把领域结果直接返回（不套币种信封）。
    assert result


def test_the_command_set_matches_the_documented_seven() -> None:
    """详情命令集合是闭集：新增命令时必须显式登记。"""
    assert LEDGER_DETAIL_COMMANDS == frozenset(
        {"positions", "expiring", "trades", "realized", "cashflow", "audit", "extremes"}
    )


def test_market_positions_uses_account_id_not_the_legacy_key() -> None:
    """**持仓响应用 ``accountId``（内部 UUID），不是 ``account``（legacy 券商标识）。**

    两者在**字段名与取值**上都不同：

    - TS 的 ``PublicPosition`` 用 ``accountId``；
    - 既有的 ``project_position`` 也做同样的脱敏映射（legacy 键 → UUID）。

    直接输出 legacy 键会泄漏完整账户标识，而客户端按 ``accountId`` 取值会读到
    ``undefined``——**而且不会报错**。
    """
    from lyra.application.public_projection import project_market_positions

    # 直接验证投影：投影是字段名的唯一决定处，因此不必绕经数据库。

    projected = project_market_positions(
        [{
            "account": "TST001", "symbol_key": "INTC", "asset_type": "stock",
            "underlying": "INTC", "expiry": None, "strike": None, "option_type": None,
            "direction": "long", "quantity": "10", "cost": "400",
            "first_open_date": "2026-01-05",
            "valuation": {
                "marketValue": None, "unrealizedPnl": None,
                "multiplier": "1", "basis": "reference_price_excluding_fees",
            },
            "quote": None,
        }],
        None,
        {"TST001": "11111111-1111-1111-1111-111111111111"},
    )

    row = projected["rows"][0]
    assert row["accountId"] == "11111111-1111-1111-1111-111111111111"
    assert "account" not in row
    # legacy 键不得出现在响应里。
    assert "TST001" not in str(projected)


def test_market_positions_fails_rather_than_leaking_an_unauthorized_account() -> None:
    """账户不在映射范围内时报错——**宁可失败也不泄漏未授权账户**。"""
    from lyra.application.public_projection import project_market_positions

    with pytest.raises(KeyError):
        project_market_positions(
            [{
                "account": "OTHER999", "symbol_key": "INTC", "asset_type": "stock",
                "underlying": "INTC", "expiry": None, "strike": None,
                "option_type": None, "direction": "long", "quantity": "10",
                "cost": "400", "first_open_date": "2026-01-05",
                "valuation": {"marketValue": None, "unrealizedPnl": None,
                              "multiplier": "1", "basis": "b"},
                "quote": None,
            }],
            None,
            {"TST001": "11111111-1111-1111-1111-111111111111"},
        )
