"""分析层的差分对照测试。

期望值来自 oracle（`schwab-cli`）在 19 份账本上运行 16 条命令的真实输出。
本文件按命令逐批覆盖：只有实现完成并验证过的命令才纳入比对，未实现的命令
由 :func:`test_unimplemented_commands_are_explicit` 显式列出，避免"看起来全绿"
但实际大半没做。
"""

from __future__ import annotations

import hashlib
import re

import pytest
from parity_support import analytics_cases, compare_structure, to_plain

from lyra.domain.analytics.audit import audit_ledger
from lyra.domain.analytics.dates import all_accounts_scope
from lyra.domain.analytics.performance import (
    calculate_allocation,
    calculate_attribution,
    calculate_monthly_performance,
)
from lyra.domain.analytics.positions import (
    aggregate_positions,
    analyze_expiring_positions,
)
from lyra.domain.analytics.queries import query_cashflow, query_realized, query_trades
from lyra.domain.analytics.risk import calculate_drawdown, calculate_option_risk
from lyra.domain.analytics.summary import calculate_account_summary
from lyra.domain.analytics.trade_analysis import (
    calculate_extreme_trades,
    calculate_holding_period,
    calculate_option_stress,
    calculate_streaks,
)
from lyra.domain.analytics.types import AccountScope
from lyra.domain.fifo.replay import replay_fifo
from lyra.domain.ledger.batch import normalize_transaction_batch
from lyra.infrastructure.imports.schwab_csv import parse_schwab_csv

# oracle 录制分析层时使用的固定基准日与窗口，必须与录制器一致。
ANALYTICS_AS_OF = "2026-09-01"
EXPIRING_DAYS = 30

# 已经实现并纳入对照的命令；每完成一条就在这里加一条。
IMPLEMENTED_COMMANDS: frozenset[str] = frozenset({
    "summary",
    "positions",
    "expiring",
    "monthly",
    "allocation",
    "attribution",
    "drawdown",
    "risk",
    "holding-period",
    "extremes",
    "streaks",
    "stress",
    "trades",
    "realized",
    "cashflow",
    "audit",
})

# 每条命令至少要比对到的叶字段数，防止空比对假通过。
MINIMUM_LEAVES: dict[str, int] = {
    "summary": 8,
    "positions": 5,
    "expiring": 5,
    "monthly": 4,
    "allocation": 4,
    "attribution": 4,
    "drawdown": 4,
    "risk": 4,
    "holding-period": 4,
    "extremes": 6,
    "streaks": 3,
    "stress": 4,
    "trades": 8,
    "realized": 6,
    "cashflow": 2,
    "audit": 5,
}

# 已知差异白名单：每条都必须写明原因。命中为 0 的条目视为死条目，应删除。
KNOWN_DIFFERENCES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^audit\.scope_note$"),
        "oracle 写「本命令」、现有实现写「本功能」；仅人读文案，迁移保持现有措辞",
    ),
)

ALL_COMMANDS: frozenset[str] = frozenset({
    "summary", "positions", "expiring", "trades", "realized", "cashflow",
    "monthly", "allocation", "attribution", "drawdown", "risk",
    "holding-period", "extremes", "streaks", "stress", "audit",
})


def _ledger(case: dict[str, object]):
    """解析一份账本语料，返回标准交易与 FIFO 结果。"""
    case_input = case["input"]
    assert isinstance(case_input, dict)
    content = str(case_input["content"]).encode("utf-8")
    file_name = str(case_input["fileName"])
    source_hash = hashlib.sha256(content).hexdigest()
    batch = parse_schwab_csv(
        content,
        file_name=file_name,
        source_hash=source_hash,
        source_reference=source_hash,
    )
    transactions = normalize_transaction_batch(batch).transactions
    return transactions, replay_fifo(transactions)


def _run_command(case: dict[str, object], command: str) -> object:
    """在当前 Python 实现上执行一条命令，返回与 oracle 键名对齐的结构。"""
    transactions, fifo = _ledger(case)
    scope: AccountScope = all_accounts_scope()

    if command == "summary":
        return calculate_account_summary(transactions, fifo, scope)
    if command == "positions":
        rows = aggregate_positions(fifo.open_lots, scope)
        return {"rows": list(rows), "count": len(rows)}
    if command == "expiring":
        positions = aggregate_positions(fifo.open_lots, scope)
        return analyze_expiring_positions(positions, ANALYTICS_AS_OF, EXPIRING_DAYS)
    if command == "monthly":
        return calculate_monthly_performance(fifo.realized_records, scope)
    if command == "allocation":
        # oracle 录制时使用 --group-by underlying。
        return calculate_allocation(fifo.open_lots, scope, "underlying")
    if command == "attribution":
        # oracle 录制时使用 --group-by underlying。
        return calculate_attribution(fifo.realized_records, scope, "underlying")
    if command == "drawdown":
        return calculate_drawdown(fifo.realized_records, scope)
    if command == "risk":
        # oracle 录制时使用 --as-of 2026-09-01。
        return calculate_option_risk(fifo.open_lots, scope, ANALYTICS_AS_OF)
    if command == "holding-period":
        return calculate_holding_period(fifo.realized_records, scope)
    if command == "extremes":
        # oracle 录制时使用 --limit 10。
        return calculate_extreme_trades(fifo.realized_records, scope, 10)
    if command == "streaks":
        return calculate_streaks(fifo.realized_records, scope)
    if command == "stress":
        return calculate_option_stress(fifo.open_lots, scope)
    if command == "trades":
        return query_trades(transactions, scope)
    if command == "realized":
        return query_realized(fifo.realized_records, scope)
    if command == "cashflow":
        return query_cashflow(transactions, scope)
    if command == "audit":
        return audit_ledger(transactions, fifo, scope)
    raise ValueError(f"未实现的分析命令: {command}")


def test_analytics_corpus_is_present() -> None:
    """确认分析层语料存在且覆盖多份账本。"""
    cases = analytics_cases()
    assert len(cases) >= 15, f"分析层语料过少：{len(cases)}"
    for case in cases:
        analytics = case["expected"]["analytics"]  # type: ignore[index]
        assert set(analytics) == ALL_COMMANDS, f"命令集合不符: {case['caseId']}"


def test_unimplemented_commands_are_explicit() -> None:
    """未实现的命令必须在测试输出中可见，而不是静默缺席。"""
    pending = sorted(ALL_COMMANDS - IMPLEMENTED_COMMANDS)
    print(f"[analytics] 已实现 {len(IMPLEMENTED_COMMANDS)}/{len(ALL_COMMANDS)}；待实现: {pending}")
    assert IMPLEMENTED_COMMANDS <= ALL_COMMANDS


@pytest.mark.parametrize("command", sorted(IMPLEMENTED_COMMANDS))
def test_analytics_command_matches_oracle(command: str) -> None:
    """逐份账本比对一条命令，并在结束时汇总覆盖度与白名单命中。"""
    compared_total = 0
    whitelist_hits: list[str] = []
    for case in analytics_cases():
        expected = case["expected"]["analytics"][command]  # type: ignore[index]
        actual = _run_command(case, command)
        mismatches, compared, whitelisted = compare_structure(
            expected,
            to_plain(actual),
            minimum_leaves=MINIMUM_LEAVES.get(command, 1),
            label_prefix=f"{command}",
            ignored_paths=tuple(pattern for pattern, _ in KNOWN_DIFFERENCES),
        )
        assert not mismatches, (
            f"[{case['caseId']}] {command} 口径不一致（{len(mismatches)} 处）\n  "
            + "\n  ".join(mismatches[:6])
        )
        compared_total += compared
        whitelist_hits.extend(whitelisted)
    assert compared_total > 0, f"{command} 没有比对到任何字段"
    print(
        f"[analytics] {command}: 19 份账本共比对 {compared_total} 个字段"
        + (f"；白名单命中 {sorted(set(whitelist_hits))}" if whitelist_hits else "")
    )


def test_analytics_whitelist_entries_are_live() -> None:
    """白名单条目必须真实命中；否则就是死条目，应当删除。"""
    hit_paths: set[str] = set()
    for case in analytics_cases():
        for pattern, _ in KNOWN_DIFFERENCES:
            expected = case["expected"]["analytics"]["audit"]  # type: ignore[index]
            actual = _run_command(case, "audit")
            _, _, whitelisted = compare_structure(
                expected,
                to_plain(actual),
                minimum_leaves=MINIMUM_LEAVES["audit"],
                label_prefix="audit",
                ignored_paths=(pattern,),
            )
            hit_paths.update(whitelisted)
    assert hit_paths, "分析层白名单没有任何命中：说明是死条目，请删除"
    print(f"[analytics] 白名单命中路径: {sorted(hit_paths)}")
