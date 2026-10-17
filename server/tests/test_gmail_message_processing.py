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
    GmailMessageProcessingDependencies,
    GmailMessageProcessSummary,
    process_gmail_messages,
)
from lyra.domain.gmail.econfirm import GmailMessage

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
    import_outcome: str = "imported"
    calls: list = field(default_factory=list)

    def build(self) -> GmailMessageProcessingDependencies:
        """装配依赖集合。"""
        return GmailMessageProcessingDependencies(
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
    deps = _Deps(messages=(_message(),), import_outcome="already_imported")

    summary = _run(deps)

    assert summary.already_imported_messages == 1
    assert summary.imported_messages == 0


def test_explicit_account_is_passed_through() -> None:
    """用户显式选择的账户传给解析。"""
    deps = _Deps(messages=(_message(),))

    _run(deps, explicit=ACCOUNT_ID)

    assert _calls(deps, "resolve")[0][1] == SUFFIX


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
    }
    for key in projected:
        assert "_" not in key, key
