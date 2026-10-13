"""Gmail 账户尾号解析测试。

**核心安全性质：系统从不猜测尾号属于哪个账户。**

即使只有一个活跃账户的尾号匹配，也必须由用户显式确认。自动匹配看起来省事，但它把
"某封邮件属于哪个账本"这个决定交给了字符串比较——**猜错会让交易进错账本**，
而更正它意味着删除已入账的交易并重算 FIFO。
"""

from __future__ import annotations

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.gmail_account_mapping import (
    AccountRecord,
    ExistingMapping,
    plan_gmail_account_mapping_update,
    resolve_gmail_account_mapping,
    suffix_matches,
)

CONNECTION = "11111111-1111-1111-1111-111111111111"
OTHER_CONNECTION = "22222222-2222-2222-2222-222222222222"
ACCOUNT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ACCOUNT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _account(
    account_id: str, *, suffix: str = "276", legacy: str = "Individual_XXX276",
    status: str = "active",
) -> AccountRecord:
    """构造账户。"""
    return AccountRecord(
        id=account_id, legacy_account_key=legacy, masked_suffix=suffix, status=status
    )


def _mapping(
    *, connection_id: str = CONNECTION, suffix: str = "276", account_id: str = ACCOUNT_A
) -> ExistingMapping:
    """构造映射。"""
    return ExistingMapping(
        id="m1", connection_id=connection_id, account_suffix=suffix,
        account_id=account_id,
    )


def _resolve(accounts, *, existing=None, explicit=None, suffix="276"):
    """跑一次解析。"""
    return resolve_gmail_account_mapping(
        connection_id=CONNECTION, account_suffix=suffix, accounts=tuple(accounts),
        existing_mapping=existing, explicit_account_id=explicit,
    )


# --------------------------------------------------------------------------
# 尾号匹配
# --------------------------------------------------------------------------


def test_matches_the_masked_suffix() -> None:
    """掩码尾号相同即匹配。"""
    assert suffix_matches(_account(ACCOUNT_A, suffix="276"), "276") is True


def test_matches_a_legacy_key_ending() -> None:
    """**完整账户键以尾号结尾也匹配。**"""
    assert suffix_matches(
        _account(ACCOUNT_A, suffix="000", legacy="Individual_XXX276"), "276"
    ) is True


def test_suffix_matching_is_case_insensitive() -> None:
    """匹配不区分大小写。

    **尾号参数由调用方预先大写化**（与参考实现一致），函数只大写化账户一侧。
    """
    assert suffix_matches(
        _account(ACCOUNT_A, suffix="ABC", legacy="Individual_XXXABC"), "ABC"
    ) is True
    assert suffix_matches(
        _account(ACCOUNT_A, suffix="abc", legacy="Individual_XXXabc"), "ABC"
    ) is True


def test_non_matching_suffix() -> None:
    """不匹配的尾号。"""
    assert suffix_matches(_account(ACCOUNT_A, suffix="276"), "999") is False


# --------------------------------------------------------------------------
# 已有映射
# --------------------------------------------------------------------------


def test_existing_mapping_is_used() -> None:
    """已有映射直接使用。"""
    result = _resolve([_account(ACCOUNT_A)], existing=_mapping())

    assert result.account_id == ACCOUNT_A
    assert result.matched_by == "existing_mapping"
    assert result.mapping_to_create is None
    assert result.legacy_account_key == "Individual_XXX276"


def test_existing_mapping_matches_suffix_case_insensitively() -> None:
    """已有映射的尾号比较不区分大小写。"""
    result = _resolve(
        [_account(ACCOUNT_A)], existing=_mapping(suffix="abc"), suffix="ABC"
    )
    assert result.account_id == ACCOUNT_A


def test_mapping_from_another_connection_is_rejected() -> None:
    """**不属于本连接的映射不能用。**"""
    with pytest.raises(ApplicationError) as raised:
        _resolve([_account(ACCOUNT_A)], existing=_mapping(connection_id=OTHER_CONNECTION))

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"


def test_mapping_for_a_different_suffix_is_rejected() -> None:
    """尾号不符的映射不能用。"""
    with pytest.raises(ApplicationError) as raised:
        _resolve([_account(ACCOUNT_A)], existing=_mapping(suffix="999"))

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"


def test_mapping_to_an_inactive_account_is_rejected() -> None:
    """**映射指向已停用账户时必须重新确认。**

    继续用它会让新交易进入一个已经不再使用的账本。
    """
    accounts = [_account(ACCOUNT_A, status="archived")]

    with pytest.raises(ApplicationError) as raised:
        _resolve(accounts, existing=_mapping())

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"


def test_explicit_selection_that_contradicts_the_mapping_is_rejected() -> None:
    """**用户显式选了别的账户时，已有映射不能压过他的选择。**

    直接沿用映射会让"用户点了改绑"这件事静默失效。
    """
    accounts = [_account(ACCOUNT_A), _account(ACCOUNT_B, legacy="Individual_XXX276")]

    with pytest.raises(ApplicationError) as raised:
        _resolve(accounts, existing=_mapping(account_id=ACCOUNT_A), explicit=ACCOUNT_B)

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"


# --------------------------------------------------------------------------
# 显式选择
# --------------------------------------------------------------------------


def test_explicit_selection_creates_a_mapping() -> None:
    """显式选择会建立映射，之后无需再次确认。"""
    result = _resolve([_account(ACCOUNT_A)], explicit=ACCOUNT_A)

    assert result.matched_by == "explicit_selection"
    assert result.mapping_to_create is not None
    assert result.mapping_to_create.account_id == ACCOUNT_A
    assert result.mapping_to_create.account_suffix == "276"


def test_explicit_selection_of_an_inactive_account_is_rejected() -> None:
    """显式选择已停用账户同样拒绝。"""
    with pytest.raises(ApplicationError):
        _resolve([_account(ACCOUNT_A, status="archived")], explicit=ACCOUNT_A)


def test_documenting_that_legacy_key_ending_also_matches() -> None:
    """**记录一处容易踩的交互**：即使掩码尾号不同，只要完整账户键以该尾号结尾就匹配。

    我在写测试时把 `masked_suffix="999"` 配上默认的 `Individual_XXX276`，
    结果按规则**确实匹配 276**——实现是对的，是我的测试数据自相矛盾。
    """
    inconsistent = _account(ACCOUNT_A, suffix="999", legacy="Individual_XXX276")

    assert suffix_matches(inconsistent, "276") is True
    assert suffix_matches(inconsistent, "999") is True


def test_explicit_selection_with_a_mismatched_suffix_is_rejected() -> None:
    """**显式选择的账户尾号必须匹配。**

    否则用户的一次误点就能把 276 的交易写进 999 的账本。
    """
    # 掩码尾号与完整账户键都指向 999，才构成真正的"尾号不符"。
    account = _account(ACCOUNT_A, suffix="999", legacy="Individual_XXX999")

    with pytest.raises(ApplicationError):
        _resolve([account], explicit=ACCOUNT_A, suffix="276")


# --------------------------------------------------------------------------
# 从不猜测
# --------------------------------------------------------------------------


def test_no_mapping_and_no_selection_requires_confirmation() -> None:
    """**即使只有一个账户匹配也不自动选。**

    自动匹配把"某封邮件属于哪个账本"交给了字符串比较——猜错会让交易进错账本，
    而更正它意味着删除已入账的交易并重算 FIFO。
    """
    with pytest.raises(ApplicationError) as raised:
        _resolve([_account(ACCOUNT_A)])

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"
    assert raised.value.status_code == 409
    assert raised.value.retryable is False


def test_candidates_are_returned_as_sorted_uuids() -> None:
    """**候选只给 UUID，不给完整券商账户键**——后者含完整账号。"""
    accounts = [
        _account(ACCOUNT_B, legacy="Individual_XXX276"),
        _account(ACCOUNT_A, legacy="Individual_YYY276"),
    ]

    with pytest.raises(ApplicationError) as raised:
        _resolve(accounts)

    assert raised.value.details is not None
    assert raised.value.details["candidate_account_ids"] == sorted([ACCOUNT_A, ACCOUNT_B])
    assert "Individual" not in str(raised.value.details)


def test_candidates_only_include_active_accounts() -> None:
    """候选只含活跃账户。"""
    accounts = [
        _account(ACCOUNT_A), _account(ACCOUNT_B, status="archived"),
    ]

    with pytest.raises(ApplicationError) as raised:
        _resolve(accounts)

    assert raised.value.details is not None
    assert raised.value.details["candidate_account_ids"] == [ACCOUNT_A]


def test_no_candidates_yields_an_empty_list() -> None:
    """没有匹配账户时候选为空，而不是报出无关账户。"""
    account = _account(ACCOUNT_A, suffix="999", legacy="Individual_XXX999")

    with pytest.raises(ApplicationError) as raised:
        _resolve([account], suffix="276")

    assert raised.value.details is not None
    assert raised.value.details["candidate_account_ids"] == []


@pytest.mark.parametrize("suffix", ["", "   "])
def test_blank_suffix_is_rejected(suffix: str) -> None:
    """空尾号是调用方错误。"""
    with pytest.raises(ValueError, match="尾号不能为空"):
        _resolve([_account(ACCOUNT_A)], suffix=suffix)


# --------------------------------------------------------------------------
# 改绑计划
# --------------------------------------------------------------------------


def test_rebinding_plans_a_change() -> None:
    """改绑到另一个合法账户。"""
    plan = plan_gmail_account_mapping_update(
        connection_id=CONNECTION, mapping=_mapping(),
        accounts=(_account(ACCOUNT_A), _account(ACCOUNT_B, legacy="Individual_XXX276")),
        target_account_id=ACCOUNT_B,
    )

    assert plan.changed is True
    assert plan.expected_account_id == ACCOUNT_A
    assert plan.next_mapping.account_id == ACCOUNT_B


def test_rebinding_to_the_same_account_is_a_no_op() -> None:
    """改绑到同一个账户不算变更。"""
    plan = plan_gmail_account_mapping_update(
        connection_id=CONNECTION, mapping=_mapping(),
        accounts=(_account(ACCOUNT_A),), target_account_id=ACCOUNT_A,
    )

    assert plan.changed is False
    assert plan.next_mapping == _mapping()


def test_rebinding_a_mapping_from_another_connection_is_rejected() -> None:
    """**映射不属于当前连接时拒绝（400）。**

    否则一次调用就能改到别的连接上。
    """
    with pytest.raises(ApplicationError) as raised:
        plan_gmail_account_mapping_update(
            connection_id=OTHER_CONNECTION, mapping=_mapping(),
            accounts=(_account(ACCOUNT_A),), target_account_id=ACCOUNT_A,
        )

    assert raised.value.status_code == 400
    assert raised.value.code == "INVALID_ARGUMENT"


def test_rebinding_to_a_suffix_mismatched_account_is_rejected() -> None:
    """目标账户尾号必须匹配。"""
    account = _account(ACCOUNT_A, suffix="999", legacy="Individual_XXX999")

    with pytest.raises(ApplicationError) as raised:
        plan_gmail_account_mapping_update(
            connection_id=CONNECTION, mapping=_mapping(),
            accounts=(account,), target_account_id=ACCOUNT_A,
        )

    assert raised.value.code == "ACCOUNT_MAPPING_REQUIRED"
