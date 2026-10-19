"""处理循环测试。

**四种结局各自计数，互不混淆。** 把 conflict 与 rejected 合并会让用户无从下手：
前者要点一下选账户，后者要看邮件内容，是两个完全不同的动作。

**单封邮件失败不终止整次同步**：一封格式异常的邮件不该让同一批里的其它邮件都不被
导入——那会让用户反复重试且每次都停在同一封上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.gmail_message_processing import (
    GmailMessageImportOutcome,
    GmailMessageProcessingDependencies,
    GmailMessageProcessSummary,
    process_gmail_messages,
)
from lyra.domain.gmail.econfirm import GmailMessage
from lyra.domain.ledger.errors import IngestError

SUFFIX = "276"
ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BODY = "Account ending in 276\n\nSymbol:\n\nINTC\n\nSecurity Description:\n\nINTEL CORP\n\nAction:\n\nPurchase\n\nType:\n\nMargin\n\nTrade Date:\n\n1/5/2026\n\nTotal Amount\n\n10\n\n40.5\n\n405\n\n$405\n"


def _message(message_id: str = "m1", body: str = BODY) -> GmailMessage:
    """构造邮件。"""
    return GmailMessage(
        message_id=message_id, sender="Schwab <donotreply@mail.schwab.com>",
        subject="Schwab eConfirms Account ending in 276", body=body,
        email_timestamp=None,
    )


@dataclass
class _Deps:
    """外部动作桩。"""

    messages: tuple = ()
    mappings: tuple = ()
    accounts: tuple = field(default_factory=lambda: (
        SimpleNamespace(id=ACCOUNT_ID, legacy_account_key="Individual_XXX276",
                        masked_suffix="276", status="active"),
    ))
    resolve_error: Exception | None = None
    import_outcome: GmailMessageImportOutcome = GmailMessageImportOutcome("imported", 1, 0)
    parse_trades_action: object | None = None
    calls: list = field(default_factory=list)

    def build(self) -> GmailMessageProcessingDependencies:
        """装配依赖集合。"""
        values = dict(
            parse_records=lambda record: self.messages,
            load_accounts=lambda: self.accounts,
            load_mappings=lambda: self.mappings,
            resolve_mapping=self._resolve,
            create_mapping=lambda plan: self.calls.append(("create_mapping", plan)),
            persist_unsuccessful=lambda message, code: self.calls.append(
                ("persist_unsuccessful", message.message_id, code)
            ),
            import_message=self._import,
        )
        if self.parse_trades_action is not None:
            values["parse_trades"] = self.parse_trades_action
        return GmailMessageProcessingDependencies(**values)

    def _resolve(self, **kwargs):
        self.calls.append(("resolve", kwargs.get("suffix")))
        if self.resolve_error is not None:
            raise self.resolve_error
        return SimpleNamespace(
            account_id=ACCOUNT_ID, legacy_account_key="Individual_XXX276",
            matched_by="explicit_selection", mapping_to_create=None,
        )

    def _import(self, message, trades, mapping):
        self.calls.append(("import", message.message_id, len(trades)))
        return self.import_outcome


def _run(deps: _Deps, *, explicit: str | None = None):
    """跑一次处理。"""
    return process_gmail_messages(
        ({"id": "record-1"},), dependencies=deps.build(),
        explicit_account_id=explicit,
    )


def _calls(deps: _Deps, name: str) -> list:
    """取出某类调用。"""
    return [call for call in deps.calls if call[0] == name]


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_imports_a_valid_message() -> None:
    """合法邮件被导入并计数。"""
    deps = _Deps(messages=(_message(),))

    summary = _run(deps)

    assert summary.imported_messages == 1
    assert summary.to_dict()["importedMessages"] == 1
    assert len(_calls(deps, "import")) == 1


def test_already_imported_is_counted_separately() -> None:
    """已导入与本次导入分开计数。"""
    deps = _Deps(
        messages=(_message(),),
        import_outcome=GmailMessageImportOutcome("already_imported", 0, 1),
    )

    summary = _run(deps)

    assert summary.already_imported_messages == 1
    assert summary.imported_messages == 0
    assert summary.inserted_transactions == 0
    assert summary.existing_transactions == 1


def test_csv_overlap_counts_existing_rows_in_a_newly_seen_email() -> None:
    """首次处理的邮件也可能全部对应 CSV 已有交易，不能把处理封数显示为新增笔数。"""
    deps = _Deps(
        messages=(_message(),),
        import_outcome=GmailMessageImportOutcome("imported", 0, 1),
    )
    summary = _run(deps)
    assert summary.imported_messages == 1
    assert summary.inserted_transactions == 0
    assert summary.existing_transactions == 1


@pytest.mark.parametrize("inserted,existing", [(-1, 0), (0, -1), (True, 0), (1, False)])
def test_invalid_transaction_counts_are_rejected(inserted: int, existing: int) -> None:
    """不允许负数或布尔值伪装成成功的金融写入计数。"""
    with pytest.raises(ValueError):
        GmailMessageImportOutcome("imported", inserted, existing)


def test_archived_message_cannot_claim_new_transactions() -> None:
    """消息已归档的结果必须明确没有新增账本行。"""
    with pytest.raises(ValueError):
        GmailMessageImportOutcome("already_imported", 1, 0)


def test_explicit_account_is_passed_through() -> None:
    """用户显式选择的账户传给解析。"""
    deps = _Deps(messages=(_message(),))

    _run(deps, explicit=ACCOUNT_ID)

    assert _calls(deps, "resolve")[0][1] == SUFFIX


def test_injected_parser_receives_message_and_explicit_account() -> None:
    """可注入解析动作获得本次显式账户，且默认调用仍保持可选。"""
    received: list[tuple[str, str | None]] = []
    deps = _Deps(messages=(_message(),))

    def parse_trades(message, explicit_account_id):
        received.append((message.message_id, explicit_account_id))
        return (SimpleNamespace(trade_date="2026-01-05"),)

    deps.parse_trades_action = parse_trades

    _run(deps, explicit=ACCOUNT_ID)

    assert received == [("m1", ACCOUNT_ID)]


def test_mapping_is_created_when_resolution_asks_for_it() -> None:
    """解析要求建映射时执行建立。"""
    deps = _Deps(messages=(_message(),))

    def resolve_with_creation(**kwargs):
        deps.calls.append(("resolve", kwargs.get("suffix")))
        return SimpleNamespace(
            account_id=ACCOUNT_ID, legacy_account_key="Individual_XXX276",
            matched_by="explicit_selection",
            mapping_to_create=SimpleNamespace(account_id=ACCOUNT_ID),
        )

    deps._resolve = resolve_with_creation  # type: ignore[method-assign]
    _run(deps)

    assert len(_calls(deps, "create_mapping")) == 1


# --------------------------------------------------------------------------
# 拒绝与冲突
# --------------------------------------------------------------------------


def test_unparseable_message_is_rejected_and_recorded() -> None:
    """**内容解析失败记 rejected 并归档**，不抛错。"""
    deps = _Deps(messages=(_message(body="no symbol blocks here"),))

    summary = _run(deps)

    assert summary.rejected_messages == 1
    assert _calls(deps, "persist_unsuccessful")[0][2] == "GMAIL_MESSAGE_REJECTED"
    assert _calls(deps, "import") == []


def test_mapping_conflict_is_counted_separately_from_rejection() -> None:
    """**账户映射冲突与解析拒绝分开计数。**

    两者对应完全不同的用户动作：前者要点一下选账户，后者要看邮件内容。
    合并会让用户无从下手。
    """
    deps = _Deps(
        messages=(_message(),),
        resolve_error=ApplicationError(
            code="ACCOUNT_MAPPING_REQUIRED", message="需要确认映射",
            status_code=409, retryable=False,
        ),
    )

    summary = _run(deps)

    assert summary.conflict_messages == 1
    assert summary.rejected_messages == 0
    assert _calls(deps, "persist_unsuccessful")[0][2] == "ACCOUNT_MAPPING_REQUIRED"


def test_other_application_errors_propagate() -> None:
    """**非"需要映射"的应用错误必须抛出**，不能被吞成一次冲突。"""
    deps = _Deps(
        messages=(_message(),),
        resolve_error=ApplicationError(
            code="SOMETHING_BROKEN", message="故障", status_code=500, retryable=False
        ),
    )

    with pytest.raises(ApplicationError, match="故障"):
        _run(deps)


def test_parse_stage_mapping_conflict_is_archived_as_conflict() -> None:
    """解析回退所需映射未确认时只归档为账户冲突。"""
    deps = _Deps(messages=(_message(),))

    def parse_trades(_message, _explicit_account_id):
        raise ApplicationError(
            code="ACCOUNT_MAPPING_REQUIRED", message="需要确认映射",
            status_code=409, retryable=False,
        )

    deps.parse_trades_action = parse_trades

    summary = _run(deps)

    assert summary.conflict_messages == 1
    assert summary.rejected_messages == 0
    assert _calls(deps, "persist_unsuccessful")[0][2] == "ACCOUNT_MAPPING_REQUIRED"
    assert _calls(deps, "import") == []


def test_parse_stage_unrelated_application_error_is_not_swallowed() -> None:
    """解析回退中的其它业务故障不得伪装为冲突或内容拒绝。"""
    deps = _Deps(messages=(_message(),))

    def parse_trades(_message, _explicit_account_id):
        raise ApplicationError(
            code="ACCOUNT_LOOKUP_FAILED", message="账户读取失败",
            status_code=500, retryable=False,
        )

    deps.parse_trades_action = parse_trades

    with pytest.raises(ApplicationError, match="账户读取失败"):
        _run(deps)

    assert _calls(deps, "persist_unsuccessful") == []


def test_unresolved_cash_option_sale_from_import_is_rejected_specifically() -> None:
    """计划确认无法关联已有期权事实时拒绝邮件，但不掩盖其它导入错误。"""
    from lyra.domain.gmail.econfirm import UnresolvedCashOptionSaleError

    deps = _Deps(messages=(_message(),))
    deps.parse_trades_action = lambda _message, _account_id: (
        SimpleNamespace(trade_date="2026-01-05"),
    )

    def unresolved_import(_message, _trades, _mapping):
        raise UnresolvedCashOptionSaleError("需要已确认的期权卖出事实")

    deps._import = unresolved_import  # type: ignore[method-assign]
    summary = _run(deps)

    assert summary.rejected_messages == 1
    assert summary.imported_messages == 0
    assert _calls(deps, "persist_unsuccessful")[0][2] == "GMAIL_MESSAGE_REJECTED"

    def unrelated_import(_message, _trades, _mapping):
        raise IngestError("其它导入故障")

    deps._import = unrelated_import  # type: ignore[method-assign]
    with pytest.raises(IngestError, match="其它导入故障"):
        _run(deps)


def test_missing_suffix_is_rejected() -> None:
    """缺账户尾号算 rejected。"""
    deps = _Deps(messages=(_message(body="Symbol:\n\nINTC\n"),))

    summary = _run(deps)

    assert summary.rejected_messages == 1


# --------------------------------------------------------------------------
# 失败不终止
# --------------------------------------------------------------------------


def test_one_bad_message_does_not_stop_the_rest() -> None:
    """**一封格式异常的邮件不终止整次同步。**

    否则用户会反复重试且每次都停在同一封上。
    """
    deps = _Deps(messages=(
        _message("bad", "no blocks"),
        _message("good"),
        _message("bad2", "also no blocks"),
        _message("good2"),
    ))

    summary = _run(deps)

    assert summary.rejected_messages == 2
    assert summary.imported_messages == 2
    assert [call[1] for call in _calls(deps, "import")] == ["good", "good2"]


def test_a_conflict_does_not_stop_the_rest() -> None:
    """一次映射冲突也不终止后续邮件。"""
    calls: list = []

    def resolve(**kwargs):
        calls.append(kwargs["suffix"])
        raise ApplicationError(
            code="ACCOUNT_MAPPING_REQUIRED", message="需要确认映射",
            status_code=409, retryable=False,
        )

    deps = _Deps(messages=(_message("m1"), _message("m2")))
    deps._resolve = resolve  # type: ignore[method-assign]

    summary = _run(deps)

    assert summary.conflict_messages == 2
    assert calls == [SUFFIX, SUFFIX]


def test_multiple_records_are_all_processed() -> None:
    """多条记录全部处理。"""
    deps = _Deps(messages=(_message(),))
    process_gmail_messages(
        ({"id": "r1"}, {"id": "r2"}, {"id": "r3"}), dependencies=deps.build()
    )

    assert len(_calls(deps, "import")) == 3


# --------------------------------------------------------------------------
# 映射查找
# --------------------------------------------------------------------------


def test_existing_mapping_is_found_case_insensitively() -> None:
    """已有映射按大写尾号查找。"""
    found: list = []

    def resolve(**kwargs):
        found.append(kwargs["existing_mapping"])
        return SimpleNamespace(
            account_id=ACCOUNT_ID, legacy_account_key="K",
            matched_by="existing_mapping", mapping_to_create=None,
        )

    deps = _Deps(
        messages=(_message(),),
        mappings=(SimpleNamespace(account_suffix="276"),),
    )
    deps._resolve = resolve  # type: ignore[method-assign]
    _run(deps)

    assert found == [SimpleNamespace(account_suffix="276")]


def test_no_matching_mapping_passes_none() -> None:
    """尾号不匹配时传 ``None``——**不猜**。"""
    found: list = []

    def resolve(**kwargs):
        found.append(kwargs["existing_mapping"])
        return SimpleNamespace(
            account_id=ACCOUNT_ID, legacy_account_key="K",
            matched_by="existing_mapping", mapping_to_create=None,
        )

    deps = _Deps(
        messages=(_message(),),
        mappings=(SimpleNamespace(account_suffix="999"),),
    )
    deps._resolve = resolve  # type: ignore[method-assign]
    _run(deps)

    assert found == [None]


# --------------------------------------------------------------------------
# 摘要投影
# --------------------------------------------------------------------------


def test_summary_projects_to_camel_case() -> None:
    """摘要投影为 camelCase，无 snake_case 键。"""
    summary = GmailMessageProcessSummary(
        imported_messages=1, already_imported_messages=2,
        conflict_messages=3, rejected_messages=4,
    )

    projected = summary.to_dict()

    assert projected == {
        "importedMessages": 1, "alreadyImportedMessages": 2,
        "conflictMessages": 3, "rejectedMessages": 4,
        "insertedTransactions": 0, "existingTransactions": 0,
        "replayOrderConflictMessages": 0,
    }
    for key in projected:
        assert "_" not in key, key


def test_newest_first_mail_is_processed_by_actual_trade_date() -> None:
    """日期排序不依赖收信时间或 CSV 范围；同日顺序证明留给金融内核。"""
    deps = _Deps(messages=(
        _message("later"),
        _message("earlier", BODY.replace("1/5/2026", "1/4/2026")),
    ))
    _run(deps)
    assert [call[1] for call in _calls(deps, "import")] == ["earlier", "later"]


def test_ambiguous_replay_is_visible_and_does_not_stop_other_messages() -> None:
    """顺序不明的邮件没有金融写入，单独提示完整 CSV，其他邮件继续处理。"""
    deps = _Deps(messages=(_message("ambiguous"), _message("good")))
    original_import = deps._import

    def import_message(message, trades, mapping):
        if message.message_id == "ambiguous":
            raise ApplicationError(
                code="GMAIL_REPLAY_ORDER_AMBIGUOUS", message="需完整 CSV",
                status_code=409, retryable=False,
            )
        return original_import(message, trades, mapping)

    deps._import = import_message
    summary = _run(deps)
    assert summary.imported_messages == 1
    assert summary.inserted_transactions == 1
    assert summary.conflict_messages == summary.replay_order_conflict_messages == 1
    assert _calls(deps, "persist_unsuccessful") == [
        ("persist_unsuccessful", "ambiguous", "GMAIL_REPLAY_ORDER_AMBIGUOUS")
    ]
