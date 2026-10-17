"""FIFO 重放的差分对照测试。

期望值来自 oracle（`schwab-cli`）的真实执行结果；与 TypeScript 侧 harness
使用同一批语料、同一套比较规则。单文件重放的语料才纳入对照，多文件重放
涉及累计账本状态，不在本轮范围内。
"""

from __future__ import annotations

import hashlib

import pytest
from parity_support import (
    canonical,
    cases_of_kind,
    expected_error_text,
    normalize_message,
)

from lyra.domain.fifo.replay import replay_fifo
from lyra.domain.fifo.types import FifoError
from lyra.domain.ledger.batch import normalize_transaction_batch
from lyra.infrastructure.imports.schwab_csv import parse_schwab_csv


def _run(case: dict[str, object]) -> tuple[list[list[str | None]], list[list[str | None]]]:
    """用 Python 领域层重放一条 fifo 语料，返回 oracle 列序的 lots 与 realized。"""
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
    result = replay_fifo(normalize_transaction_batch(batch).transactions)

    lots = [
        [
            lot.account,
            lot.symbol_key,
            lot.asset_type,
            lot.underlying,
            lot.expiry,
            None if lot.strike is None else str(lot.strike),
            lot.option_type,
            lot.direction,
            lot.open_date,
            str(lot.open_price),
            str(lot.open_quantity),
            str(lot.remaining_quantity),
            str(lot.open_fees),
            lot.open_transaction,
        ]
        for lot in result.open_lots
    ]
    realized = [
        [
            record.account,
            record.symbol_key,
            record.asset_type,
            record.underlying,
            record.direction,
            record.open_date,
            record.close_date,
            str(record.quantity),
            str(record.open_price),
            str(record.close_price),
            str(record.open_fees),
            str(record.close_fees),
            format(record.pnl, "f"),
            record.close_action,
            record.open_transaction,
            record.close_transaction,
        ]
        for record in result.realized_records
    ]
    return lots, realized


def _oracle_open_lots(case: dict[str, object]) -> list[list[str | None]]:
    """取出 oracle 的未平批次。

    oracle 的 lots 表保留已完全平仓的批次（remaining_qty = 0），而现有实现
    从不物化这类行（出队 + 数据库约束保证），因此只比对未平批次。
    """
    return [
        list(row)
        for row in case["expected"]["lots"]  # type: ignore[index]
        if row[11] is not None and row[11] not in ("0", "0.000000")
    ]


@pytest.mark.parametrize("case", cases_of_kind("fifo"), ids=str)
def test_fifo_replay_matches_oracle(case: dict[str, object]) -> None:
    """每条 fifo 语料的 lots 与 realized 必须与 oracle 逐字段一致。"""
    expected_error = case["expectedError"]

    if expected_error is not None:
        expected = expected_error_text(case)
        with pytest.raises(FifoError) as raised:
            _run(case)
        assert normalize_message(str(raised.value)) == normalize_message(expected), (
            f"失败原因不同\n  oracle: {expected}\n  python: {raised.value}"
        )
        return

    lots, realized = _run(case)
    expected_lots = _oracle_open_lots(case)
    expected_realized = [list(row) for row in case["expected"]["realized"]]  # type: ignore[index]

    assert canonical(lots) == canonical(expected_lots), (
        "lots 不一致\n"
        f"  仅 Python 有: {[row for row in canonical(lots) if row not in canonical(expected_lots)][:2]}\n"
        f"  仅 oracle 有: {[row for row in canonical(expected_lots) if row not in canonical(lots)][:2]}"
    )
    assert canonical(realized) == canonical(expected_realized), (
        "realized 不一致\n"
        f"  仅 Python 有: {[row for row in canonical(realized) if row not in canonical(expected_realized)][:2]}\n"
        f"  仅 oracle 有: {[row for row in canonical(expected_realized) if row not in canonical(realized)][:2]}"
    )


def test_fifo_corpus_covers_multiple_ledgers() -> None:
    """确认 fifo 语料确实覆盖多份账本，避免对照退化为单例。"""
    cases = cases_of_kind("fifo")
    assert len(cases) >= 8, f"fifo 语料过少：{len(cases)}"


def test_fifo_realized_pnl_matches_oracle_in_aggregate() -> None:
    """跨语料核对已实现损益合计，防止个别用例通过而整体口径偏移。"""
    compared = 0
    for case in cases_of_kind("fifo"):
        if case["expectedError"] is not None:
            continue
        _, realized = _run(case)
        expected = [list(row) for row in case["expected"]["realized"]]  # type: ignore[index]
        assert canonical(realized) == canonical(expected), f"合计不一致: {case['caseId']}"
        compared += 1
    assert compared >= 8, f"参与比对的语料过少：{compared}"
