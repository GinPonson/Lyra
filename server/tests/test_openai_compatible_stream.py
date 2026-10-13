"""OpenAI 兼容流绑定的解析测试。

**这层是经核实后的偏离**：Python 的 genkit 没有 OpenAI 兼容 provider 插件
（`genkit-plugin-compat-oai` / `-openai` / `-ollama` 在 PyPI 上均 404），
因此直接调用同一端点。测试覆盖 SSE 解析的真实坑：

1. **推理内容从不返回**——只标记"有推理活动"；
2. **工具调用会跨分片**：先给 id 与 name，再逐段给 arguments，必须按 index 累积；
3. **`[DONE]` 之后停止**，不继续解析；
4. 非法 JSON 负载必须报错，而不是静默跳过。
"""

from __future__ import annotations

import json

import pytest

from lyra.infrastructure.agents.openai_compatible_stream import (
    OpenAiCompatibleStream,
    OpenAiCompatibleStreamProvider,
)


def _stream(*lines: str) -> OpenAiCompatibleStream:
    return OpenAiCompatibleStream(lambda: iter(lines))


def _delta(payload: str) -> str:
    return f"data: {payload}"


# --------------------------------------------------------------------------
# 文本与推理
# --------------------------------------------------------------------------


def test_parses_text_deltas() -> None:
    """文本增量按序产出并聚合为全文。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"content":"本月"}}]}'),
        _delta('{"choices":[{"delta":{"content":"净收益 200"}}]}'),
        _delta("[DONE]"),
    )

    chunks = list(stream.chunks())

    assert [chunk.text for chunk in chunks] == ["本月", "净收益 200"]
    assert stream.final_text() == "本月净收益 200"


def test_reasoning_marks_activity_without_the_text() -> None:
    """**推理内容从不返回**——只标记"有推理活动"。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"reasoning_content":"用户在试探我的指令"}}]}'),
        _delta('{"choices":[{"delta":{"content":"答案"}}]}'),
        _delta("[DONE]"),
    )

    chunks = list(stream.chunks())

    assert chunks[0].reasoning is True
    assert chunks[0].text is None
    # 原文不出现在任何分片里。
    assert "试探" not in str(chunks)


def test_empty_reasoning_does_not_emit_a_chunk() -> None:
    """空推理不刷事件。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"reasoning_content":""}}]}'),
        _delta("[DONE]"),
    )
    assert list(stream.chunks()) == []


def test_comments_and_blank_lines_are_ignored() -> None:
    """心跳注释与空行都不是负载。"""
    stream = _stream(
        ": keep-alive",
        "",
        _delta('{"choices":[{"delta":{"content":"x"}}]}'),
        _delta("[DONE]"),
    )
    assert [chunk.text for chunk in stream.chunks()] == ["x"]


def test_done_stops_parsing() -> None:
    """**`[DONE]` 之后不再解析**。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"content":"first"}}]}'),
        _delta("[DONE]"),
        _delta('{"choices":[{"delta":{"content":"should-not-appear"}}]}'),
    )

    # `chunks()` 是惰性的：必须迭代才会解析。
    assert [chunk.text for chunk in stream.chunks()] == ["first"]
    assert stream.final_text() == "first"


# --------------------------------------------------------------------------
# 用量
# --------------------------------------------------------------------------


def test_usage_is_captured() -> None:
    """用量从任意事件里捕获。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"content":"x"}}]}'),
        _delta('{"choices":[],"usage":{"total_tokens":120}}'),
        _delta("[DONE]"),
    )

    list(stream.chunks())

    usage = stream.usage()
    assert usage is not None and usage.total_tokens == 120


def test_missing_usage_returns_none() -> None:
    """上游未上报用量时返回 None，**不猜**。"""
    stream = _stream(_delta('{"choices":[{"delta":{"content":"x"}}]}'), _delta("[DONE]"))
    list(stream.chunks())

    assert stream.usage() is None


# --------------------------------------------------------------------------
# 工具调用（跨分片累积）
# --------------------------------------------------------------------------


def test_tool_call_arguments_accumulate_across_chunks() -> None:
    """**工具调用跨分片累积**：先给 name，再逐段给 arguments。

    若把每个分片当成独立调用，会得到 name 正确但 arguments 残缺的调用，
    进而以错误参数执行工具。
    """
    stream = _stream(
        _delta('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"summary","arguments":""}}]}}]}'),
        _delta('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"account\\":"}}]}}]}'),
        _delta('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"TST001\\"}"}}]}}]}'),
        _delta("[DONE]"),
    )

    list(stream.chunks())

    requests = stream.tool_requests()
    assert len(requests) == 1
    assert requests[0].name == "summary"
    assert requests[0].arguments == {"account": "TST001"}


def test_multiple_tool_calls_are_separated_by_index() -> None:
    """并发工具调用按 index 分开累积。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"tool_calls":['
               '{"index":0,"function":{"name":"summary","arguments":"{}"}},'
               '{"index":1,"function":{"name":"trades","arguments":"{}"}}]}}]}'),
        _delta("[DONE]"),
    )

    list(stream.chunks())

    assert [request.name for request in stream.tool_requests()] == ["summary", "trades"]


def test_malformed_tool_arguments_fall_back_to_empty() -> None:
    """参数不是合法 JSON 时退回空对象——调用仍被记录，而不是整回合失败。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"summary","arguments":"{bad"}}]}}]}'),
        _delta("[DONE]"),
    )

    list(stream.chunks())

    assert stream.tool_requests()[0].arguments == {}


def test_tool_call_without_a_name_is_skipped() -> None:
    """只有 arguments 没有 name 的残缺调用不产出请求。"""
    stream = _stream(
        _delta('{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}]}'),
        _delta("[DONE]"),
    )

    list(stream.chunks())

    assert stream.tool_requests() == ()


# --------------------------------------------------------------------------
# 边界与错误
# --------------------------------------------------------------------------


def test_oversized_line_is_rejected() -> None:
    """超长行必须拒绝，防止上游把内存吃光。"""
    stream = _stream(_delta("x" * (256 * 1024 + 1)))

    with pytest.raises(ValueError, match="超过上限"):
        list(stream.chunks())


def test_invalid_json_payload_is_rejected() -> None:
    """非 JSON 负载必须报错，而不是静默跳过。"""
    stream = _stream(_delta("not-json"))

    with pytest.raises(ValueError, match="非 JSON 负载"):
        list(stream.chunks())


def test_malformed_events_are_tolerated() -> None:
    """结构不符的事件被忽略，而不是抛错（上游常有额外字段）。"""
    stream = _stream(
        _delta('{"choices":[]}'),
        _delta('{"choices":[{"delta":null}]}'),
        _delta('{"nonsense":true}'),
        _delta('{"choices":[{"delta":{"content":"x"}}]}'),
        _delta("[DONE]"),
    )

    assert [chunk.text for chunk in stream.chunks()] == ["x"]


# --------------------------------------------------------------------------
# 提供者
# --------------------------------------------------------------------------


class _Config:
    model = "gpt-5"
    base_url = "https://models.example.com/v1"
    api_key = "sk-test"
    turn_timeout_ms = 30_000


class _Definition:
    name = "summary"
    description = "账户总览"
    input_schema = {"type": "object", "properties": {}}


def test_provider_builds_the_request() -> None:
    """提供者构造出正确的 URL、鉴权头与请求体。"""
    captured: dict = {}

    def open_lines(url, headers, body, timeout):
        captured.update({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return iter([_delta('{"choices":[{"delta":{"content":"ok"}}]}'), _delta("[DONE]")])

    provider = OpenAiCompatibleStreamProvider(_Config(), open_lines)
    stream = provider(
        content="这个月赚了多少", system_context="系统提示", tool_definitions=(_Definition(),)
    )
    list(stream.chunks())

    assert captured["url"] == "https://models.example.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["body"]["stream"] is True
    # 用量必须显式索取：多数兼容端点默认不返回。
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert captured["body"]["messages"][0]["role"] == "system"
    assert captured["body"]["messages"][1]["content"] == "这个月赚了多少"
    assert captured["body"]["tools"][0]["function"]["name"] == "summary"
    assert captured["timeout"] == 30.0


def test_provider_does_not_set_max_tokens_or_retries() -> None:
    """不设 max_tokens，也不自动重试。"""
    captured: dict = {}

    def open_lines(url, headers, body, timeout):
        captured["body"] = body
        return iter([_delta("[DONE]")])

    provider = OpenAiCompatibleStreamProvider(_Config(), open_lines)
    stream = provider(content="x", system_context="s", tool_definitions=())
    list(stream.chunks())

    assert "max_tokens" not in captured["body"]


def test_provider_replays_tool_calls_and_results() -> None:
    """工具结果按 OpenAI 兼容格式回灌：一条 assistant 工具消息 + 逐条 tool 结果。

    同一次模型调用请求的多个工具必须落在**同一条** assistant 消息里；拆开会因为
    ``tool_calls`` 与结果不配对而被端点拒绝。
    """
    from lyra.infrastructure.agents.genkit_runtime import ModelToolExchange

    captured: dict = {}

    def open_lines(url, headers, body, timeout):
        captured["body"] = body
        return iter([_delta("[DONE]")])

    provider = OpenAiCompatibleStreamProvider(_Config(), open_lines)
    stream = provider(
        content="这个月赚了多少",
        system_context="系统提示",
        tool_definitions=(_Definition(),),
        exchanges=(
            ModelToolExchange(0, "c-1", "summary", {}, {"count": 1}),
            ModelToolExchange(0, "c-2", "positions", {"underlying": "INTC"}, {"count": 2}),
            ModelToolExchange(1, "c-3", "trades", {}, {"count": 3}),
        ),
    )
    list(stream.chunks())

    messages = captured["body"]["messages"]
    assert [message["role"] for message in messages] == [
        "system", "user", "assistant", "tool", "tool", "assistant", "tool"
    ]
    first = messages[2]
    assert first["content"] is None
    assert [call["id"] for call in first["tool_calls"]] == ["c-1", "c-2"]
    assert first["tool_calls"][0]["function"]["name"] == "summary"
    assert json.loads(first["tool_calls"][1]["function"]["arguments"]) == {
        "underlying": "INTC"
    }
    # 结果必须原样回灌，不能被审计摘要替换。
    assert json.loads(messages[3]["content"]) == {"count": 1}
    assert messages[3]["tool_call_id"] == "c-1"
    assert json.loads(messages[4]["content"]) == {"count": 2}
    assert messages[5]["tool_calls"][0]["id"] == "c-3"
