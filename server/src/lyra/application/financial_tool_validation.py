"""财务工具的参数校验。

镜像 TypeScript 版 ``financial-tool-validation.ts``。

工具的 JSON Schema 同时承担两个职责：**对外公开的参数说明**与**服务端执行前的参数
边界**。两者用同一份 schema，因此不存在"文档说一套、校验做一套"。

一处关键取舍：**不做类型强转**（对应 Ajv 的 ``coerceTypes: false``）。

若能强转，客户端提交 ``"5"`` 就会被当成整数 5 通过校验，于是：
- 参数类型的事实来源变成服务端的宽松程度，而不是 schema；
- 任何依赖"这里一定是 int"的下游代码都会在真实数据上炸。

宁可返回"参数无效"，也不要替调用方猜他想要什么。
"""

from __future__ import annotations

from typing import Any

import jsonschema

# 校验器按 schema 缓存：同一工具会被反复调用，重复构造校验器是无谓开销。
_VALIDATORS: dict[int, jsonschema.protocols.Validator] = {}


def is_valid_financial_tool_arguments(
    input_schema: dict[str, Any],
    arguments: Any,
) -> bool:
    """按工具 schema 校验参数。

    只做校验、不做转换：校验通过时调用方拿到的仍是**原样**的参数。

    @param input_schema: 工具的 JSON Schema。
    @param arguments: 调用方提交的参数。
    @returns 是否符合 schema。
    """
    validator = _validator_for(input_schema)
    return validator.is_valid(arguments)


def validate_financial_tool_arguments(
    input_schema: dict[str, Any],
    arguments: Any,
) -> None:
    """按工具 schema 校验参数，失败时抛出。

    @param input_schema: 工具的 JSON Schema。
    @param arguments: 调用方提交的参数。
    @raises jsonschema.ValidationError: 参数不符合 schema。
    """
    _validator_for(input_schema).validate(arguments)


def _validator_for(input_schema: dict[str, Any]) -> jsonschema.protocols.Validator:
    """取（或构造）某个 schema 对应的校验器。

    用 ``id(schema)`` 作键是安全的：调用方持有 schema 的引用，生命周期覆盖调用期。
    """
    key = id(input_schema)
    validator = _VALIDATORS.get(key)
    if validator is None:
        # 不注册 format 检查器：与 Ajv 侧保持一致的宽松度，避免两处对同一 schema
        # 得出不同结论。
        validator = jsonschema.Draft202012Validator(input_schema)
        _VALIDATORS[key] = validator
    return validator
