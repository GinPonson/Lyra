"""已认证请求的用户与设备身份。

P0 冻结契约 §3.8 规定：多用户下「谁在访问」由**一个不可变对象**表达，而不是散落在
各路由的 ``request.state.device`` / 请求参数里。

**只由服务端构造**：构造点是 :meth:`lyra.application.device_authenticator.DeviceAuthenticator.authenticate`
——它在「访问令牌有效 → 设备未撤销 → 用户仍活动」三步全部通过后才产出本对象。
路由处理器、请求正文与模型参数**不得**自行拼装或覆盖它；业务访问的 userId 只能
来自这里，否则一个伪造的查询参数就能读到别人的账本（计划 §4.2）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..infrastructure.db.user_repository import UserRole


@dataclass(frozen=True)
class AuthContext:
    """一次已认证请求的身份：谁（用户）、从哪台设备、什么角色。

    @param user_id: 当前用户的 UUID 文本；业务仓储的用户范围一律取自它。
    @param device_id: 当前设备（会话）的 UUID 文本；凭据撤销与审计用。
    @param role: 用户的角色，``admin`` 或 ``member``；管理路由据此判定。
    """

    user_id: str
    device_id: str
    role: UserRole
