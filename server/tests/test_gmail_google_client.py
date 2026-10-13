"""Gmail API 响应解析测试。

最要紧的一条：**message id 的字符集校验**。

id 会被拼进 API 路径（``/messages/{id}``）。不校验字符集，一个含 ``/`` 或 ``..``
的 id 就能把请求指向另一个端点——而 Gmail 返回的 id 本就只有这个字符集，放宽没有
任何收益。

策略是**结构不符即报错**，而不是尽力而为地跳过：一次静默跳过会让某封邮件永远不被
导入，而调用方以为同步成功了。
"""

from __future__ import annotations

import pytest

from lyra.domain.gmail.sync_plan import GmailSyncError
from lyra.infrastructure.gmail.google_gmail_client import (
    GMAIL_HISTORY_ENDPOINT,
    GMAIL_MAX_PAGE_SIZE,
    GMAIL_MESSAGES_ENDPOINT,
    GMAIL_PROFILE_ENDPOINT,
    GMAIL_REQUEST_TIMEOUT_MILLISECONDS,
    GMAIL_TEXT_MIME_TYPES,
    fetch_bounded_gmail_message_ids,
    fetch_full_gmail_records,
    fetch_incremental_gmail_full_records,
    fetch_incremental_gmail_message_ids,
    gmail_http_error,
    hydrate_gmail_text_attachments,
    is_schwab_econfirm_metadata,
    parse_added_message_ids,
    parse_gmail_profile,
    parse_message_id,
    parse_message_ids,
    request_gmail_json,
    require_attachment_id,
    require_base64url_data,
)

SCHWAB_FROM = "Schwab <donotreply@mail.schwab.com>"


def _metadata(from_value: str = SCHWAB_FROM, subject: str = "Schwab eConfirms", **extra):
    """构造一份 metadata 响应。"""
    headers = [
        {"name": "From", "value": from_value},
        {"name": "Subject", "value": subject},
    ]
    headers.extend(extra.get("headers", []))
    return {"payload": {"headers": headers}}


# --------------------------------------------------------------------------
# 端点常量
# --------------------------------------------------------------------------


def test_endpoint_constants() -> None:
    """端点为 Gmail API 的正式地址，单页上限 500，超时 15 秒。"""
    assert GMAIL_MESSAGES_ENDPOINT == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    )
    assert GMAIL_HISTORY_ENDPOINT == (
        "https://gmail.googleapis.com/gmail/v1/users/me/history"
    )
    assert GMAIL_PROFILE_ENDPOINT == (
        "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    )
    assert GMAIL_MAX_PAGE_SIZE == 500
    assert GMAIL_REQUEST_TIMEOUT_MILLISECONDS == 15_000
    assert GMAIL_TEXT_MIME_TYPES == ("text/plain", "text/html")


# --------------------------------------------------------------------------
# message id 校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["abc", "a" * 256, "A-b_c9", "18f2b3"])
def test_accepts_valid_message_ids(value: str) -> None:
    """Gmail 的 id 字符集通过。"""
    assert parse_message_id(value, "missing") == value


@pytest.mark.parametrize(
    "value",
    [
        "", "  ",
        "a/b",             # **路径穿越**
        "../messages",     # 相对路径
        "a b",             # 空白会破坏日志与 URL
        "a?b", "a#b", "a%b",
        "a" * 257,         # 超长
        "日本語",
    ],
)
def test_rejects_invalid_message_ids(value: str) -> None:
    """**字符集不符必须拒绝**——id 会被拼进 API 路径。"""
    with pytest.raises(GmailSyncError) as raised:
        parse_message_id(value, "missing")

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"


@pytest.mark.parametrize("value", [None, 5, [], {}])
def test_rejects_non_string_message_ids(value: object) -> None:
    """非字符串 id 拒绝。"""
    with pytest.raises(GmailSyncError):
        parse_message_id(value, "missing")


def test_missing_message_id_uses_the_caller_message() -> None:
    """缺失时给出调用方指定的说明，便于定位。"""
    with pytest.raises(GmailSyncError, match="messages.list 缺少"):
        parse_message_id(None, "messages.list 缺少 message id")


# --------------------------------------------------------------------------
# messages.list
# --------------------------------------------------------------------------


def test_parses_message_ids() -> None:
    """正常解析。"""
    assert parse_message_ids([{"id": "m1"}, {"id": "m2"}]) == ("m1", "m2")


def test_absent_messages_is_an_empty_result() -> None:
    """字段缺失表示没有消息，不是错误。"""
    assert parse_message_ids(None) == ()


def test_non_array_messages_is_rejected() -> None:
    """不是数组必须报错。"""
    with pytest.raises(GmailSyncError, match="不是数组"):
        parse_message_ids({"id": "m1"})


def test_non_object_item_is_rejected() -> None:
    """数组项不是对象必须报错。"""
    with pytest.raises(GmailSyncError, match="项不是对象"):
        parse_message_ids(["m1"])


# --------------------------------------------------------------------------
# history.list
# --------------------------------------------------------------------------


def test_parses_added_message_ids() -> None:
    """解析嵌套的 ``messagesAdded[].message.id``。"""
    assert parse_added_message_ids([
        {"messagesAdded": [{"message": {"id": "m1"}}]},
        {"messagesAdded": [{"message": {"id": "m2"}}, {"message": {"id": "m3"}}]},
    ]) == ("m1", "m2", "m3")


def test_history_entries_without_added_messages_are_skipped() -> None:
    """**只取 ``messagesAdded``。**

    ``messagesDeleted`` / ``labelsAdded`` 等变更与导入无关，跳过它们而不是报错。
    """
    assert parse_added_message_ids([
        {"messagesDeleted": [{"message": {"id": "gone"}}]},
        {"labelsAdded": [{"message": {"id": "labelled"}}]},
        {"messagesAdded": [{"message": {"id": "m1"}}]},
    ]) == ("m1",)


def test_absent_history_is_an_empty_result() -> None:
    """没有历史记录时返回空。"""
    assert parse_added_message_ids(None) == ()


@pytest.mark.parametrize(
    "value, match",
    [
        ({"x": 1}, "不是数组"),
        (["not-an-object"], "项不是对象"),
        ([{"messagesAdded": "nope"}], "messagesAdded 不是数组"),
        ([{"messagesAdded": ["nope"]}], "messageAdded 项无效"),
        ([{"messagesAdded": [{"message": {}}]}], "缺少 message id"),
    ],
)
def test_malformed_history_is_rejected(value: object, match: str) -> None:
    """**结构不符即报错**，不尽力而为地跳过。"""
    with pytest.raises(GmailSyncError, match=match):
        parse_added_message_ids(value)


def test_duplicate_added_ids_are_preserved() -> None:
    """重复的新增记录原样保留——去重是调用方的事（同一封邮件只处理一次）。"""
    assert parse_added_message_ids([
        {"messagesAdded": [{"message": {"id": "m1"}}]},
        {"messagesAdded": [{"message": {"id": "m1"}}]},
    ]) == ("m1", "m1")


# --------------------------------------------------------------------------
# 元数据过滤
# --------------------------------------------------------------------------


def test_matching_metadata_is_accepted() -> None:
    """来自 Schwab 且主题为 eConfirms 的元数据通过。"""
    assert is_schwab_econfirm_metadata(_metadata()) is True


def test_header_names_are_case_insensitive() -> None:
    """**HTTP 头名大小写不敏感。**"""
    metadata = {"payload": {"headers": [
        {"name": "FROM", "value": SCHWAB_FROM},
        {"name": "subject", "value": "Schwab eConfirms"},
    ]}}

    assert is_schwab_econfirm_metadata(metadata) is True


def test_lookalike_sender_is_rejected() -> None:
    """伪装发件人不匹配。"""
    assert is_schwab_econfirm_metadata(
        _metadata(from_value="donotreply@mail.schwab.com.attacker.net")
    ) is False


def test_wrong_subject_is_rejected() -> None:
    """主题不符的邮件被过滤掉。"""
    assert is_schwab_econfirm_metadata(_metadata(subject="Account statement")) is False


def test_metadata_without_headers_is_rejected() -> None:
    """缺少 ``payload.headers`` 必须报错。"""
    with pytest.raises(GmailSyncError, match="payload.headers"):
        is_schwab_econfirm_metadata({"payload": {}})

    with pytest.raises(GmailSyncError, match="payload.headers"):
        is_schwab_econfirm_metadata({})


def test_malformed_header_is_rejected() -> None:
    """头部项结构不符必须报错。"""
    with pytest.raises(GmailSyncError, match="header 无效"):
        is_schwab_econfirm_metadata({"payload": {"headers": [{"name": "From"}]}})


def test_duplicate_headers_take_the_last_value() -> None:
    """重复头取**最后一个**——与参考实现一致。"""
    metadata = {"payload": {"headers": [
        {"name": "From", "value": "someone@example.com"},
        {"name": "From", "value": SCHWAB_FROM},
        {"name": "Subject", "value": "Schwab eConfirms"},
    ]}}

    assert is_schwab_econfirm_metadata(metadata) is True


# --------------------------------------------------------------------------
# 分页
# --------------------------------------------------------------------------


class _Plan:
    """有界查询计划桩。"""

    def __init__(self, maximum: int = 3, query: str = "from:x") -> None:
        self.query = query
        self.maximum_messages = maximum


def _pager(pages: list[dict]):
    """构造一个按序返回页面的传输层桩。"""
    def get_json(url, headers, timeout):
        get_json.urls.append(url)
        return pages[len(get_json.urls) - 1]
    get_json.urls = []
    return get_json


def test_collects_ids_across_pages() -> None:
    """跨页收集消息 ID。"""
    pager = _pager([
        {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "t1"},
        {"messages": [{"id": "m3"}]},
    ])

    result = fetch_bounded_gmail_message_ids(
        "ya29.x", _Plan(maximum=10), get_json=pager
    )

    assert result.message_ids == ("m1", "m2", "m3")
    assert result.truncated is False
    assert "pageToken" not in pager.urls[0]
    assert "pageToken=t1" in pager.urls[1]


def test_page_size_is_capped_and_shrinks_to_the_remaining_budget() -> None:
    """**单页上限 500，且不超过剩余额度。**

    不收缩会在只剩 1 条额度时还要拉 500 条，把无关邮件也读进来。
    """
    pager = _pager([{"messages": []}])
    fetch_bounded_gmail_message_ids("t", _Plan(maximum=1000), get_json=pager)
    assert "maxResults=500" in pager.urls[0]

    pager = _pager([{"messages": []}])
    fetch_bounded_gmail_message_ids("t", _Plan(maximum=3), get_json=pager)
    assert "maxResults=3" in pager.urls[0]


def test_reaching_the_limit_with_more_pages_is_truncated() -> None:
    """**达到上限且仍有下一页 → 截断。**

    截断会被同步层转成"需要重扫"，而不是静默少导入几封邮件。
    """
    pager = _pager([
        {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "t1"},
    ])

    result = fetch_bounded_gmail_message_ids("t", _Plan(maximum=2), get_json=pager)

    assert result.message_ids == ("m1", "m2")
    assert result.truncated is True


def test_reaching_the_limit_on_the_last_page_is_not_truncated() -> None:
    """恰好凑满且没有下一页 → 不算截断。"""
    pager = _pager([{"messages": [{"id": "m1"}, {"id": "m2"}]}])

    result = fetch_bounded_gmail_message_ids("t", _Plan(maximum=2), get_json=pager)

    assert result.truncated is False


def test_a_page_exceeding_the_remaining_budget_is_truncated() -> None:
    """**服务端无视 maxResults 时也算截断。**

    否则我们会以为自己只拿到了刚好够用的那批，而实际上还有更多没读。
    """
    pager = _pager([{"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}])

    result = fetch_bounded_gmail_message_ids("t", _Plan(maximum=2), get_json=pager)

    assert result.message_ids == ("m1", "m2")
    assert result.truncated is True


def test_duplicate_message_id_across_pages_is_rejected() -> None:
    """**同一 message id 出现在多页里 → 报错。**

    重复会让同一封邮件被处理两次，而导入层的幂等检查在更后面才起作用。
    """
    pager = _pager([
        {"messages": [{"id": "m1"}], "nextPageToken": "t1"},
        {"messages": [{"id": "m1"}]},
    ])

    with pytest.raises(GmailSyncError, match="重复 message id"):
        fetch_bounded_gmail_message_ids("t", _Plan(maximum=10), get_json=pager)


def test_duplicate_page_token_is_rejected() -> None:
    """**重复的 ``nextPageToken`` → 报错（死循环防线）。**

    一个总是返回同一个 token 的响应会让循环永远转下去，不断打 API。
    """
    pager = _pager([
        {"messages": [{"id": "m1"}], "nextPageToken": "same"},
        {"messages": [{"id": "m2"}], "nextPageToken": "same"},
    ])

    with pytest.raises(GmailSyncError, match="重复 page token"):
        fetch_bounded_gmail_message_ids("t", _Plan(maximum=100), get_json=pager)


def test_empty_page_token_is_rejected() -> None:
    """存在但为空的 page token 是结构错误。"""
    pager = _pager([{"messages": [{"id": "m1"}], "nextPageToken": "  "}])

    with pytest.raises(GmailSyncError, match="nextPageToken 无效"):
        fetch_bounded_gmail_message_ids("t", _Plan(maximum=10), get_json=pager)


def test_the_access_token_is_sent_as_a_bearer_header() -> None:
    """令牌经 Authorization 头发送，不进 URL。"""
    captured: dict = {}

    def get_json(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return {"messages": []}

    fetch_bounded_gmail_message_ids("ya29.secret", _Plan(), get_json=get_json)

    assert captured["headers"]["authorization"] == "Bearer ya29.secret"
    assert "secret" not in captured["url"]


@pytest.mark.parametrize("maximum", [0, -1, 1.5, True])
def test_invalid_limits_are_rejected(maximum: object) -> None:
    """上限必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        fetch_bounded_gmail_message_ids("t", _Plan(maximum=maximum), get_json=_pager([]))  # type: ignore[arg-type]


def test_blank_access_token_is_rejected() -> None:
    """空令牌拒绝。"""
    with pytest.raises(ValueError, match="access token"):
        fetch_bounded_gmail_message_ids("  ", _Plan(), get_json=_pager([]))


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def _profile(**overrides) -> dict:
    base = {
        "emailAddress": "Owner@Example.com", "historyId": "12345",
        "messagesTotal": 42, "threadsTotal": 7,
    }
    base.update(overrides)
    return base


def test_parses_the_profile() -> None:
    """解析档案并小写化邮箱。"""
    profile = parse_gmail_profile(_profile(), expected_email="owner@example.com")

    assert profile.email_address == "owner@example.com"
    assert profile.history_id == "12345"
    assert profile.messages_total == 42
    assert profile.threads_total == 7


def test_profile_email_must_match_the_connected_account() -> None:
    """**profile 的邮箱必须与已连接的账户一致。**

    不核对就可能用一个账户的令牌去读另一个账户的邮箱——而返回的数据看起来完全正常。
    """
    with pytest.raises(GmailSyncError, match="不一致"):
        parse_gmail_profile(_profile(emailAddress="other@example.com"),
                            expected_email="owner@example.com")


def test_profile_email_comparison_is_case_insensitive() -> None:
    """邮箱比较不区分大小写。"""
    profile = parse_gmail_profile(_profile(emailAddress="OWNER@EXAMPLE.COM"),
                                  expected_email="owner@example.com")
    assert profile.email_address == "owner@example.com"


@pytest.mark.parametrize("history_id", [None, "", "abc", "0", "007", 5])
def test_invalid_profile_history_ids_are_rejected(history_id: object) -> None:
    """**history id 必须是正十进制字符串。**"""
    with pytest.raises(GmailSyncError, match="history id 无效"):
        parse_gmail_profile(
            _profile(historyId=history_id), expected_email="owner@example.com"
        )


@pytest.mark.parametrize("field", ["messagesTotal", "threadsTotal"])
@pytest.mark.parametrize("value", [-1, 1.5, "42", None, True])
def test_invalid_counts_are_rejected(field: str, value: object) -> None:
    """计数必须是非负整数。"""
    with pytest.raises(GmailSyncError):
        parse_gmail_profile(
            _profile(**{field: value}), expected_email="owner@example.com"
        )


def test_blank_expected_email_is_rejected() -> None:
    """空的期望邮箱是调用方错误，必须报错而不是放行比较。"""
    with pytest.raises(GmailSyncError, match="已验证 Google 邮箱不能为空"):
        parse_gmail_profile(_profile(), expected_email="   ")


@pytest.mark.parametrize("email", ["", "not-an-email", "a@", 5, None])
def test_invalid_profile_emails_are_rejected(email: object) -> None:
    """profile 邮箱格式非法必须拒绝。"""
    with pytest.raises(GmailSyncError, match="profile 邮箱"):
        parse_gmail_profile(
            _profile(emailAddress=email), expected_email="owner@example.com"
        )


# --------------------------------------------------------------------------
# 增量历史拉取
# --------------------------------------------------------------------------


def _history_page(
    history_id: str, ids: list[str], *, next_token: str | None = None
) -> dict:
    """构造一页 history 响应。"""
    page: dict = {
        "historyId": history_id,
        "history": [{"messagesAdded": [{"message": {"id": i}}]} for i in ids],
    }
    if next_token is not None:
        page["nextPageToken"] = next_token
    return page


def _fetch_incremental(pages: list[dict], *, maximum: int = 10, start: str = "99"):
    """跑一次增量拉取。"""
    pager = _pager(pages)
    result = fetch_incremental_gmail_message_ids(
        "ya29.x", start_history_id=start, maximum_messages=maximum, get_json=pager
    )
    return result, pager


def test_incremental_filters_server_side_to_added_messages() -> None:
    """**服务端过滤 ``historyTypes=messageAdded``。**

    ``history.list`` 默认会返回标签变更等与导入无关的记录；不过滤会让无关变更也走一遍
    拉取流程。
    """
    _, pager = _fetch_incremental([_history_page("100", [])])

    assert "historyTypes=messageAdded" in pager.urls[0]
    assert "startHistoryId=99" in pager.urls[0]


def test_incremental_collects_ids_across_pages() -> None:
    """跨页收集。"""
    result, pager = _fetch_incremental([
        _history_page("100", ["m1"], next_token="t1"),
        _history_page("105", ["m2"]),
    ])

    assert result.message_ids == ("m1", "m2")
    assert "pageToken=t1" in pager.urls[1]


def test_observed_history_id_is_the_last_page() -> None:
    """**游标以最后一页为准**：它标记"本次读到了哪个时点"。"""
    result, _ = _fetch_incremental([
        _history_page("100", ["m1"], next_token="t1"),
        _history_page("105", ["m2"]),
    ])

    assert result.observed_history_id == "105"


def test_duplicate_ids_are_skipped_not_rejected() -> None:
    """**重复 id 跳过而不是报错。**

    ``history.list`` 会为同一封邮件返回多条记录（例如它同时被加了标签），因此重复是
    **正常的**。有界扫描走 ``messages.list``，那里的重复意味着响应损坏——两处行为
    分别匹配各自接口的语义。
    """
    result, _ = _fetch_incremental([
        _history_page("100", ["m1", "m1"], next_token="t1"),
        _history_page("105", ["m1", "m2"]),
    ])

    assert result.message_ids == ("m1", "m2")


def test_incremental_truncates_when_the_limit_is_reached() -> None:
    """达到上限且仍有剩余 → 截断。"""
    result, _ = _fetch_incremental(
        [_history_page("100", ["m1", "m2"], next_token="t1")], maximum=2
    )

    assert result.message_ids == ("m1", "m2")
    assert result.truncated is True


def test_incremental_truncates_when_a_page_exceeds_the_budget() -> None:
    """一页本身就超出剩余额度 → 截断。"""
    result, _ = _fetch_incremental([_history_page("100", ["m1", "m2", "m3"])], maximum=2)

    assert result.message_ids == ("m1", "m2")
    assert result.truncated is True


def test_incremental_is_not_truncated_when_exactly_drained() -> None:
    """恰好用完且没有下一页 → 不算截断。"""
    result, _ = _fetch_incremental([_history_page("100", ["m1", "m2"])], maximum=2)

    assert result.truncated is False


def test_incremental_duplicate_page_token_is_rejected() -> None:
    """**重复 page token 同样拒绝（死循环防线）。**"""
    with pytest.raises(GmailSyncError, match="重复 page token"):
        _fetch_incremental([
            _history_page("100", ["m1"], next_token="same"),
            _history_page("101", ["m2"], next_token="same"),
        ], maximum=100)


@pytest.mark.parametrize("history_id", [None, "", "abc", "0"])
def test_incremental_requires_a_valid_observed_history_id(history_id: object) -> None:
    """每页的 ``historyId`` 必须合法。"""
    with pytest.raises(GmailSyncError):
        _fetch_incremental([{"historyId": history_id, "history": []}])


@pytest.mark.parametrize("start", ["0", "007", "abc", ""])
def test_incremental_rejects_invalid_start_cursors(start: str) -> None:
    """起始游标必须是正十进制字符串。"""
    with pytest.raises(ValueError):
        _fetch_incremental([_history_page("100", [])], start=start)


@pytest.mark.parametrize("maximum", [0, -1, 1.5, True])
def test_incremental_rejects_invalid_limits(maximum: object) -> None:
    """上限必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        _fetch_incremental([_history_page("100", [])], maximum=maximum)  # type: ignore[arg-type]


def test_incremental_sends_the_token_in_the_header() -> None:
    """令牌只经请求头，不进 URL。"""
    captured: list = []

    def get_json(url, headers, timeout):
        captured.append((url, headers))
        return _history_page("100", [])

    fetch_incremental_gmail_message_ids(
        "ya29.secret", start_history_id="99", maximum_messages=5, get_json=get_json
    )

    url, headers = captured[0]
    assert headers["authorization"] == "Bearer ya29.secret"
    assert "secret" not in url


# --------------------------------------------------------------------------
# HTTP 错误映射
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_map_to_revoked(status: int) -> None:
    """**401 与 403 都表示授权已失效。**

    Gmail 用 403 表达"令牌有效但缺少该资源的权限"，与 401 一样需要用户重新授权；
    当成暂时性故障会无限重试。
    """
    error = gmail_http_error(status)

    assert error.code == "GMAIL_AUTH_REVOKED"
    assert error.retryable is False


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_gmail_failures_are_retryable(status: int) -> None:
    """429 与 5xx 可重试。"""
    error = gmail_http_error(status)

    assert error.code == "GMAIL_CONNECTION_ERROR"
    assert error.retryable is True


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_other_client_errors_are_not_retryable(status: int) -> None:
    """其它 4xx 重试没有意义。"""
    assert gmail_http_error(status).retryable is False


def _json_getter(status: int, payload=None):
    """构造返回固定状态的传输层。"""
    def get_json(url, headers, timeout):
        get_json.url = url
        return status, payload
    return get_json


def test_request_returns_the_payload_on_success() -> None:
    """2xx 返回负载。"""
    result = request_gmail_json(
        "https://gmail.example/x", "t", get_json=_json_getter(200, {"ok": True})
    )
    assert result == {"ok": True}


def test_history_404_means_the_cursor_expired() -> None:
    """**``history.list`` 的 404 表示游标已过期。**

    Google 只保留大约一周的历史；当成一般连接错误会让客户端拿着一个**永久失效的
    游标**反复重试，而正确的动作是让用户确认重扫范围。
    """
    with pytest.raises(GmailSyncError) as raised:
        request_gmail_json(
            "https://gmail.example/history", "t",
            get_json=_json_getter(404), history_404_means_expired=True,
        )

    assert raised.value.code == "GMAIL_HISTORY_EXPIRED"


def test_ordinary_404_is_not_a_cursor_expiry() -> None:
    """其它端点的 404 不是游标过期。"""
    with pytest.raises(GmailSyncError) as raised:
        request_gmail_json(
            "https://gmail.example/x", "t", get_json=_json_getter(404)
        )

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"


def test_transport_failure_is_retryable() -> None:
    """传输层异常映射为可重试的连接错误。"""
    def get_json(url, headers, timeout):
        raise TimeoutError("timed out")

    with pytest.raises(GmailSyncError) as raised:
        request_gmail_json("https://gmail.example/x", "t", get_json=get_json)

    assert raised.value.code == "GMAIL_CONNECTION_ERROR"
    assert raised.value.retryable is True


def test_non_object_root_is_rejected() -> None:
    """JSON 根节点不是对象必须报错。"""
    with pytest.raises(GmailSyncError, match="根节点不是对象"):
        request_gmail_json(
            "https://gmail.example/x", "t", get_json=_json_getter(200, ["a"])
        )


def test_request_sends_the_token_in_the_header() -> None:
    """令牌只经请求头。"""
    captured: dict = {}

    def get_json(url, headers, timeout):
        captured.update({"url": url, "headers": headers})
        return 200, {}

    request_gmail_json("https://gmail.example/x", "ya29.secret", get_json=get_json)

    assert captured["headers"]["authorization"] == "Bearer ya29.secret"
    assert "secret" not in captured["url"]


# --------------------------------------------------------------------------
# 附件水合
# --------------------------------------------------------------------------


def _text_part(**overrides) -> dict:
    base = {"mimeType": "text/plain", "body": {"size": 5000}}
    base.update(overrides)
    return base


def test_inline_data_is_left_alone() -> None:
    """已有内联数据时不发附件请求。"""
    calls: list = []

    def get_json(url, headers, timeout):
        calls.append(url)
        return 200, {"data": "other"}

    record = {"payload": _text_part(body={"data": "inline", "size": 6})}
    result = hydrate_gmail_text_attachments(
        record, message_id="m1", access_token="t", get_json=get_json
    )

    assert result["payload"]["body"]["data"] == "inline"
    assert calls == []


def test_attachment_is_fetched_when_data_is_absent() -> None:
    """**没有内联数据时去取附件。**

    Gmail 会把较大的正文作为附件返回；只读 ``body.data`` 会让那类邮件拿到空正文，
    解析随之报出"正文中没有交易块"——一个与真正原因无关的提示。
    """
    def get_json(url, headers, timeout):
        get_json.url = url
        return 200, {"data": "SGVsbG8"}

    result = hydrate_gmail_text_attachments(
        {"payload": _text_part(body={"attachmentId": "att-1", "size": 5000})},
        message_id="m1", access_token="t", get_json=get_json,
    )

    assert result["payload"]["body"]["data"] == "SGVsbG8"
    assert result["payload"]["body"]["attachmentId"] == "att-1"
    assert "/messages/m1/attachments/att-1" in get_json.url


def test_empty_string_data_counts_as_absent() -> None:
    """空字符串不算内联数据。"""
    def get_json(url, headers, timeout):
        return 200, {"data": "ZmlsbGVk"}

    result = hydrate_gmail_text_attachments(
        {"payload": _text_part(body={"data": "", "attachmentId": "att-1"})},
        message_id="m1", access_token="t", get_json=get_json,
    )

    assert result["payload"]["body"]["data"] == "ZmlsbGVk"


def test_non_text_parts_are_not_hydrated() -> None:
    """非文本部分不取附件——PDF 之类的内容不参与账本解析。"""
    calls: list = []

    def get_json(url, headers, timeout):
        calls.append(url)
        return 200, {"data": "x"}

    result = hydrate_gmail_text_attachments(
        {"payload": {"mimeType": "application/pdf", "body": {"attachmentId": "att-1"}}},
        message_id="m1", access_token="t", get_json=get_json,
    )

    assert calls == []
    assert "data" not in result["payload"]["body"]


def test_nested_parts_are_hydrated_recursively() -> None:
    """MIME 嵌套结构被递归水合。"""
    def get_json(url, headers, timeout):
        return 200, {"data": "TkVTVEVE"}

    record = {"payload": {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                _text_part(body={"attachmentId": "att-2"}),
            ]},
            {"mimeType": "application/pdf", "body": {"attachmentId": "att-9"}},
        ],
    }}

    result = hydrate_gmail_text_attachments(
        record, message_id="m1", access_token="t", get_json=get_json
    )

    nested = result["payload"]["parts"][0]["parts"][0]
    assert nested["body"]["data"] == "TkVTVEVE"
    # PDF 部分未被水合。
    assert "data" not in result["payload"]["parts"][1]["body"]


def test_record_without_a_payload_is_returned_unchanged() -> None:
    """没有 payload 的记录原样返回。"""
    record = {"id": "m1"}
    assert hydrate_gmail_text_attachments(
        record, message_id="m1", access_token="t", get_json=_json_getter(200, {})
    ) == record


def test_non_array_parts_are_rejected() -> None:
    """``parts`` 不是数组必须报错。"""
    with pytest.raises(GmailSyncError, match="parts 必须是数组"):
        hydrate_gmail_text_attachments(
            {"payload": {"mimeType": "multipart/mixed", "parts": "nope"}},
            message_id="m1", access_token="t", get_json=_json_getter(200, {}),
        )


def test_non_object_child_part_is_rejected() -> None:
    """子部分不是对象必须报错。"""
    with pytest.raises(GmailSyncError, match="part 必须是对象"):
        hydrate_gmail_text_attachments(
            {"payload": {"mimeType": "multipart/mixed", "parts": ["nope"]}},
            message_id="m1", access_token="t", get_json=_json_getter(200, {}),
        )


# --------------------------------------------------------------------------
# 附件校验器
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["att-1", "A" * 2048, "a_b-c"])
def test_valid_attachment_ids(value: str) -> None:
    """合法附件 ID 通过。"""
    assert require_attachment_id(value) == value


@pytest.mark.parametrize("value", ["", "att/1", "../x", "a b", "a" * 2049, 5, None])
def test_invalid_attachment_ids_are_rejected(value: object) -> None:
    """**附件 ID 是路径注入防线**：它会被拼进 API 路径。"""
    with pytest.raises(GmailSyncError, match="attachment id 无效"):
        require_attachment_id(value)


@pytest.mark.parametrize("value", ["SGVsbG8", "YWJjZA", "AAAA", "ab"])
def test_valid_base64url_data(value: str) -> None:
    """合法 base64url 通过（长度 ≠ 1 mod 4）。"""
    assert require_base64url_data(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "", "not base64!", "SGVs bG8",
        "a",          # 长度 1 ≡ 1 (mod 4)：**不可能存在**
        "abcde",      # 长度 5 ≡ 1 (mod 4)
        5, None,
    ],
)
def test_invalid_base64url_data_is_rejected(value: object) -> None:
    """**长度 ≡ 1 (mod 4) 的 base64 不可能存在**，直接拒绝而不是让下游去猜。"""
    with pytest.raises(GmailSyncError, match="attachment data 无效"):
        require_base64url_data(value)


# --------------------------------------------------------------------------
# 完整记录拉取
# --------------------------------------------------------------------------


def _full_getter(records: dict[str, dict], *, metadata: dict[str, list] | None = None):
    """构造按消息 ID 分发的传输层。"""
    def get_json(url, headers, timeout):
        get_json.urls.append(url)
        body = url.split("/messages/")[1]
        message_id = body.split("?")[0].split("/")[0]
        if "format=metadata" in url:
            headers_payload = (metadata or {}).get(message_id, [
                {"name": "From", "value": SCHWAB_FROM},
                {"name": "Subject", "value": "Schwab eConfirms"},
            ])
            return 200, {"id": message_id, "payload": {"headers": headers_payload}}
        return 200, records[message_id]
    get_json.urls = []
    return get_json


def _full_record(message_id: str, data: str = "aGk=") -> dict:
    """构造一条完整消息记录。"""
    return {
        "id": message_id,
        "payload": {"mimeType": "text/plain", "body": {"data": data}},
    }


def test_fetches_full_records_in_order() -> None:
    """按入参顺序拉取完整记录。"""
    getter = _full_getter({"m1": _full_record("m1"), "m2": _full_record("m2")})

    records = fetch_full_gmail_records(("m1", "m2"), "t", get_json=getter)

    assert [record["id"] for record in records] == ["m1", "m2"]
    assert "format=full" in getter.urls[0]


def test_full_record_id_must_match_the_request() -> None:
    """**回显 id 必须与请求一致。**

    不核对就可能把另一封邮件的内容当成这一封来解析——而解析结果看起来完全正常
    （符号、数量、价格都在），**只是属于另一封邮件**。
    """
    getter = _full_getter({"m1": _full_record("other")})

    with pytest.raises(GmailSyncError, match="响应 id 与请求不一致"):
        fetch_full_gmail_records(("m1",), "t", get_json=getter)


def test_metadata_filters_before_fetching_bodies() -> None:
    """**先按元数据过滤，再拉正文。**

    元数据便宜、正文昂贵；增量结果里常混有大量无关邮件。
    """
    getter = _full_getter(
        {"m1": _full_record("m1")},
        metadata={
            "m1": [{"name": "From", "value": SCHWAB_FROM},
                   {"name": "Subject", "value": "Schwab eConfirms"}],
            "m2": [{"name": "From", "value": "friend@example.com"},
                   {"name": "Subject", "value": "Re: hello"}],
            "m3": [{"name": "From", "value": SCHWAB_FROM},
                   {"name": "Subject", "value": "Account statement"}],
        },
    )

    result = fetch_incremental_gmail_full_records(("m1", "m2", "m3"), "t", get_json=getter)

    assert [record["id"] for record in result.records] == ["m1"]
    # 只有 m1 走了 format=full。
    assert sum("format=full" in url for url in getter.urls) == 1


def test_ignored_count_distinguishes_no_mail_from_irrelevant_mail() -> None:
    """**``ignored_message_count`` 让调用方区分两种"没有结果"。**

    "这段时间没有邮件"与"有邮件但都不相关"的处置完全不同。
    """
    getter = _full_getter({}, metadata={
        "m2": [{"name": "From", "value": "friend@example.com"},
               {"name": "Subject", "value": "Re: hello"}],
    })

    result = fetch_incremental_gmail_full_records(("m2",), "t", get_json=getter)

    assert result.records == ()
    assert result.ignored_message_count == 1


def test_zero_candidates_is_not_an_error() -> None:
    """零条候选是合法结果。"""
    result = fetch_incremental_gmail_full_records((), "t", get_json=_full_getter({}))

    assert result.records == ()
    assert result.ignored_message_count == 0


def test_metadata_id_must_match_the_request() -> None:
    """元数据响应的回显 id 同样必须核对。"""
    def get_json(url, headers, timeout):
        return 200, {"id": "other", "payload": {"headers": []}}

    with pytest.raises(GmailSyncError, match="metadata 响应 id 与请求不一致"):
        fetch_incremental_gmail_full_records(("m1",), "t", get_json=get_json)


def test_irrelevant_messages_never_trigger_a_body_request() -> None:
    """**无关邮件完全不会触发正文请求。**"""
    getter = _full_getter({}, metadata={
        "m2": [{"name": "From", "value": "friend@example.com"},
               {"name": "Subject", "value": "hi"}],
    })

    fetch_incremental_gmail_full_records(("m2",), "t", get_json=getter)

    assert not any("format=full" in url for url in getter.urls)
