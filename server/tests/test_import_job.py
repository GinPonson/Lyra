"""导入作业状态机与公开视图测试。

两条最要紧的性质：

1. **终态不存在出边**——已提交/已失败/已取消/已拒绝的作业不能再变化。少了这条，
   一个迟到的重试就能把"已提交"改回"提交中"，进而重复写入账本。
2. **同状态请求返回原对象本身**——调用方靠对象同一性判断是否需要写库；返回副本
   会让每次幂等轮询都产生一次无收益写入。

另有一条安全语义：**非稳定错误码必须被拒绝**，不能当自由文本透传给 App。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lyra.application.app_error import ApplicationError
from lyra.application.import_job import (
    IMPORT_JOB_ERROR_CODES,
    IMPORT_JOB_TRANSITIONS,
    ImportJobState,
    PersistedImportJobRecord,
    create_public_import_job,
    is_import_job_error_code,
    transition_import_job,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)

TERMINAL = ("committed", "rejected", "failed", "cancelled")
NON_TERMINAL = ("uploaded", "parsing", "preview_ready", "committing")


def _state(status: str = "uploaded") -> ImportJobState:
    return ImportJobState(
        import_id="job-1", status=status, updated_at="2026-10-12T08:00:00.000Z"  # type: ignore[arg-type]
    )


def _job(**overrides) -> PersistedImportJobRecord:
    """构造完整作业记录。"""
    base = {
        "id": "job-1",
        "file_hash": "a" * 64,
        "original_file_name": "ledger.csv",
        "file_type": "csv",
        "file_size_bytes": 2048,
        "parser_version": "2",
        "status": "preview_ready",
        "target_account_id": "11111111-1111-1111-1111-111111111111",
        "preview_token_hash": "b" * 64,
        "preview_ledger_version": 3,
        "total_rows": 10,
        "inserted_rows": 8,
        "skipped_rows": 2,
        "error_code": None,
        "error_summary": None,
        "committed_ledger_version": None,
        "created_at": "2026-10-12T08:00:00.000Z",
        "updated_at": "2026-10-12T08:30:00.000Z",
    }
    base.update(overrides)
    return PersistedImportJobRecord(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        ("uploaded", "parsing"),
        ("uploaded", "cancelled"),
        ("parsing", "preview_ready"),
        ("parsing", "rejected"),
        ("parsing", "failed"),
        ("preview_ready", "committing"),
        ("preview_ready", "cancelled"),
        ("committing", "committed"),
        ("committing", "failed"),
    ],
)
def test_legal_transitions_are_applied(current: str, next_status: str) -> None:
    """转换图里的每条边都必须可用，并刷新更新时间。"""
    result = transition_import_job(_state(current), next_status, NOW)  # type: ignore[arg-type]

    assert result.status == next_status
    assert result.updated_at == "2026-10-12T09:00:00.000Z"
    assert result.import_id == "job-1"


@pytest.mark.parametrize("status", TERMINAL)
@pytest.mark.parametrize("target", NON_TERMINAL + TERMINAL)
def test_terminal_states_have_no_outgoing_edges(status: str, target: str) -> None:
    """**终态不存在出边。**

    少了这条，一个迟到的重试就能把「已提交」改回「提交中」，进而重复写入账本。
    """
    if status == target:
        pytest.skip("同状态属于幂等重放，另有用例覆盖")

    with pytest.raises(ApplicationError) as raised:
        transition_import_job(_state(status), target, NOW)  # type: ignore[arg-type]

    assert raised.value.code == "IMPORT_STATE_CONFLICT"
    assert raised.value.status_code == 409
    assert raised.value.details == {
        "current_status": status, "requested_status": target
    }


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("uploaded", "preview_ready"),   # 跳过解析
        ("uploaded", "committing"),      # 跳过预览
        ("parsing", "committing"),       # 跳过预览就绪
        ("preview_ready", "committed"),  # 跳过提交中
        ("committing", "cancelled"),     # 提交中不可取消
        ("parsing", "cancelled"),        # 解析中不可取消
    ],
)
def test_skipping_steps_is_rejected(current: str, target: str) -> None:
    """「先预览再提交」的顺序不能被绕过。"""
    with pytest.raises(ApplicationError) as raised:
        transition_import_job(_state(current), target, NOW)  # type: ignore[arg-type]
    assert raised.value.code == "IMPORT_STATE_CONFLICT"


@pytest.mark.parametrize("status", [*NON_TERMINAL, *TERMINAL])
def test_same_status_returns_the_same_object(status: str) -> None:
    """同状态请求必须返回**原对象**，调用方据此跳过写库。"""
    current = _state(status)
    assert transition_import_job(current, status, NOW) is current  # type: ignore[arg-type]


def test_unknown_status_is_rejected() -> None:
    """不在转换图中的目标状态是调用方 bug。"""
    with pytest.raises(ValueError, match="未知的导入作业状态"):
        transition_import_job(_state(), "nonsense", NOW)  # type: ignore[arg-type]


def test_naive_datetime_is_rejected() -> None:
    """朴素时间必须拒绝：写入的更新时间会带本地时区偏移。"""
    with pytest.raises(ValueError, match="必须带时区"):
        transition_import_job(_state(), "parsing", datetime(2026, 10, 12, 9, 0, 0))


def test_every_status_has_a_transition_entry() -> None:
    """每个状态都必须在转换图里有条目（含空的终态），否则查表会 KeyError。"""
    for status in (*NON_TERMINAL, *TERMINAL):
        assert status in IMPORT_JOB_TRANSITIONS


# --------------------------------------------------------------------------
# 公开视图
# --------------------------------------------------------------------------


def test_public_view_exposes_recovery_fields() -> None:
    """公开视图必须给出 App 恢复流程所需的字段。"""
    public = create_public_import_job(_job())

    assert public.id == "job-1"
    assert public.file_name == "ledger.csv"
    assert public.status == "preview_ready"
    assert public.target_account_id == "11111111-1111-1111-1111-111111111111"
    assert public.base_ledger_version == 3
    assert public.total_rows == 10
    assert public.inserted_rows == 8
    assert public.skipped_rows == 2


def test_public_view_hides_commit_secrets_and_internal_detail() -> None:
    """文件哈希、解析器版本、预览令牌摘要与错误详情都不得公开。"""
    public = create_public_import_job(_job(error_summary="内部堆栈: ..."))
    serialized = repr(public)

    for hidden in ("file_hash", "a" * 64, "parser_version", "preview_token_hash", "b" * 64):
        assert hidden not in serialized, f"公开作业泄漏了 {hidden}"
    assert "内部堆栈" not in serialized, "公开作业泄漏了错误详情"
    assert not hasattr(public, "preview_token_hash")


def test_public_view_accepts_only_stable_error_codes() -> None:
    """稳定错误码正常通过。"""
    for code in IMPORT_JOB_ERROR_CODES:
        assert create_public_import_job(_job(error_code=code)).error_code == code
        assert is_import_job_error_code(code) is True


def test_unstable_error_code_is_rejected_not_passed_through() -> None:
    """**非稳定错误码必须被拒绝**，不能当自由文本透传给 App。"""
    leaky = "PSQLException at line 42: connection string postgres://user:pw@host"

    with pytest.raises(ValueError, match="非稳定错误码"):
        create_public_import_job(_job(error_code=leaky))
    assert is_import_job_error_code(leaky) is False


def test_error_code_set_is_a_closed_list() -> None:
    """错误码是闭集，不含自由文本入口。"""
    assert len(IMPORT_JOB_ERROR_CODES) == len(set(IMPORT_JOB_ERROR_CODES))
    assert all(code.isupper() or "_" in code for code in IMPORT_JOB_ERROR_CODES)
