"""Gmail 公开视图测试。

公开视图**刻意不暴露**这几个字段：``encrypted_refresh_token`` / ``google_subject`` /
``granted_scopes`` / ``history_id``。

连接视图还会**校验 ``last_error_code`` 是已知错误码**——一行含未知错误码说明它被别的
写入路径污染了，照原样投影会把任意文本送到客户端。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lyra.application.gmail_views import (
    create_public_gmail_account_mapping_list,
    create_public_gmail_connection,
)

CONNECTION_ID = "11111111-1111-1111-1111-111111111111"
OTHER_CONNECTION = "22222222-2222-2222-2222-222222222222"


def _connection(**overrides):
    """构造连接行。"""
    base = {
        "id": CONNECTION_ID, "google_email": "owner@example.com", "status": "active",
        "last_error_code": None, "last_sync_at": None,
        "created_at": "2026-10-12T09:00:00.000Z",
        "updated_at": "2026-10-12T09:00:00.000Z",
        # 以下字段都**不应出现在公开视图里**。
        "google_subject": "112233", "encrypted_refresh_token": "v1.iv.ct.tag",
        "granted_scopes": "https://www.googleapis.com/auth/gmail.readonly",
        "history_id": "987654",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _mapping(mapping_id="m1", *, connection_id=CONNECTION_ID, suffix="276", **overrides):
    """构造映射行。"""
    base = {
        "id": mapping_id, "connection_id": connection_id, "account_suffix": suffix,
        "account_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "created_at": "2026-10-12T09:00:00.000Z",
        "updated_at": "2026-10-12T09:00:00.000Z",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# 连接视图
# --------------------------------------------------------------------------


def test_projects_the_public_fields() -> None:
    """公开字段齐全且为 camelCase。"""
    view = create_public_gmail_connection(_connection()).to_json()

    assert view == {
        "id": CONNECTION_ID, "googleEmail": "owner@example.com", "status": "active",
        "lastErrorCode": None, "lastSyncAt": None,
        "createdAt": "2026-10-12T09:00:00.000Z",
        "updatedAt": "2026-10-12T09:00:00.000Z",
    }


@pytest.mark.parametrize(
    "secret", ["encrypted_refresh_token", "google_subject", "granted_scopes", "history_id"]
)
def test_internal_fields_are_withheld(secret: str) -> None:
    """**内部字段不出现在视图里。**

    ``encrypted_refresh_token`` 虽不可直接使用，但泄露它等于给出离线解密的靶子。
    """
    view = create_public_gmail_connection(_connection()).to_json()

    assert secret not in str(view)
    assert "v1.iv.ct.tag" not in str(view)
    assert "987654" not in str(view)
    assert "112233" not in str(view)


def test_no_snake_case_keys_escape() -> None:
    """公开响应不得出现 snake_case 键。"""
    view = create_public_gmail_connection(_connection()).to_json()

    for key in view:
        assert "_" not in key, key


def test_known_error_codes_are_projected() -> None:
    """已知错误码正常投影。"""
    for code in (
        "GMAIL_HISTORY_EXPIRED", "GMAIL_INCREMENTAL_LIMIT_EXCEEDED",
        "GMAIL_RESYNC_CONFIRMATION_REQUIRED", "GMAIL_AUTH_REVOKED",
        "GMAIL_CONNECTION_ERROR",
    ):
        view = create_public_gmail_connection(_connection(last_error_code=code))
        assert view.last_error_code == code


def test_unknown_error_codes_are_rejected() -> None:
    """**未知错误码必须拒绝投影。**

    那说明该行被别的写入路径污染了；照原样投影会把任意文本送到客户端。
    """
    with pytest.raises(ValueError, match="非稳定错误码"):
        create_public_gmail_connection(
            _connection(last_error_code="postgres://user:pw@host")
        )


# --------------------------------------------------------------------------
# 映射列表
# --------------------------------------------------------------------------


def test_projects_and_sorts_mappings() -> None:
    """按尾号再按 ID 排序，并投影公开字段。"""
    views = create_public_gmail_account_mapping_list(CONNECTION_ID, (
        _mapping("m2", suffix="999"), _mapping("m1", suffix="276"),
        _mapping("m0", suffix="276"),
    ))

    assert [view.account_suffix for view in views] == ["276", "276", "999"]
    assert [view.id for view in views] == ["m0", "m1", "m2"]
    assert views[0].to_json() == {
        "id": "m0", "accountSuffix": "276",
        "accountId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "createdAt": "2026-10-12T09:00:00.000Z",
        "updatedAt": "2026-10-12T09:00:00.000Z",
    }


def test_mappings_from_another_connection_are_rejected() -> None:
    """**仓储若返回别的连接的行，直接报错。**

    把它们展示给用户会泄露另一个连接的账户归属。
    """
    with pytest.raises(ValueError, match="不属于当前连接"):
        create_public_gmail_account_mapping_list(CONNECTION_ID, (
            _mapping("m1"), _mapping("m2", connection_id=OTHER_CONNECTION),
        ))


def test_empty_mapping_list_is_valid() -> None:
    """空列表合法。"""
    assert create_public_gmail_account_mapping_list(CONNECTION_ID, ()) == ()


def test_mapping_view_has_no_snake_case_keys() -> None:
    """映射视图同样是 camelCase。"""
    views = create_public_gmail_account_mapping_list(CONNECTION_ID, (_mapping(),))

    for key in views[0].to_json():
        assert "_" not in key, key
