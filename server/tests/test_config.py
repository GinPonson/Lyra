"""环境配置契约测试。

迁移期间两侧必须能被**同一份 .env** 驱动。因此这里不止验证「能读」，
还直接拿 TypeScript 的 ``server-environment.ts`` 做对照，确保环境变量名
集合完全一致——配置面差异是重写中最容易被忽略、又最难在运行时发现的一类回归。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from lyra.config import Settings

MASTER_KEY = "a" * 64

# **环境变量契约（签入）。**
#
# 它此前是与 TypeScript 版 ``server-environment.ts`` 的**逐名比对**——那份耦合已随 TS
# 服务端移除。契约改为记录在此：**保护没有消失，只是从"比对另一个实现"变成了"回归检测"**。
#
# 新增或改名环境变量时必须同时改这里：部署配置（systemd / 容器 / 密钥注入）按这些名字
# 提供值，**静默改名会让服务在启动时才发现配置读不到**。
EXPECTED_ENVIRONMENT_NAMES = {
    "LYRA_DATABASE_PATH",
    "LYRA_GOOGLE_CLIENT_ID",
    "LYRA_GOOGLE_CLIENT_SECRET",
    "LYRA_GOOGLE_REDIRECT_URI",
    "LYRA_HOST",
    "LYRA_IMPORT_STAGING_DIR",
    "LYRA_PORT",
    "LYRA_SECRET_MASTER_KEY",
}


def _required_environment(database_path: str = "/tmp/lyra.duckdb") -> dict[str, str]:
    """构造满足必填项的环境变量。"""
    return {
        "LYRA_DATABASE_PATH": database_path,
        "LYRA_IMPORT_STAGING_DIR": "/tmp/lyra-staging",
        "LYRA_SECRET_MASTER_KEY": MASTER_KEY,
    }


def _apply(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> None:
    """设置环境；未给出的 LYRA_* 变量一并清空，避免宿主机环境污染断言。"""
    for name in list(Settings.model_fields.values()):
        alias = name.validation_alias
        if isinstance(alias, str):
            monkeypatch.delenv(alias, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)


def test_defaults_match_typescript(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时的默认值必须与 TypeScript 版一致。"""
    _apply(monkeypatch, _required_environment())

    settings = Settings()

    assert settings.lyra_host == "127.0.0.1"
    assert settings.lyra_port == 3141
    assert settings.lyra_google_client_id == ""


def test_missing_required_variable_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """必填项缺失必须报错，不得回落到隐式默认值。"""
    environment = _required_environment()
    del environment["LYRA_DATABASE_PATH"]
    _apply(monkeypatch, environment)

    with pytest.raises(ValidationError):
        Settings()


def test_whitespace_only_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯空白等同未配置（对应 Zod 的 .trim()）。"""
    environment = _required_environment(database_path="   ")
    _apply(monkeypatch, environment)

    with pytest.raises(ValidationError):
        Settings()


def test_master_key_must_be_32_bytes_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    """主密钥必须是 64 位 hex；长度或字符集不符都要拒绝。"""
    for invalid in ("short", "z" * 64, "0" * 63, "0" * 65):
        environment = _required_environment()
        environment["LYRA_SECRET_MASTER_KEY"] = invalid
        _apply(monkeypatch, environment)
        with pytest.raises(ValidationError):
            Settings()


def test_port_out_of_range_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """端口必须落在 1–65535。"""
    for invalid in ("0", "65536"):
        environment = _required_environment()
        environment["LYRA_PORT"] = invalid
        _apply(monkeypatch, environment)
        with pytest.raises(ValidationError):
            Settings()


def test_environment_variable_names_match_the_recorded_contract() -> None:
    """**环境变量名集合必须与签入的契约一致。**

    部署配置按这些名字提供值。**静默改名不会在构建时失败**——服务要等到启动读配置时
    才发现，而那时它已经在生产环境里了。

    契约从"与另一个实现比对"改为"签入并回归检测"：后者不会因为另一个实现消失而失去
    保护。
    """
    python_names = {
        field.validation_alias
        for field in Settings.model_fields.values()
        if isinstance(field.validation_alias, str)
    }

    assert python_names == EXPECTED_ENVIRONMENT_NAMES, (
        f"契约里有而代码没有: {sorted(EXPECTED_ENVIRONMENT_NAMES - python_names)}；"
        f"代码里有而契约没有: {sorted(python_names - EXPECTED_ENVIRONMENT_NAMES)}"
    )
