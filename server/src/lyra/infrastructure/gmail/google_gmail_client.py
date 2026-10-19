"""Gmail API 客户端。

镜像 TypeScript 版 ``infrastructure/gmail/google-gmail-client.ts`` 的端点常量与
响应解析部分。

**最要紧的一处是 message id 的字符集校验。**

```
^[A-Za-z0-9_-]{1,256}$
```

message id 会被拼进 API 路径（``/messages/{id}``）。不校验字符集，一个含 ``/`` 或
``..`` 的 id 就能把请求指向另一个端点——而 Gmail 返回的 id 本就只有这个字符集，
放宽没有任何收益。同理，含 ``/`` 或空白的 id 进入日志也会破坏日志结构。

解析策略是**结构不符即报错**，而不是尽力而为地跳过：一次静默跳过会让某封邮件永远
不被导入，而调用方以为同步成功了。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any

from ...domain.gmail.econfirm import (
    SCHWAB_ECONFIRM_SUBJECT,
    is_schwab_sender,
)
from ...domain.gmail.sync_plan import GmailSyncError
from .base64url import decode_base64url

GMAIL_MESSAGES_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_HISTORY_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/history"
GMAIL_PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# Gmail 单页上限由接口规定，超过会被拒绝。
GMAIL_MAX_PAGE_SIZE = 500

GMAIL_REQUEST_TIMEOUT_MILLISECONDS = 15_000

# messages.get(format=full) 配额消耗较高。只对明确的短期速率限制做有限退避；
# 日配额、权限和配置错误不能靠快速重试恢复。
GMAIL_RATE_LIMIT_MAX_RETRIES = 6
GMAIL_RATE_LIMIT_BASE_DELAY_SECONDS = 1
GMAIL_RATE_LIMIT_JITTER_SECONDS = 0.25

GMAIL_TEXT_MIME_TYPES: tuple[str, ...] = ("text/plain", "text/html")

# 同 upper bound 256：Gmail 的 id 不会更长，超长的必然是异常输入。
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def _invalid_response(message: str) -> GmailSyncError:
    """构造"Gmail 响应结构不符"的错误。

    统一用 ``GMAIL_CONNECTION_ERROR``：结构不符说明这次交互不可信，需要重试或排查，
    而不是把它当成"没有新邮件"。
    """
    return GmailSyncError("GMAIL_CONNECTION_ERROR", message)


def parse_message_id(value: Any, message: str) -> str:
    """校验并返回一个 Gmail message id。

    @param value: 供应商返回值。
    @param message: 缺失时的说明。
    @returns 校验通过的 id。
    @raises GmailSyncError: 缺失或字符集不符。
    """
    if not isinstance(value, str):
        raise _invalid_response(message)
    normalized = value.strip()
    if normalized == "":
        raise _invalid_response(message)
    if not _MESSAGE_ID.fullmatch(normalized):
        # **路径注入防线**：id 会被拼进 API 路径。
        raise _invalid_response("Gmail 返回非法 message id")
    return normalized


def parse_message_ids(value: Any) -> tuple[str, ...]:
    """解析 ``messages.list`` 的 ``messages`` 数组。

    @param value: 供应商返回值；``None`` 表示没有消息。
    @returns 消息 ID 序列。
    @raises GmailSyncError: 结构不符。
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _invalid_response("messages.list 的 messages 不是数组")
    message_ids: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise _invalid_response("messages.list 项不是对象")
        message_ids.append(
            parse_message_id(item.get("id"), "messages.list 缺少 message id")
        )
    return tuple(message_ids)


def parse_added_message_ids(value: Any) -> tuple[str, ...]:
    """解析 ``history.list`` 里新增的消息 ID。

    Gmail 把新增消息嵌套在 ``history[].messagesAdded[].message.id``；
    只取 ``messagesAdded``，忽略 ``messagesDeleted`` / ``labelsAdded`` 等其它变更——
    本同步只关心**新出现的邮件**。

    @param value: 供应商返回值；``None`` 表示没有历史记录。
    @returns 消息 ID 序列（**保持出现顺序**，可含重复）。
    @raises GmailSyncError: 结构不符。
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _invalid_response("history.list 的 history 不是数组")
    message_ids: list[str] = []
    for history in value:
        if not isinstance(history, dict):
            raise _invalid_response("history.list 项不是对象")
        added = history.get("messagesAdded")
        if added is None:
            continue
        if not isinstance(added, list):
            raise _invalid_response("history.list 的 messagesAdded 不是数组")
        for entry in added:
            if not isinstance(entry, dict) or not isinstance(entry.get("message"), dict):
                raise _invalid_response("history.list 的 messageAdded 项无效")
            message_ids.append(parse_message_id(
                entry["message"].get("id"), "messageAdded 缺少 message id"
            ))
    return tuple(message_ids)


def is_schwab_econfirm_metadata(metadata: Any) -> bool:
    """判断一封邮件的元数据是否指向 Schwab eConfirm 通知。

    这是**在拉取正文之前**的过滤：元数据便宜、正文昂贵，先把无关邮件筛掉。

    头部名**大小写不敏感**（HTTP 头本身如此），重复头取**最后一个**——与参考实现
    一致。

    @param metadata: ``messages.get`` 的 ``format=metadata`` 响应。
    @returns 是否匹配。
    @raises GmailSyncError: 缺少 ``payload.headers``。
    """
    if not isinstance(metadata, dict):
        raise _invalid_response("messages.get metadata 缺少 payload.headers")
    payload = metadata.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("headers"), list):
        raise _invalid_response("messages.get metadata 缺少 payload.headers")
    headers: dict[str, str] = {}
    for header in payload["headers"]:
        if (
            not isinstance(header, dict)
            or not isinstance(header.get("name"), str)
            or not isinstance(header.get("value"), str)
        ):
            raise _invalid_response("messages.get metadata header 无效")
        headers[header["name"].lower()] = header["value"]
    sender = headers.get("from", "")
    subject = headers.get("subject", "")
    return is_schwab_sender(sender) and SCHWAB_ECONFIRM_SUBJECT in subject


@dataclass(frozen=True)
class GmailProfile:
    """Gmail 账户档案。"""

    email_address: str
    history_id: str
    messages_total: int
    threads_total: int


def parse_gmail_profile(payload: Any, *, expected_email: str) -> GmailProfile:
    """解析 Gmail profile 并核对邮箱归属。

    **profile 的邮箱必须与已连接的 Google 邮箱一致。** 不核对就可能用一个账户的令牌
    去读另一个账户的邮箱——而返回的数据看起来完全正常。

    @param payload: profile 响应。
    @param expected_email: 授权时已验证的邮箱（已小写）。
    @returns 档案。
    @raises GmailSyncError: 结构不符或邮箱不一致。
    """
    expected = expected_email.strip().lower()
    if expected == "":
        raise _invalid_response("已验证 Google 邮箱不能为空")
    if not isinstance(payload, dict):
        raise _invalid_response("Gmail profile 响应无效")
    raw_email = payload.get("emailAddress")
    if not isinstance(raw_email, str):
        raise _invalid_response("Gmail profile 邮箱无效")
    email_address = raw_email.strip().lower()
    if _EMAIL.fullmatch(email_address) is None:
        raise _invalid_response("Gmail profile 邮箱无效")
    if email_address != expected:
        # 令牌与已连接的账户不是同一个：立即停止，而不是读出别人的邮件。
        raise _invalid_response("Gmail profile 与已连接的 Google 邮箱不一致")
    return GmailProfile(
        email_address=email_address,
        history_id=_require_history_id(payload.get("historyId")),
        messages_total=_require_non_negative(payload.get("messagesTotal"), "messagesTotal"),
        threads_total=_require_non_negative(payload.get("threadsTotal"), "threadsTotal"),
    )


@dataclass(frozen=True)
class BoundedGmailRecords:
    """一次有界扫描的结果。"""

    message_ids: tuple[str, ...]
    truncated: bool


def fetch_bounded_gmail_message_ids(
    access_token: str, plan: Any, *, get_json: Any
) -> BoundedGmailRecords:
    """按有界查询分页列出消息 ID。

    **两道守卫**：

    1. **同一 message id 出现在多页里 → 报错。** Gmail 不该这样返回；重复会让同一封
       邮件被处理两次（而导入层的幂等检查是在更后面才起作用）。
    2. **重复的 ``nextPageToken`` → 报错。** 这是**死循环防线**：一个总是返回同一个
       token 的（有 bug 或被篡改的）响应会让循环永远转下去，不断打 API。

    @param access_token: Google 临时令牌。
    @param plan: 有界查询计划（``query`` / ``maximum_messages``）。
    @param get_json: ``(url, headers, timeout_seconds) -> (status, payload)``。
    @returns 消息 ID 与是否被截断。
    @raises ValueError: 上限不是正整数或令牌为空。
    @raises GmailSyncError: 响应结构不符或触发守卫。
    """
    token = access_token.strip()
    if token == "":
        raise ValueError("Google access token不能为空")
    if not isinstance(plan.query, str) or plan.query.strip() == "":
        raise ValueError("Gmail 查询不能为空")
    maximum = plan.maximum_messages
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        raise ValueError("Gmail 有界扫描上限必须是正安全整数")

    message_ids: list[str] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None
    truncated = False

    while True:
        remaining = maximum - len(message_ids)
        params = {
            "q": plan.query,
            "maxResults": str(min(GMAIL_MAX_PAGE_SIZE, remaining)),
        }
        if page_token is not None:
            params["pageToken"] = page_token
        page = request_gmail_json(
            f"{GMAIL_MESSAGES_ENDPOINT}?{_urlencode(params)}",
            token,
            get_json=get_json,
        )
        page_ids = parse_message_ids(page.get("messages"))
        for message_id in page_ids:
            if message_id in seen_ids:
                raise _invalid_response("messages.list 返回重复 message id")
            seen_ids.add(message_id)
            message_ids.append(message_id)
            if len(message_ids) == maximum:
                break

        next_token = _optional_text(page.get("nextPageToken"), "nextPageToken")
        if len(message_ids) == maximum:
            # 达到上限即被截断：还有下一页，或这一页本身就超出了剩余额度。
            truncated = next_token is not None or len(page_ids) > remaining
            break
        if next_token is not None:
            if next_token in seen_tokens:
                # 死循环防线。
                raise _invalid_response("messages.list 返回重复 page token")
            seen_tokens.add(next_token)
        page_token = next_token
        if page_token is None:
            break

    return BoundedGmailRecords(message_ids=tuple(message_ids), truncated=truncated)


def _optional_text(value: Any, field: str) -> str | None:
    """读取可选文本；存在但为空必须报错。"""
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise _invalid_response(f"Gmail {field} 无效")
    return value.strip()


def _require_history_id(value: Any) -> str:
    """history id 是 Google 返回的十进制不透明游标。"""
    from ...domain.gmail.sync_state import normalize_gmail_history_id

    if not isinstance(value, str):
        raise _invalid_response("Gmail profile history id 无效")
    try:
        return normalize_gmail_history_id(value)
    except ValueError as error:
        raise _invalid_response("Gmail profile history id 无效") from error


def _require_non_negative(value: Any, label: str) -> int:
    """读取非负整数。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid_response(f"Gmail {label} 无效")
    return value


def _urlencode(params: dict[str, str]) -> str:
    """URL 编码查询参数。"""
    from urllib.parse import urlencode

    return urlencode(params)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


@dataclass(frozen=True)
class IncrementalGmailRecords:
    """一次增量拉取的结果。"""

    message_ids: tuple[str, ...]
    observed_history_id: str
    truncated: bool


def fetch_incremental_gmail_message_ids(
    access_token: str, *, start_history_id: str, maximum_messages: int, get_json: Any
) -> IncrementalGmailRecords:
    """按 history 增量列出新增的消息 ID。

    **与有界扫描有一处刻意不同的处理：重复 id 跳过而不是报错。**

    ``history.list`` 会为同一封邮件返回多条 history 记录（例如它同时被加了标签），
    因此重复是**正常的**，不是响应损坏。而有界扫描走的是 ``messages.list``，
    那里重复返回意味着响应有问题。两处行为分别与各自接口的语义匹配。

    另一处：每次响应里的 ``historyId`` 都覆盖上一次，**以最后一页为准**——它标记的是
    "本次增量读到了哪个时点"，而不是起始时点。

    @param access_token: Google 临时令牌。
    @param start_history_id: 上次同步完成时的游标。
    @param maximum_messages: 本次最多处理的消息数。
    @param get_json: ``(url, headers, timeout_seconds) -> (status, payload)``。
    @returns 消息 ID、观察到的游标与是否被截断。
    @raises ValueError: 上限或游标非法。
    @raises GmailSyncError: 响应结构不符或触发守卫。
    """
    from ...domain.gmail.sync_state import normalize_gmail_history_id

    token = access_token.strip()
    if token == "":
        raise ValueError("Google access token不能为空")
    start = normalize_gmail_history_id(start_history_id)
    if (
        not isinstance(maximum_messages, int)
        or isinstance(maximum_messages, bool)
        or maximum_messages <= 0
    ):
        raise ValueError("Gmail 增量消息上限必须是正安全整数")

    message_ids: list[str] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None
    observed_history_id: str | None = None
    # **必须在循环前初始化**：未达到上限时循环会从别处退出，那条路径也要读到它。
    truncated = False

    while True:
        remaining = maximum_messages - len(message_ids)
        params = {
            "startHistoryId": start,
            # **服务端过滤**：只要新增消息，``history.list`` 默认会返回标签变更等
            # 与导入无关的记录。
            "historyTypes": "messageAdded",
            "maxResults": str(min(GMAIL_MAX_PAGE_SIZE, remaining)),
        }
        if page_token is not None:
            params["pageToken"] = page_token
        page = request_gmail_json(
            f"{GMAIL_HISTORY_ENDPOINT}?{_urlencode(params)}",
            token,
            get_json=get_json,
            history_404_means_expired=True,
        )
        # 每一页都覆盖：最终值标记"本次读到了哪个时点"。
        observed_history_id = _require_history_id(page.get("historyId"))

        page_ids = parse_added_message_ids(page.get("history"))
        exceeded_within_page = False
        for message_id in page_ids:
            if message_id in seen_ids:
                # 重复是正常的：同一封邮件可能出现在多条 history 记录里。
                continue
            if len(message_ids) == maximum_messages:
                exceeded_within_page = True
                break
            seen_ids.add(message_id)
            message_ids.append(message_id)

        next_token = _optional_text(page.get("nextPageToken"), "nextPageToken")
        if len(message_ids) == maximum_messages:
            truncated = exceeded_within_page or next_token is not None
            break
        if next_token is not None:
            if next_token in seen_tokens:
                # 死循环防线，与有界扫描一致。
                raise _invalid_response("history.list 返回重复 page token")
            seen_tokens.add(next_token)
        page_token = next_token
        if page_token is None:
            break

    if observed_history_id is None:  # pragma: no cover - 循环至少执行一次
        raise _invalid_response("history.list 缺少 observed history id")
    return IncrementalGmailRecords(
        message_ids=tuple(message_ids),
        observed_history_id=observed_history_id,
        truncated=truncated,
    )


def _google_error_reasons(payload: Any) -> set[str]:
    """从 Google 错误响应中提取稳定 reason，不保留供应商正文。

    @param payload: Google 错误响应负载。
    @returns 有界提取出的 reason 集合；负载结构不符时为空集合。
    """
    reasons: set[str] = set()
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        for field in ("errors", "details"):
            items = error.get(field)
            if isinstance(items, list):
                for item in items[:50]:
                    if isinstance(item, dict) and isinstance(item.get("reason"), str):
                        reasons.add(item["reason"])
    return reasons


def _is_retryable_rate_limit(status: int, payload: Any) -> bool:
    """判断错误是否为可短暂恢复的 per-user Gmail 速率限制。

    HTTP 429 按临时限流处理；403 仅在 reason 明确为 rateLimitExceeded 或
    userRateLimitExceeded 时重试。日限额与配额错误优先视为不可通过短暂等待恢复。

    @param status: HTTP 状态码。
    @param payload: Google 错误响应负载。
    @returns 是否进入有限指数退避。
    """
    reasons = _google_error_reasons(payload)
    if reasons & {
        "dailyLimitExceeded", "quotaExceeded", "insufficientPermissions",
        "SERVICE_DISABLED", "accessNotConfigured", "domainPolicy",
    }:
        return False
    if status == 429:
        return True
    if status != 403:
        return False
    return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})


def gmail_http_error(status: int, payload: Any = None) -> GmailSyncError:
    """仅按状态码与 Google 稳定原因区分授权、配置、配额及访问限制。

    明确的 per-user 速率限制可重试；``dailyLimitExceeded`` 与 ``quotaExceeded``
    仍返回同一错误码，但不重试，也不推断配额周期。

    @param status: HTTP 状态码。
    @param payload: Google 错误对象，仅检查稳定 reason；原始正文绝不向上返回。
    @returns 带安全说明的同步错误，不将所有 403 误报成授权失效。
    """
    reasons = _google_error_reasons(payload)
    if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
        return GmailSyncError(
            "GMAIL_API_DISABLED", "Google Cloud 项目尚未启用 Gmail API，请启用后重试"
        )
    if status == 401 or "insufficientPermissions" in reasons:
        return GmailSyncError("GMAIL_AUTH_REVOKED", "Gmail 授权已失效，需要重新授权")
    if reasons & {"dailyLimitExceeded", "quotaExceeded"}:
        return GmailSyncError(
            "GMAIL_RATE_LIMITED",
            "Gmail 配额已用尽，请检查 Google Cloud 项目配额后重试",
            retryable=False,
        )
    if "domainPolicy" in reasons:
        return GmailSyncError(
            "GMAIL_ACCESS_DENIED", "Google 拒绝访问 Gmail，请检查邮箱权限或管理员策略"
        )
    if status == 429 or (status == 403 and reasons & {
        "rateLimitExceeded", "userRateLimitExceeded",
    }):
        return GmailSyncError(
            "GMAIL_RATE_LIMITED", "Gmail 请求达到限额，请稍后再试", retryable=True
        )
    if status == 403 or "domainPolicy" in reasons:
        return GmailSyncError(
            "GMAIL_ACCESS_DENIED", "Google 拒绝访问 Gmail，请检查邮箱权限或管理员策略"
        )
    return GmailSyncError(
        "GMAIL_CONNECTION_ERROR", "Gmail 请求失败，请稍后重试",
        retryable=status == 429 or status >= 500,
    )


def request_gmail_json(
    url: str,
    access_token: str,
    *,
    get_json: Any,
    history_404_means_expired: bool = False,
) -> dict:
    """发起一次 Gmail JSON 请求并做统一错误映射。

    **``history.list`` 的 404 表示游标已过期。** Google 只保留大约一周的历史；把 404
    当成一般的连接错误，会让客户端拿着一个**永久失效的游标**反复重试，而正确的动作是
    让用户确认重扫范围。

    只有 HTTP 429 和明确的 per-user 速率限制做最多六次有界退避；日配额、授权、配置、
    访问拒绝、格式错误与传输异常不进入此重试循环。

    @param url: 完整地址。
    @param access_token: Google 临时令牌。
    @param get_json: ``(url, headers, timeout_seconds) -> (status, payload)``。
    @param history_404_means_expired: 该请求是否为 ``history.list``。
    @returns 响应对象。
    @raises GmailSyncError: 鉴权失效、游标过期、限流耗尽、临时故障或响应结构不符。
    """
    token = access_token.strip()
    if token == "":
        raise ValueError("Google access token不能为空")
    for retry_number in range(GMAIL_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            status, payload = get_json(
                url, {"authorization": f"Bearer {token}"},
                GMAIL_REQUEST_TIMEOUT_MILLISECONDS / 1000,
            )
        except Exception as error:
            raise GmailSyncError(
                "GMAIL_CONNECTION_ERROR", "无法连接 Gmail，请稍后重试", retryable=True
            ) from error

        if 200 <= status < 300:
            if not isinstance(payload, dict):
                raise _invalid_response("Gmail JSON 根节点不是对象")
            return payload

        if history_404_means_expired and status == 404:
            # 游标太旧，Google 已经清理了那段历史；不要误当成限流去等待。
            raise GmailSyncError(
                "GMAIL_HISTORY_EXPIRED", "Gmail 增量游标已过期，必须确认重扫范围"
            )

        error = gmail_http_error(status, payload)
        if error.code != "GMAIL_RATE_LIMITED" or not _is_retryable_rate_limit(
            status, payload
        ):
            raise error
        if retry_number == GMAIL_RATE_LIMIT_MAX_RETRIES:
            # 第七次响应仍是临时限流时，保留安全错误分类并交由上层处理。
            raise error

        base_delay = GMAIL_RATE_LIMIT_BASE_DELAY_SECONDS * (2 ** retry_number)
        jitter = random.uniform(0, GMAIL_RATE_LIMIT_JITTER_SECONDS)
        # 只在明确临时限流时等待，且在下一次只读 GET 前退避。
        time.sleep(base_delay + jitter)

    raise AssertionError("Gmail 限流重试循环应在返回或抛错时结束")  # pragma: no cover


def hydrate_gmail_text_attachments(
    record: Any, *, message_id: str, access_token: str, get_json: Any
) -> Any:
    """把以附件形式返回的文本正文水合回 ``body.data``。

    **Gmail 会把较大的正文作为附件返回，而不是内联的 ``body.data``。** 只读
    ``body.data`` 的话，那类邮件会拿到**空正文**，eConfirm 解析随之报出"正文中没有
    交易块"——一个与真正原因（正文在附件里）无关的提示。

    @param record: ``messages.get`` 响应。
    @param message_id: 消息 ID。
    @param access_token: Google 临时令牌。
    @param get_json: ``(url, headers, timeout_seconds) -> (status, payload)``。
    @returns 水合后的响应（结构不适用时原样返回）。
    """
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return record
    hydrated = dict(record)
    hydrated["payload"] = _hydrate_gmail_part(
        record["payload"], message_id=message_id,
        access_token=access_token, get_json=get_json,
    )
    return hydrated


def _hydrate_gmail_part(
    part: dict, *, message_id: str, access_token: str, get_json: Any
) -> dict:
    """递归水合一个 MIME 部分。"""
    hydrated = dict(part)
    mime_type = part.get("mimeType")
    normalized_type = mime_type.lower() if isinstance(mime_type, str) else ""
    body = part.get("body")
    if normalized_type in GMAIL_TEXT_MIME_TYPES and isinstance(body, dict):
        inline = body.get("data")
        has_inline = isinstance(inline, str) and inline != ""
        if not has_inline and body.get("attachmentId") is not None:
            attachment_id = require_attachment_id(body["attachmentId"])
            payload = request_gmail_json(
                f"{GMAIL_MESSAGES_ENDPOINT}/{_encode_path(message_id)}"
                f"/attachments/{_encode_path(attachment_id)}",
                access_token, get_json=get_json,
            )
            hydrated["body"] = {
                **body, "data": require_base64url_data(payload.get("data")),
            }

    children = part.get("parts")
    if children is not None:
        if not isinstance(children, list):
            raise _invalid_response("Gmail MIME parts 必须是数组")
        hydrated["parts"] = [
            _hydrate_gmail_part(child, message_id=message_id,
                                access_token=access_token, get_json=get_json)
            if isinstance(child, dict)
            else _raise_part_not_object()
            for child in children
        ]
    return hydrated


def require_attachment_id(value: Any) -> str:
    """校验附件 ID。

    **同样是路径注入防线**：附件 ID 会被拼进 API 路径。
    """
    if not isinstance(value, str) or not _ATTACHMENT_ID.fullmatch(value):
        raise _invalid_response("Gmail text attachment id 无效")
    return value


def require_base64url_data(value: Any) -> str:
    """校验 Gmail 附件的规范 Base64URL 数据并原样返回。

    Gmail 可返回带 ``=`` 填充或无填充编码；两者都须符合严格字符集、填充和 pad bits
    规范，避免把畸形供应商响应交给正文解析器猜测。

    @param value: Gmail attachment.data 中的 Base64URL 字符串。
    @returns 验证通过的原字符串，供 MIME 层解码。
    @raises GmailSyncError: 数据为空或不是规范 Base64URL。
    """
    if not isinstance(value, str) or value == "":
        raise _invalid_response("Gmail text attachment data 无效")
    try:
        decode_base64url(value)
    except ValueError as error:
        raise _invalid_response("Gmail text attachment data 无效") from error
    return value


def _encode_path(value: str) -> str:
    """URL 编码路径片段。"""
    from urllib.parse import quote

    return quote(value, safe="")


def _raise_part_not_object() -> dict:
    """在推导式里抛出结构错误。"""
    raise _invalid_response("Gmail MIME part 必须是对象")


_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,2048}$")
def fetch_full_gmail_records(
    message_ids: tuple[str, ...], access_token: str, *, get_json: Any
) -> tuple[dict, ...]:
    """逐条拉取完整消息并水合文本附件。

    **响应里回显的 ``id`` 必须与请求的一致。** 不核对就可能把另一封邮件的内容当成
    这一封来解析——而解析结果看起来完全正常（符号、数量、价格都在），
    **只是属于另一封邮件**。

    与行情侧的"符号回显校验"是同一条原则。

    @param message_ids: 要拉取的消息 ID。
    @param access_token: Google 临时令牌。
    @param get_json: ``(url, headers, timeout_seconds) -> (status, payload)``。
    @returns 完整消息记录（顺序与入参一致）。
    @raises GmailSyncError: 响应 id 不一致或结构不符。
    """
    records: list[dict] = []
    for message_id in message_ids:
        record = request_gmail_json(
            f"{GMAIL_MESSAGES_ENDPOINT}/{_encode_path(message_id)}?format=full",
            access_token, get_json=get_json,
        )
        if record.get("id") != message_id:
            raise _invalid_response("messages.get 响应 id 与请求不一致")
        records.append(hydrate_gmail_text_attachments(
            record, message_id=message_id, access_token=access_token, get_json=get_json
        ))
    return tuple(records)


@dataclass(frozen=True)
class IncrementalGmailFullRecords:
    """增量同步的完整结果。"""

    records: tuple[dict, ...]
    ignored_message_count: int


def fetch_incremental_gmail_full_records(
    message_ids: tuple[str, ...], access_token: str, *, get_json: Any
) -> IncrementalGmailFullRecords:
    """先用元数据过滤，再拉取完整正文。

    **元数据便宜、正文昂贵。** 增量结果里可能混有大量非 eConfirm 邮件（用户转发、
    回复都会新增消息），先按发件人与主题筛掉，只对候选拉正文。

    ``ignored_message_count`` 是"看过但不需要"的数量——它让调用方能在诊断里区分
    "这段时间没有邮件"与"有邮件但都不相关"，两者的处置完全不同。

    @param message_ids: 候选消息 ID。
    @param access_token: Google 临时令牌。
    @param get_json: ``(url, headers, timeout_seconds) -> (status, payload)``。
    @returns 相关记录的完整内容与被忽略的数量。
    """
    relevant_ids: list[str] = []
    for message_id in message_ids:
        metadata = request_gmail_json(
            f"{GMAIL_MESSAGES_ENDPOINT}/{_encode_path(message_id)}?format=metadata",
            access_token, get_json=get_json,
        )
        # 同样核对回显 id。
        if metadata.get("id") != message_id:
            raise _invalid_response("messages.get metadata 响应 id 与请求不一致")
        if is_schwab_econfirm_metadata(metadata):
            relevant_ids.append(message_id)
    return IncrementalGmailFullRecords(
        records=fetch_full_gmail_records(
            tuple(relevant_ids), access_token, get_json=get_json
        ),
        ignored_message_count=len(message_ids) - len(relevant_ids),
    )
