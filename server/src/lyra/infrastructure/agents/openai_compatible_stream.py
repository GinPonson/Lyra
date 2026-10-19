"""OpenAI 兼容端点的流式绑定。

**这是一处经核实后的偏离，必须写清楚**：

- TypeScript 侧用 ``@genkit-ai/compat-oai``（OpenAI 兼容 provider）；
- Python 的 ``genkit`` 核心包存在且已安装，但**没有 OpenAI 兼容 provider 插件**
  ——已核实 ``genkit-plugin-compat-oai`` / ``genkit-plugin-openai`` /
  ``genkit-plugin-ollama`` 在 PyPI 上均为 404，Python 侧只有
  ``genkit-plugin-google-genai`` 与 ``genkit-plugin-firebase``。

因此本模块**直接调用同一个 OpenAI 兼容端点**，做的事情与 ``compat-oai`` 相同。

这是一个**有边界的偏离**：``GenkitTurnExecutor`` 已经就绪，把 ``stream_provider``
换成基于 Genkit 插件的实现是**改一个文件**的事。等 Python 侧出现对应插件，或改用
``google-genai`` provider 时，其余代码不受影响。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ...domain.agent.switching import NormalizedAgentMessage
from .genkit_runtime import (
    ModelChunk,
    ModelStream,
    ModelToolExchange,
    ModelToolRequest,
    ModelUsage,
    group_exchanges_by_turn,
)

# 一行的最大字节数：防止上游返回超长行把内存吃光。
MAX_LINE_BYTES = 256 * 1024


class OpenAiCompatibleStream:
    """把 SSE 流解析为规范化分片。

    **惰性执行**：``chunks()`` 被迭代时才发起请求并解析；``tool_requests`` /
    ``usage`` / ``final_text`` 返回累积结果，因此必须在 ``chunks()`` 之后调用。
    这一顺序由 ``run_agent_turn_stream`` 保证。

    @param lines: SSE 行迭代器（已按行切分）。
    """

    def __init__(self, lines: Callable[[], Iterable[str]]) -> None:
        self._lines = lines
        self._text_parts: list[str] = []
        self._tool_requests: list[ModelToolRequest] = []
        self._usage: ModelUsage | None = None
        self._tool_buffers: dict[int, dict[str, str]] = {}

    def chunks(self) -> Iterable[ModelChunk]:
        """解析 SSE 并按序产出分片。

        @returns 规范化分片序列。
        @raises ValueError: 行超长或负载不是合法 JSON。
        """
        for line in self._lines():
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("上游返回的行超过上限")
            if not line.startswith("data:"):
                # 空行与注释（`:` 开头的心跳）都不是负载。
                continue
            payload = line[len("data:") :].strip()
            if payload == "" or payload == "[DONE]":
                if payload == "[DONE]":
                    break
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as error:
                raise ValueError("上游返回了非 JSON 负载") from error
            yield from self._consume(event)

    def _consume(self, event: Any) -> Iterable[ModelChunk]:
        """消费一条 SSE 事件。

        推理内容**从不返回**——只标记"有推理活动"。这与领域层的约定一致：
        推理原文不得进入传输层。
        """
        if not isinstance(event, dict):
            return
        usage = event.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            self._usage = ModelUsage(total_tokens=usage["total_tokens"])

        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        if not isinstance(delta, dict):
            return

        # 推理活动：只在真正有内容时标记，避免空推理刷出事件。
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning != "":
            yield ModelChunk(reasoning=True)

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            self._accumulate_tool_calls(tool_calls)

        text = delta.get("content")
        if isinstance(text, str) and text != "":
            self._text_parts.append(text)
            yield ModelChunk(text=text)

    def _accumulate_tool_calls(self, tool_calls: list[Any]) -> None:
        """累积工具调用分片。

        OpenAI 兼容端点会把一次调用拆到多个分片里（先给 id 与 name，再逐段给
        arguments），因此必须按 index 累积，不能把每个分片当成独立调用。
        """
        for entry in tool_calls:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            if not isinstance(index, int):
                continue
            buffer = self._tool_buffers.setdefault(index, {"name": "", "arguments": ""})
            function = entry.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    buffer["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    buffer["arguments"] += function["arguments"]

    def tool_requests(self) -> tuple[ModelToolRequest, ...]:
        """返回累积完成的工具调用。

        参数不是合法 JSON 时**退回空对象**：调用会被记录为一次拒绝审计，
        而不是让整个回合并失败。
        """
        requests: list[ModelToolRequest] = []
        for index in sorted(self._tool_buffers):
            buffer = self._tool_buffers[index]
            if buffer["name"] == "":
                continue
            try:
                arguments = json.loads(buffer["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            requests.append(ModelToolRequest(name=buffer["name"], arguments=arguments))
        return tuple(requests)

    def usage(self) -> ModelUsage | None:
        """返回上游上报的用量；未上报时为 ``None``。"""
        return self._usage

    def final_text(self) -> str:
        """返回聚合后的完整可见文本。"""
        return "".join(self._text_parts)


class OpenAiCompatibleStreamProvider:
    """按 OpenAI 兼容协议发起一次流式对话。

    @param config: 已校验的模型配置。
    @param open_lines: 注入的传输层；接收 (url, headers, body) 返回 SSE 行迭代器。
    """

    def __init__(self, config: Any, open_lines: Callable[..., Iterable[str]]) -> None:
        self._config = config
        self._open_lines = open_lines

    def __call__(
        self,
        *,
        content: str,
        system_context: str,
        tool_definitions: tuple[Any, ...],
        history: tuple[NormalizedAgentMessage, ...] = (),
        exchanges: tuple[ModelToolExchange, ...] = (),
    ) -> ModelStream:
        """构造一次流式调用。

        @param content: 用户输入。
        @param system_context: 系统提示词。
        @param tool_definitions: 可调用工具。
        @param history: 当前用户消息之前的历史 user/assistant 消息。
        @param exchanges: 前几轮已执行的工具调用与结果；为空即首轮。
        @returns 惰性解析的 ``ModelStream``。
        """
        body = {
            "model": self._config.model,
            "stream": True,
            # 用量必须显式索取：多数兼容端点默认不返回。
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "system", "content": system_context},
                *[
                    {"role": message.role, "content": message.content}
                    for message in history
                ],
                {"role": "user", "content": content},
                *_tool_exchange_messages(exchanges),
            ],
            "tools": [_tool_payload(definition) for definition in tool_definitions],
        }
        # 刻意不设 max_tokens 与重试：上限由上游配置决定；重试会让一次已经产生
        # 费用与副作用的对话被重复执行。
        return OpenAiCompatibleStream(lambda: self._open_lines(
            f"{self._config.base_url}/chat/completions",
            {
                "authorization": f"Bearer {self._config.api_key}",
                "content-type": "application/json",
                "accept": "text/event-stream",
            },
            body,
            self._config.turn_timeout_ms / 1000,
        ))


def _tool_payload(definition: Any) -> dict[str, Any]:
    """把工具定义转成 OpenAI 兼容的 tools 条目。"""
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.input_schema,
        },
    }


def _tool_exchange_messages(
    exchanges: tuple[ModelToolExchange, ...],
) -> list[dict[str, Any]]:
    """把已执行的工具调用与结果翻译成 OpenAI 兼容的多轮消息。

    同一次模型调用请求的多个工具**合并成一条 assistant 消息**，再逐条给出
    ``role: tool`` 结果；拆成多条 assistant 消息会让端点因为 ``tool_calls`` 与
    tool 结果不配对而拒绝整个请求。

    @param exchanges: 按执行顺序排列的回灌记录。
    @returns 追加到消息列表尾部的消息。
    """
    messages: list[dict[str, Any]] = []
    for round_exchanges in group_exchanges_by_turn(exchanges):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": exchange.call_id,
                    "type": "function",
                    "function": {
                        "name": exchange.tool_name,
                        "arguments": json.dumps(exchange.arguments, ensure_ascii=False),
                    },
                }
                for exchange in round_exchanges
            ],
        })
        for exchange in round_exchanges:
            messages.append({
                "role": "tool",
                "tool_call_id": exchange.call_id,
                # 结果原样回灌：截断或重写会让模型基于被改动过的数据作答。
                "content": json.dumps(exchange.result, ensure_ascii=False, default=str),
            })
    return messages


def httpx_open_lines(
    url: str, headers: Mapping[str, str], body: dict[str, Any], timeout: float
) -> Iterable[str]:
    """用 httpx 发起流式请求并逐行产出 SSE。

    按行流式读取而不是先整体下载：**增量必须在生成过程中就到客户端**，否则
    "流式"只是把一次性响应拆成多次发送。

    @param url: 完整端点地址。
    @param headers: 请求头。
    @param body: 请求体。
    @param timeout: 读超时（秒）。
    @returns SSE 行。
    @raises RuntimeError: 上游返回非 2xx；错误正文只进异常，不返回给模型。
    """
    import httpx

    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, headers=dict(headers), json=body) as response:
            if not 200 <= response.status_code < 300:
                # 不把上游错误正文回传给模型：它常含端点、密钥或内部堆栈。
                raise RuntimeError(f"上游返回 {response.status_code}")
            for line in response.iter_lines():
                yield line
