"""账本解析的差分对照测试。

用**同一批 oracle 录制语料**验证 Python 领域层：期望值来自 `schwab-cli`
（oracle）的**真实执行结果**，在迁移期录制并冻结于此。
因此这里不是"自己造数据自己验"，而是与业务口径的原始来源直接对齐。

语料含完整的输入与预期输出，因此
公共部分见 `parity_support.py`。
"""

from __future__ import annotations

import hashlib

import pytest
from parity_support import (
    canonical,
    cases_of_kind,
    expected_error_text,
    load_cases,
    normalize_message,
)

from lyra.domain.ledger.batch import normalize_transaction_batch
from lyra.domain.ledger.errors import IngestError
from lyra.domain.ledger.types import ParsedTransaction
from lyra.infrastructure.imports.schwab_csv import parse_schwab_csv


def _lyra_transaction_rows(
    transactions: tuple[ParsedTransaction, ...],
) -> list[list[str | None]]:
    """把 Python 侧交易映射为 oracle 的 transactions 列序。"""
    return [
        [
            transaction.transaction_hash,
            transaction.account,
            transaction.record_date,
            transaction.transaction_date,
            transaction.action,
            transaction.raw_symbol,
            transaction.description,
            transaction.asset_type,
            transaction.underlying,
            transaction.expiry,
            None if transaction.strike is None else str(transaction.strike),
            transaction.option_type,
            None if transaction.quantity is None else str(transaction.quantity),
            None if transaction.price is None else str(transaction.price),
            None if transaction.fees is None else str(transaction.fees),
            None if transaction.amount is None else str(transaction.amount),
            str(transaction.sequence),
        ]
        for transaction in transactions
    ]


def _run(case: dict[str, object]) -> list[list[str | None]]:
    """用 Python 领域层解析一条 csv 语料。"""
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
    return _lyra_transaction_rows(normalize_transaction_batch(batch).transactions)


def test_fixture_corpus_is_present_and_shared_with_typescript() -> None:
    """语料必须存在且含 csv 用例；两侧共用同一份录制结果。"""
    cases = load_cases()
    assert len(cases) >= 90, f"语料过少：{len(cases)}"
    assert len(cases_of_kind("csv")) > 0


@pytest.mark.parametrize("case", cases_of_kind("csv"), ids=str)
def test_csv_parsing_matches_oracle(case: dict[str, object]) -> None:
    """每条 csv 语料的解析结果必须与 oracle 逐字段一致。"""
    expected_error = case["expectedError"]

    if expected_error is not None:
        expected = expected_error_text(case)
        with pytest.raises(IngestError) as raised:
            _run(case)
        assert normalize_message(str(raised.value)) == normalize_message(expected), (
            f"失败原因不同\n  oracle: {expected}\n  python: {raised.value}"
        )
        return

    expected_rows = [list(row) for row in case["expected"]["transactions"]]  # type: ignore[index]
    actual = canonical(_run(case))
    wanted = canonical(expected_rows)

    assert actual == wanted, (
        f"transactions 不一致：实际 {len(actual)} 行，期望 {len(wanted)} 行\n"
        f"  仅 Python 有: {[row for row in actual if row not in wanted][:2]}\n"
        f"  仅 oracle 有: {[row for row in wanted if row not in actual][:2]}"
    )


def test_business_hash_matches_oracle_across_whole_corpus() -> None:
    """跨全部 csv 语料统计哈希一致性，避免个别用例意外通过掩盖系统性问题。"""
    compared = 0
    for case in cases_of_kind("csv"):
        if case["expectedError"] is not None:
            continue
        expected_hashes = {row[0] for row in case["expected"]["transactions"]}  # type: ignore[index]
        actual_hashes = {row[0] for row in _run(case)}
        assert actual_hashes == expected_hashes, f"哈希集合不一致: {case['caseId']}"
        compared += 1
    assert compared >= 20, f"参与哈希比对的用例过少：{compared}"
