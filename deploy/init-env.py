"""为一次**新部署**生成部署密钥文件。

从 ``.env.example`` / ``server.env.example`` 生成 ``.env`` / ``server.env``，把
``=GENERATE_ONCE`` 替换为随机 32 字节的十六进制主密钥。

**三个刻意的性质**：

1. **排他创建**（``O_EXCL``）——已存在的部署不会被覆盖。主密钥一旦被换掉，
   **此前用旧密钥加密的密文就再也解不开**（Gmail 凭据等），而症状是"连接还在但读不出
   令牌"，极难定位；
2. **权限 0600**——密钥文件的默认权限过于宽松；
3. **不打印任何密钥值**——终端与 CI 日志都会被留存。

用法：

    python3 deploy/init-env.py
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
PLACEHOLDER = "=GENERATE_ONCE"


def main() -> int:
    """生成两份环境文件。"""
    exit_code = 0
    for name in (".env", "server.env"):
        template_path = DEPLOY_DIR / f"{name}.example"
        target_path = DEPLOY_DIR / name
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError:
            print(f"读不到模板：{template_path.name}", file=sys.stderr)
            exit_code = 1
            continue

        content = template.replace(
            PLACEHOLDER, "=" + secrets.token_hex(32)
        )
        try:
            # ``O_EXCL`` 是排他创建：文件已存在即失败，**绝不覆盖**。
            descriptor = os.open(
                target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            print(f"保留已有的 deploy/{name}；未改动任何值。")
            continue

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError:
            print(f"无法写入 deploy/{name}。", file=sys.stderr)
            exit_code = 1
            continue
        print(f"已创建 deploy/{name}（0600）。")

    if exit_code == 0:
        print(
            "新部署请在本地编辑这两个文件；升级时请沿用原来的主密钥。"
            "未打印任何密钥值。"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
