"""服务端环境配置。

字段、必填性与默认值由**本项目的契约**固定（见 `tests/test_config.py` 的
``EXPECTED_ENVIRONMENT_NAMES``）——部署配置按这些名字提供值：
同名环境变量、同样的约束。迁移期间两侧必须能被**同一份 .env** 驱动，
否则切换会引入配置面差异——这正是最容易在重写中被忽略的一类回归。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """已校验的服务端环境。

    每个字段用 ``validation_alias`` 显式声明对应的 ``LYRA_*`` 环境变量名，
    而不是依赖字段名的大小写映射——环境契约应当是可 grep 的，
    且两侧同名这一点必须由代码直接表达，而不是由框架约定隐式决定。
    ``str_strip_whitespace`` 对应 Zod 的 ``.trim()``：纯空白视同未配置。
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    lyra_host: str = Field(default="127.0.0.1", min_length=1, validation_alias="LYRA_HOST")
    lyra_port: int = Field(default=3141, ge=1, le=65_535, validation_alias="LYRA_PORT")
    lyra_database_path: str = Field(min_length=1, validation_alias="LYRA_DATABASE_PATH")
    lyra_import_staging_dir: str = Field(min_length=1, validation_alias="LYRA_IMPORT_STAGING_DIR")
    # 32 字节 hex 主密钥；Gmail refresh token 等敏感数据经 AES-256-GCM 加密。
    lyra_secret_master_key: str = Field(
        pattern=r"^[0-9a-fA-F]{64}$", validation_alias="LYRA_SECRET_MASTER_KEY"
    )
    lyra_google_client_id: str = Field(default="", validation_alias="LYRA_GOOGLE_CLIENT_ID")
    lyra_google_client_secret: str = Field(default="", validation_alias="LYRA_GOOGLE_CLIENT_SECRET")
    lyra_google_redirect_uri: str = Field(default="", validation_alias="LYRA_GOOGLE_REDIRECT_URI")
