"""Agent 模型配置的读取与校验。

镜像 TypeScript 版 ``genkit-runtime.ts`` 的配置部分。

**三项缺一即视为未配置**，且未配置时执行器**必须抛错**而不是静默返回空回答——
静默失败会让用户以为模型"什么也没说"，而实际上是服务端没配好。

``LYRA_GENKIT_BASE_URL`` 的校验刻意严格：不接受凭据、查询串或片段。带凭据的 URL
会把密钥写进日志与错误信息；带查询串的 URL 常常是复制粘贴时多带了参数。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_MAX_TURNS = 10
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 600_000
MIN_TURNS = 1
MAX_TURNS = 32


@dataclass(frozen=True)
class GenkitConfig:
    """已校验的模型配置。"""

    base_url: str
    model: str
    api_key: str
    turn_timeout_ms: int
    max_turns: int


def load_genkit_config(environment: Mapping[str, str | None]) -> GenkitConfig | None:
    """从环境读取模型配置。

    @param environment: 环境变量映射。
    @returns 已校验配置；三项全空时返回 ``None``（模型未启用）。
    @raises ValueError: 部分配置或取值非法。
    """
    base_url = (environment.get("LYRA_GENKIT_BASE_URL") or "").strip()
    model = (environment.get("LYRA_GENKIT_MODEL") or "").strip()
    api_key = (environment.get("LYRA_GENKIT_API_KEY") or "").strip()

    if not base_url and not model and not api_key:
        return None
    missing = [
        name
        for name, value in (
            ("LYRA_GENKIT_BASE_URL", base_url),
            ("LYRA_GENKIT_MODEL", model),
            ("LYRA_GENKIT_API_KEY", api_key),
        )
        if value == ""
    ]
    if missing:
        # 部分配置必须报错：否则服务会以"看起来启用了但每次对话都失败"的状态启动。
        raise ValueError(f"Agent 模型配置缺少 {', '.join(missing)}")

    parts = urlsplit(base_url)
    if (
        parts.scheme not in ("http", "https")
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "LYRA_GENKIT_BASE_URL 必须是无凭据、查询参数和片段的 HTTP(S) 地址"
        )

    return GenkitConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=api_key,
        turn_timeout_ms=_bounded_int(
            environment.get("LYRA_AGENT_TURN_TIMEOUT_MS"),
            DEFAULT_TIMEOUT_MS, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS,
            "LYRA_AGENT_TURN_TIMEOUT_MS",
        ),
        max_turns=_bounded_int(
            environment.get("LYRA_AGENT_MAX_TURNS"),
            DEFAULT_MAX_TURNS, MIN_TURNS, MAX_TURNS, "LYRA_AGENT_MAX_TURNS",
        ),
    )


def _bounded_int(
    raw: str | None, default: int, low: int, high: int, name: str
) -> int:
    """读取有界正整数；空值用默认。

    @raises ValueError: 非整数或越界。
    """
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} 必须是整数") from error
    if not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")
    return value
