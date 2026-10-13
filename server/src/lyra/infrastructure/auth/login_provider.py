"""登录来源契约。

镜像 TypeScript 版 ``login-provider.ts``。**三种来源共享同一份私有账本，但不
按邮箱或用户名自动关联身份**：每个来源各自绑定不可变的数字 owner ID，同名或
同邮箱不会获得权限。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

LoginProviderId = Literal["google", "linuxdo", "github"]

# 登录目录固定顺序；界面按此顺序展示。
LOGIN_PROVIDER_IDS: tuple[LoginProviderId, ...] = ("google", "linuxdo", "github")

LOGIN_PROVIDER_NAMES: dict[LoginProviderId, str] = {
    "google": "Google",
    "linuxdo": "LINUX DO",
    "github": "GitHub",
}


@dataclass(frozen=True)
class LoginIdentity:
    """只保留验证过的来源身份。

    第三方 access token **永不**进入 App 会话或持久化层，因此这里没有它的位置。
    """

    provider: LoginProviderId
    subject: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class ConfiguredLoginProvider:
    """只有完整配置且绑定明确 owner 的来源才可进入可用登录目录。"""

    id: LoginProviderId
    name: str
    client: Any
    assert_owner: Callable[[LoginIdentity], None]
