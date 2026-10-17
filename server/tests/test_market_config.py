"""行情配置测试。

两条最容易做错的：

1. **只给一半凭据也算错误**——操作者显然打算配置它，只是填漏了；让服务带着这种配置
   启动只会把错误推迟到用户点击时；
2. **秘钥禁含换行**——换行会把后续内容变成新的请求头，即请求头注入。
"""

from __future__ import annotations

import pytest

from lyra.infrastructure.market_data.config import (
    DEFAULT_DAILY_REQUEST_LIMIT,
    DEFAULT_MARKETDATA_MODE,
    DEFAULT_TIMEOUT_MS,
    load_market_data_config,
    parse_provider_route,
)

# --------------------------------------------------------------------------
# 路由解析
# --------------------------------------------------------------------------


def test_default_route_is_empty() -> None:
    """未配置即不启用任何来源。"""
    assert parse_provider_route(None) == ()
    assert parse_provider_route("") == ()
    assert parse_provider_route("  ") == ()
    assert parse_provider_route("none") == ()


def test_parses_a_route_list() -> None:
    """逗号分隔并去除空白，保序。"""
    assert parse_provider_route("alpaca, marketdata") == ("alpaca", "marketdata")
    assert parse_provider_route("marketdata") == ("marketdata",)


def test_rejects_duplicate_providers() -> None:
    """重复项必须拒绝——通常说明配置里粘了两遍。"""
    with pytest.raises(ValueError, match="重复项"):
        parse_provider_route("alpaca,alpaca")


def test_rejects_unknown_provider() -> None:
    """未知来源必须拒绝，而不是静默忽略。"""
    with pytest.raises(ValueError, match="未知的行情来源"):
        parse_provider_route("bloomberg")


# --------------------------------------------------------------------------
# 完整配置
# --------------------------------------------------------------------------


def test_loads_defaults_without_any_configuration() -> None:
    """完全没有行情配置时使用默认值。"""
    config = load_market_data_config({})

    assert config.stock_providers == ()
    assert config.option_providers == ()
    assert config.marketdata_mode == DEFAULT_MARKETDATA_MODE
    assert config.daily_request_limit == DEFAULT_DAILY_REQUEST_LIMIT
    assert config.timeout_ms == DEFAULT_TIMEOUT_MS


def test_loads_a_complete_alpaca_configuration() -> None:
    """完整 Alpaca 配置通过。"""
    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "alpaca",
        "LYRA_ALPACA_API_KEY": "key",
        "LYRA_ALPACA_SECRET_KEY": "secret",
    })

    assert config.stock_providers == ("alpaca",)
    assert config.alpaca_api_key == "key"


def test_selected_provider_requires_credentials() -> None:
    """**被选中的来源必须有凭据**——缺凭据不是"过一会儿再试"，而是每次都失败。"""
    with pytest.raises(ValueError, match="必须同时配置"):
        load_market_data_config({"LYRA_MARKET_STOCK_PROVIDERS": "alpaca"})

    with pytest.raises(ValueError, match="必须配置 API token"):
        load_market_data_config({"LYRA_MARKET_STOCK_PROVIDERS": "marketdata"})


def test_half_configured_credentials_are_rejected_even_when_not_routed() -> None:
    """**只给一半凭据同样算错误**，即使当前没被路由选中。

    操作者显然打算配置它，只是填漏了——启动时失败比运行时每次失败好。
    """
    with pytest.raises(ValueError, match="必须同时配置"):
        load_market_data_config({"LYRA_ALPACA_API_KEY": "key"})

    with pytest.raises(ValueError, match="必须同时配置"):
        load_market_data_config({"LYRA_ALPACA_SECRET_KEY": "secret"})


def test_both_credentials_without_routing_is_allowed() -> None:
    """一对凭据齐全但不路由：合法（预配置，尚未启用）。"""
    config = load_market_data_config({
        "LYRA_ALPACA_API_KEY": "key", "LYRA_ALPACA_SECRET_KEY": "secret"
    })

    assert config.stock_providers == ()
    assert config.alpaca_api_key == "key"


# --------------------------------------------------------------------------
# 秘钥
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret", ["key\rX", "key\nX", "key\r\nX"]
)
def test_secrets_must_not_contain_line_breaks(secret: str) -> None:
    """**换行会把后续内容变成新的请求头**——请求头注入防线。"""
    with pytest.raises(ValueError, match="不能包含换行"):
        load_market_data_config({
            "LYRA_MARKET_STOCK_PROVIDERS": "alpaca",
            "LYRA_ALPACA_API_KEY": secret,
            "LYRA_ALPACA_SECRET_KEY": "s",
        })


def test_oversized_secret_is_rejected() -> None:
    """超长秘钥拒绝。"""
    with pytest.raises(ValueError, match="过长"):
        load_market_data_config({
            "LYRA_MARKET_STOCK_PROVIDERS": "alpaca",
            "LYRA_ALPACA_API_KEY": "x" * 8193,
            "LYRA_ALPACA_SECRET_KEY": "s",
        })


def test_blank_secret_is_treated_as_absent() -> None:
    """纯空白秘钥视为未配置。"""
    config = load_market_data_config({})
    assert config.alpaca_api_key is None


# --------------------------------------------------------------------------
# 数值与模式
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["historical", "delayed"])
def test_accepts_both_marketdata_modes(mode: str) -> None:
    """两种模式都接受。"""
    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "marketdata",
        "LYRA_MARKETDATA_API_TOKEN": "t",
        "LYRA_MARKETDATA_MODE": mode,
    })
    assert config.marketdata_mode == mode


def test_rejects_unknown_marketdata_mode() -> None:
    """未知模式必须拒绝。"""
    with pytest.raises(ValueError, match="historical 或 delayed"):
        load_market_data_config({"LYRA_MARKETDATA_MODE": "live"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LYRA_MARKET_TIMEOUT_MS", "999"),
        ("LYRA_MARKET_TIMEOUT_MS", "15001"),
        ("LYRA_MARKETDATA_DAILY_REQUEST_LIMIT", "0"),
        ("LYRA_MARKETDATA_DAILY_REQUEST_LIMIT", "100001"),
        ("LYRA_MARKET_TIMEOUT_MS", "abc"),
    ],
)
def test_out_of_range_numbers_are_rejected(name: str, value: str) -> None:
    """数值越界或非整数必须拒绝。"""
    with pytest.raises(ValueError):
        load_market_data_config({name: value})


def test_limits_can_be_overridden() -> None:
    """限额可覆盖。"""
    config = load_market_data_config({
        "LYRA_MARKET_TIMEOUT_MS": "12000",
        "LYRA_MARKETDATA_DAILY_REQUEST_LIMIT": "200",
    })

    assert config.timeout_ms == 12_000
    assert config.daily_request_limit == 200


def test_blank_numbers_fall_back_to_defaults() -> None:
    """空数值用默认。"""
    config = load_market_data_config({
        "LYRA_MARKET_TIMEOUT_MS": "  ", "LYRA_MARKETDATA_DAILY_REQUEST_LIMIT": ""
    })

    assert config.timeout_ms == DEFAULT_TIMEOUT_MS
    assert config.daily_request_limit == DEFAULT_DAILY_REQUEST_LIMIT


def test_stock_and_option_routes_are_independent() -> None:
    """正股与期权可以走不同来源。"""
    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "alpaca",
        "LYRA_MARKET_OPTION_PROVIDERS": "marketdata",
        "LYRA_ALPACA_API_KEY": "k",
        "LYRA_ALPACA_SECRET_KEY": "s",
        "LYRA_MARKETDATA_API_TOKEN": "t",
    })

    assert config.stock_providers == ("alpaca",)
    assert config.option_providers == ("marketdata",)


def test_primary_and_secondary_providers_are_ordered() -> None:
    """路由顺序即主备顺序。"""
    config = load_market_data_config({
        "LYRA_MARKET_STOCK_PROVIDERS": "alpaca,marketdata",
        "LYRA_ALPACA_API_KEY": "k",
        "LYRA_ALPACA_SECRET_KEY": "s",
        "LYRA_MARKETDATA_API_TOKEN": "t",
    })

    assert config.stock_providers == ("alpaca", "marketdata")
