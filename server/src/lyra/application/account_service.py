"""账户创建。

镜像 TypeScript 版 ``application/account-service.ts`` 的 ``createAccount``。

**三条不变量**：

1. **掩码只取标识末尾 3–4 位数字**，绝不回传文件账户标识本身——它是导入匹配键，
   泄漏它等于泄漏"哪份文件属于哪个账户"；
2. **幂等重放返回同一个账户**，不重复创建；
3. 账户、审计与幂等收口**同一事务**（见 :mod:`.account_repository`）。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

from ..domain.ledger.errors import IngestError
from ..infrastructure.db.account_repository import (
    load_brokerage_account,
    persist_created_account,
)
from ..infrastructure.db.mutation_idempotency_repository import (
    load_mutation_idempotency_record,
    start_mutation_idempotency_record,
)
from .app_error import ApplicationError
from .idempotent_mutation import (
    IdempotentMutationReplay,
    IdempotentMutationStart,
    hash_idempotency_key,
    plan_idempotent_mutation,
)

# 幂等记录的操作名。**必须与参考实现用同一个字符串**，否则两侧对同一次创建
# 会被视为两个不同的操作。
ACCOUNT_CREATE_OPERATION = "account.create"

# 账户标识：4–32 位大写字母、数字或连字符，**且以至少 3 位数字结尾**。
#
# "以数字结尾"不是格式洁癖：掩码取的就是末尾数字，没有它就没有可展示的脱敏后缀。
_ACCOUNT_IDENTIFIER = re.compile(r"^[A-Z0-9-]+[0-9]{3}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# 控制字符不得进入展示名——它们会让日志与 UI 出现不可见内容。
_CONTROL_CHARACTERS = re.compile(r"[\u0000-\u001f\u007f-\u009f]")

# 校验失败时的文案**逐项列出**，便于客户端定位是哪一个字段的问题。
_INVALID_MESSAGE = (
    "账户名称须为 1 至 64 个字符；"
    "文件账户标识须为 4 至 32 位字母、数字或连字符并以至少 3 位数字结尾；"
    "币种须为 3 个字母"
)


def _invalid() -> ApplicationError:
    """构造统一的参数错误。"""
    return ApplicationError(
        code="INVALID_ARGUMENT", message=_INVALID_MESSAGE, status_code=400, retryable=False
    )


def validate_account_request(body: Any) -> dict[str, str]:
    """校验并归一化账户创建正文。

    @param body: 请求正文。
    @returns ``{display_name, legacy_account_key, base_currency}``。
    @raises ApplicationError: 任一字段非法（400）。
    """
    if not isinstance(body, dict):
        raise _invalid()
    display_name = body.get("displayName")
    identifier = body.get("accountIdentifier")
    currency = body.get("baseCurrency", "USD")
    if not isinstance(display_name, str) or not isinstance(identifier, str):
        raise _invalid()
    if not isinstance(currency, str):
        raise _invalid()

    display_name = display_name.strip()
    identifier = identifier.strip().upper()
    currency = currency.strip().upper()

    if not 1 <= len(display_name) <= 64 or _CONTROL_CHARACTERS.search(display_name):
        raise _invalid()
    if not 4 <= len(identifier) <= 32 or not _ACCOUNT_IDENTIFIER.fullmatch(identifier):
        raise _invalid()
    if not _CURRENCY.fullmatch(currency):
        raise _invalid()
    return {
        "display_name": display_name,
        "legacy_account_key": identifier,
        "base_currency": currency,
    }


def masked_suffix_of(identifier: str) -> str:
    """取出可展示的脱敏后缀——**标识末尾 3 或 4 位数字**。

    @param identifier: 已归一化的账户标识（保证以 ≥3 位数字结尾）。
    @returns 后缀。
    """
    match = re.search(r"[0-9]{3,4}$", identifier)
    # 校验保证了至少 3 位数字结尾，因此这里必能匹配。
    assert match is not None
    return match[0]


def create_account(
    connection: Any,
    *,
    body: Any,
    device_id: str,
    request_id: str,
    idempotency_key: str | None,
    now: datetime,
    account_id: str | None = None,
    audit_id: str | None = None,
) -> tuple[dict[str, object], bool]:
    """创建一个账户（幂等）。

    @param connection: 数据库连接。
    @param body: 请求正文。
    @param device_id: 发起设备 UUID。
    @param request_id: 请求 ID，写入审计便于关联日志。
    @param idempotency_key: 明文幂等键；``None`` 表示客户端未要求幂等。
    @param now: 服务端可信时间。
    @param account_id: 新账户 ID；测试注入用。
    @param audit_id: 审计事件 ID；测试注入用。
    @returns ``(账户行, 是否为重放)``。
    @raises ApplicationError: 参数非法（400）或幂等键被复用于不同请求（409）。
    """
    request = validate_account_request(body)

    planned = None
    if idempotency_key is not None:
        planned = plan_idempotent_mutation(
            record_id=str(uuid.uuid4()),
            device_id=device_id,
            operation=ACCOUNT_CREATE_OPERATION,
            key=idempotency_key,
            # 请求指纹取自**归一化后**的条件：大小写或空白不同不该算作不同请求。
            request_hash=hashlib.sha256(
                json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            existing_record=load_mutation_idempotency_record(
                connection, device_id=device_id, operation=ACCOUNT_CREATE_OPERATION,
                key_hash=hash_idempotency_key(idempotency_key),
            ),
            now=now,
        )

    if isinstance(planned, IdempotentMutationReplay):
        existing = load_brokerage_account(connection, planned.result_resource_id)
        if existing is None:
            # 幂等记录说"已完成"，但目标账户不存在——数据库处于不一致状态。
            # **宁可报错也不静默重建**：静默重建会掩盖一次真实的数据损坏。
            raise ApplicationError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="幂等记录指向的账户不存在",
                status_code=409,
                retryable=False,
            )
        return existing, True

    new_id = account_id or str(uuid.uuid4())
    created_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    account: dict[str, object] = {
        "id": new_id,
        "display_name": request["display_name"],
        # **文件账户标识本身不入公开响应**，但必须落库——它是导入时的匹配键。
        "legacy_account_key": request["legacy_account_key"],
        "broker": "schwab",
        "masked_suffix": masked_suffix_of(request["legacy_account_key"]),
        "base_currency": request["base_currency"],
        "status": "active",
        "created_at": created_at,
    }
    audit: dict[str, object] = {
        "id": audit_id or str(uuid.uuid4()),
        "occurred_at": created_at,
        "event_type": "account.created",
        "outcome": "success",
        "actor_id": device_id,
        "request_id": request_id,
        "target_type": "account",
        "target_id": new_id,
        "account_id": new_id,
        "metadata_json": json.dumps(
            {"broker": account["broker"], "base_currency": account["base_currency"]},
            ensure_ascii=False,
        ),
    }
    completion = None
    if isinstance(planned, IdempotentMutationStart):
        # **登记必须在写入之前。** 若等到事务内才登记，并发的同键请求会各自走到这里、
        # 各建一个账户——幂等在最需要它的场景失效。
        #
        # 这一步此前漏了，症状是首次调用时收口找不到记录并抛
        # ``ValueError: 幂等记录完成收口失败``——**因为记录从来没被创建过**。
        start_mutation_idempotency_record(
            connection, record=planned.record,
            replaces_expired_record_id=planned.replaces_expired_record_id,
        )
        completion = (planned.record.id, new_id, 201, now)
    persist_created_account(
        connection, account=account, audit_event=audit, idempotency_completion=completion
    )
    return account, False
