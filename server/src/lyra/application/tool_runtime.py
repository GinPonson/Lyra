"""Agent 工具调用的分发。

把「按名字查找工具 → 校验参数 → 执行」收敛到一处，供 Genkit 与 REST 两条入口复用。

三类失败各有归属，**不能合并成一个"工具调用失败"**：

| 情况 | 错误码 | 理由 |
| --- | --- | --- |
| 工具名不存在 | ``TOOL_NOT_FOUND`` | 模型编了个不存在的工具名，提示词需要修正 |
| 参数不符合 schema | ``TOOL_ARGUMENTS_INVALID`` | 模型参数给错，可带着校验信息重试 |
| 处理器抛出预期错误 | 透传 | 例如账户无权限，调用方需要看到真实原因 |
| 处理器抛出意外异常 | ``TOOL_EXECUTION_FAILED`` | **不得把内部异常文本透给模型** |

最后一条是安全性质：内部异常常含 SQL、路径或连接串，把它们当作工具结果喂回模型，
等于把服务端内部细节写进了对话记录。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.agent.tool_call import AgentToolContext
from .app_error import ApplicationError
from .financial_tool_validation import is_valid_financial_tool_arguments


@dataclass(frozen=True)
class ToolDefinition:
    """一个可调用工具。

    @param name: 工具名。
    @param description: 给模型看的用途说明。
    @param input_schema: JSON Schema，同时用于公开说明与执行前校验。**只包含模型
        可控字段**：账户范围、账本版本与截止时间由 :class:`AgentToolContext` 注入。
    @param handler: 执行体；接收**原样**的调用参数与服务端注入的上下文。校验只回答
        "能不能调用"，不回答"用什么参数调用"——把净化后的摘要当成参数会把越界调用
        静默改写成一次更宽的查询。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[Mapping[str, Any], AgentToolContext], Any]


class ToolRuntime:
    """按名字分发工具调用。

    @param definitions: 工具定义；名字必须唯一。
    """

    def __init__(self, definitions: tuple[ToolDefinition, ...]) -> None:
        by_name: dict[str, ToolDefinition] = {}
        for definition in definitions:
            if definition.name in by_name:
                raise ValueError(f"工具名重复: {definition.name}")
            by_name[definition.name] = definition
        self._definitions = by_name

    @property
    def tool_names(self) -> tuple[str, ...]:
        """已注册的工具名，保持注册顺序。"""
        return tuple(self._definitions)

    def definition(self, name: str) -> ToolDefinition:
        """取出工具定义。

        @raises ApplicationError: 工具不存在。
        """
        definition = self._definitions.get(name)
        if definition is None:
            raise ApplicationError(
                code="TOOL_NOT_FOUND",
                message=f"未知工具: {name}",
                status_code=400,
                retryable=False,
            )
        return definition

    def invoke(
        self, name: str, arguments: Any, context: AgentToolContext
    ) -> Any:
        """校验并执行一个工具调用。

        @param name: 工具名。
        @param arguments: 调用方提交的参数；**原样**交给处理器。
        @param context: 服务端注入的账户范围、账本版本与截止时间。
        @returns 处理器的返回值（应是可安全公开的投影）。
        @raises ApplicationError: 工具不存在、参数非法，或处理器抛出预期错误。
        """
        definition = self.definition(name)
        if not is_valid_financial_tool_arguments(definition.input_schema, arguments):
            raise ApplicationError(
                code="TOOL_ARGUMENTS_INVALID",
                message=f"工具 {name} 的参数无效",
                status_code=400,
                retryable=False,
            )
        try:
            return definition.handler(arguments, context)
        except ApplicationError:
            # 预期错误透传：例如账户无权限，调用方需要看到真实原因。
            raise
        except Exception as error:
            raise ApplicationError(
                code="TOOL_EXECUTION_FAILED",
                message=f"工具 {name} 执行失败",
                status_code=500,
                retryable=False,
            ) from error

    def public_descriptions(self) -> list[dict[str, Any]]:
        """公开全部工具的说明与参数 schema，供模型与客户端发现能力。"""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "inputSchema": definition.input_schema,
            }
            for definition in self._definitions.values()
        ]
