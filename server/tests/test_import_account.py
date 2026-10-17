"""导入账户绑定测试。

绑定是导入的**授权闸门**：它决定一份文件能写进哪个账户。因此重点是拒绝路径——
所有"不能唯一确定"的情况都必须停下来要求用户选择，而不是猜一个。

另有一条安全语义：绑定失败的错误里**只出现候选 UUID**，不得回显文件中的账户键
或完整券商标识。
"""

from __future__ import annotations

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_account import (
    ImportAccountRecord,
    resolve_import_account,
)

MAIN = ImportAccountRecord(
    id="11111111-1111-1111-1111-111111111111",
    legacy_account_key="TST001",
    masked_suffix="001",
    status="active",
)
SECOND = ImportAccountRecord(
    id="22222222-2222-2222-2222-222222222222",
    legacy_account_key="TST002",
    masked_suffix="002",
    status="active",
)
ARCHIVED = ImportAccountRecord(
    id="33333333-3333-3333-3333-333333333333",
    legacy_account_key="TST003",
    masked_suffix="003",
    status="archived",
)
ACCOUNTS = (MAIN, SECOND, ARCHIVED)


def _resolve(hint: str, explicit: str | None = None, accounts=ACCOUNTS):
    return resolve_import_account(hint, accounts, explicit)


# --------------------------------------------------------------------------
# 自动绑定
# --------------------------------------------------------------------------


def test_binds_by_exact_legacy_key() -> None:
    """唯一命中的完整旧键可以自动绑定。"""
    resolved = _resolve("TST001")

    assert resolved.account_id == MAIN.id
    assert resolved.legacy_account_key == "TST001"
    assert resolved.matched_by == "legacy_key"


def test_binding_is_case_and_whitespace_insensitive() -> None:
    """线索大小写与两端空白不影响匹配。"""
    for hint in ("tst001", "  TST001  ", "Tst001"):
        assert _resolve(hint).account_id == MAIN.id


@pytest.mark.parametrize("hint", ["", "   "])
def test_rejects_empty_hint(hint: str) -> None:
    """空线索是调用方 bug，抛 ValueError 而不是 409。"""
    with pytest.raises(ValueError, match="不能为空"):
        _resolve(hint)


def test_unknown_hint_requires_mapping() -> None:
    """线索对不上任何账户时必须要求显式选择。"""
    with pytest.raises(ApplicationError) as raised:
        _resolve("NOPE")
    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"
    assert raised.value.status_code == 409
    assert raised.value.details == {"candidate_account_ids": []}


def test_archived_account_is_not_auto_bound() -> None:
    """已归档账户不参与自动绑定。"""
    with pytest.raises(ApplicationError) as raised:
        _resolve("TST003")
    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"


# --------------------------------------------------------------------------
# 显式选择
# --------------------------------------------------------------------------


def test_explicit_selection_wins_when_hint_matches() -> None:
    """显式选择且线索相符时绑定到该账户。"""
    resolved = _resolve("TST002", explicit=SECOND.id)

    assert resolved.account_id == SECOND.id
    assert resolved.matched_by == "explicit_selection"


def test_explicit_selection_allows_masked_suffix_hint() -> None:
    """文件里只带脱敏后缀时，显式选择仍可绑定。"""
    resolved = _resolve("...001", explicit=MAIN.id)
    assert resolved.account_id == MAIN.id


def test_explicit_selection_must_match_the_hint() -> None:
    """**显式选择不能绕过线索校验**。

    否则用户可以把 A 账户的文件提交到 B 账户——这是把授权检查做成了摆设。
    """
    with pytest.raises(ApplicationError) as raised:
        _resolve("TST001", explicit=SECOND.id)

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"
    # 候选里只有与线索相符的账户。
    assert raised.value.details == {"candidate_account_ids": [MAIN.id]}


def test_explicit_selection_rejects_archived_account() -> None:
    """显式选择也不能指向已归档账户。"""
    with pytest.raises(ApplicationError):
        _resolve("TST003", explicit=ARCHIVED.id)


def test_explicit_selection_rejects_unknown_id() -> None:
    """不存在的账户 UUID 必须拒绝。"""
    with pytest.raises(ApplicationError) as raised:
        _resolve("TST001", explicit="99999999-9999-9999-9999-999999999999")
    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"


# --------------------------------------------------------------------------
# 歧义与信息暴露
# --------------------------------------------------------------------------


def test_ambiguous_suffix_requires_mapping() -> None:
    """后缀线索命中多个账户时不得自行挑一个。"""
    twin = ImportAccountRecord(
        id="44444444-4444-4444-4444-444444444444",
        legacy_account_key="OTH001",
        masked_suffix="001",
        status="active",
    )

    # 完整旧键仍能唯一确定，不受影响。
    assert _resolve("TST001", accounts=(*ACCOUNTS, twin)).account_id == MAIN.id

    # 只给后缀时两条都命中，必须要求选择。
    with pytest.raises(ApplicationError) as raised:
        _resolve("X001", explicit=None, accounts=(*ACCOUNTS, twin))
    assert set(raised.value.details["candidate_account_ids"]) == {MAIN.id, twin.id}


def test_error_never_leaks_account_keys_or_identifiers() -> None:
    """错误信息与 details 只含 UUID，不得回显账户键或券商标识。"""
    with pytest.raises(ApplicationError) as raised:
        _resolve("TST001", explicit=SECOND.id)

    error = raised.value
    serialized = f"{error.message} {error.details}"
    assert "TST001" not in serialized
    assert "TST002" not in serialized
    assert "001" not in str(error.details.get("candidate_account_ids"))
    # details 只允许这一项，键名也是内部命名。
    assert set(error.details) == {"candidate_account_ids"}


def test_candidates_are_sorted_and_deduplicated() -> None:
    """候选 UUID 排序稳定，便于客户端对照。"""
    twins = tuple(
        ImportAccountRecord(
            id=f"{index}{index}{index}{index}{index}{index}{index}{index}-1111-1111-1111-111111111111",
            legacy_account_key=f"ACC{index}",
            masked_suffix="001",
            status="active",
        )
        for index in (5, 3, 4)
    )

    with pytest.raises(ApplicationError) as raised:
        _resolve("X001", accounts=(*twins, MAIN))

    candidates = raised.value.details["candidate_account_ids"]
    assert candidates == sorted(candidates)
    assert len(candidates) == len(set(candidates))
