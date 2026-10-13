"""Agent 模型配置测试。

两条要点：

1. **部分配置必须报错**。否则服务会以"看起来启用了但每次对话都失败"的状态启动；
2. **三项缺一即视为未配置**，此时执行器必须抛错而不是静默返回空回答——静默失败会
   让用户以为模型"什么也没说"，而实际上是服务端没配好。
"""

from __future__ import annotations

import pytest

from lyra.application.agent_config import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_MS,
    load_genkit_config,
)

FULL = {
    "LYRA_GENKIT_BASE_URL": "https://models.example.com/v1",
    "LYRA_GENKIT_MODEL": "gpt-5",
    "LYRA_GENKIT_API_KEY": "sk-test",
}


def test_returns_none_when_fully_unconfigured() -> None:
    """三项全空 → 未启用。"""
    assert load_genkit_config({}) is None
    assert load_genkit_config({"LYRA_GENKIT_MODEL": "   "}) is None


def test_accepts_complete_configuration() -> None:
    """完整配置通过并使用默认限额。"""
    config = load_genkit_config(FULL)

    assert config is not None
    assert config.base_url == "https://models.example.com/v1"
    assert config.model == "gpt-5"
    assert config.turn_timeout_ms == DEFAULT_TIMEOUT_MS
    assert config.max_turns == DEFAULT_MAX_TURNS


@pytest.mark.parametrize("missing", ["LYRA_GENKIT_BASE_URL", "LYRA_GENKIT_MODEL", "LYRA_GENKIT_API_KEY"])
def test_partial_configuration_is_rejected(missing: str) -> None:
    """少任何一项都必须报错。"""
    partial = {key: value for key, value in FULL.items() if key != missing}

    with pytest.raises(ValueError, match="缺少"):
        load_genkit_config(partial)


def test_trailing_slash_is_stripped() -> None:
    """基础地址去掉尾部斜杠，避免拼接出双斜杠路径。"""
    config = load_genkit_config({**FULL, "LYRA_GENKIT_BASE_URL": "https://x.example.com/v1/"})
    assert config is not None and config.base_url == "https://x.example.com/v1"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://models.example.com",
        "https://user:pw@models.example.com/v1",
        "https://models.example.com/v1?key=secret",
        "https://models.example.com/v1#frag",
        "models.example.com/v1",
    ],
)
def test_invalid_base_url_is_rejected(url: str) -> None:
    """凭据、查询串、片段与非 HTTP(S) 一律拒绝。

    带凭据的 URL 会把密钥写进日志与错误信息；带查询串的常常是复制粘贴时多带了参数。
    """
    with pytest.raises(ValueError, match="必须是无凭据"):
        load_genkit_config({**FULL, "LYRA_GENKIT_BASE_URL": url})


def test_limits_can_be_overridden() -> None:
    """限额可覆盖。"""
    config = load_genkit_config({
        **FULL, "LYRA_AGENT_TURN_TIMEOUT_MS": "30000", "LYRA_AGENT_MAX_TURNS": "5"
    })

    assert config is not None
    assert config.turn_timeout_ms == 30_000
    assert config.max_turns == 5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LYRA_AGENT_TURN_TIMEOUT_MS", "999"),      # 低于下限
        ("LYRA_AGENT_TURN_TIMEOUT_MS", "600001"),   # 高于上限
        ("LYRA_AGENT_MAX_TURNS", "0"),
        ("LYRA_AGENT_MAX_TURNS", "33"),
        ("LYRA_AGENT_MAX_TURNS", "abc"),
    ],
)
def test_out_of_range_limits_are_rejected(name: str, value: str) -> None:
    """限额越界或非整数必须拒绝。"""
    with pytest.raises(ValueError):
        load_genkit_config({**FULL, name: value})


def test_blank_limits_fall_back_to_defaults() -> None:
    """空限额用默认值，而不是报错。"""
    config = load_genkit_config({
        **FULL, "LYRA_AGENT_TURN_TIMEOUT_MS": "  ", "LYRA_AGENT_MAX_TURNS": ""
    })

    assert config is not None
    assert config.turn_timeout_ms == DEFAULT_TIMEOUT_MS
    assert config.max_turns == DEFAULT_MAX_TURNS
