"""导出供服务端与 Flutter 共用的合成契约样本（计划 §8）。

**为什么需要它**：服务端与客户端各自手写样本时，两份样本可以同时"自洽"却互相
不符——漂移被两边各自的测试掩盖。这里让**服务端自己产出一份真实响应**并落盘，
Flutter 侧再用**生产解析器**读同一份文件，于是任何一侧改了字段名或形状，另一侧
的测试立刻失败。

**它走真实 HTTP 端点**：登录样本来自真实的 `/v1/auth/{provider}/exchange`，`/me`
样本来自真实的 `GET /v1/me`。只把 OAuth 来源客户端换成脚本替身——自动注册、身份
解析、设备签发、凭据签发全部走生产代码。这样样本反映的是**协议**，不是某个内部
函数的返回值。

**只用合成数据**：临时目录里的空库 + 脚本身份。不读取真实凭据、真实账本，也不
访问任何外部服务。

用法（在 `server/` 下）：

```bash
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \\
  uv run python scripts/export_contract_samples.py
```
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from datetime import UTC, datetime

from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.login_service import UserLoginService
from lyra.application.login_service import pkce_challenge
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.infrastructure.auth.login_provider import (
    ConfiguredLoginProvider,
    LoginIdentity,
)
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.infrastructure.db.invite_code_repository import InviteCodeRepository
from lyra.infrastructure.db.user_repository import UserRepository
from lyra.main import create_app

SERVER_PY = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = SERVER_PY.parent
#: 共享样本的落点。放在仓库根的 ``contracts/``：它既不属于服务端也不属于客户端，
#: 两边都按相对路径读它，避免任一侧把它当成"自己的夹具"而各改一份。
OUTPUT = REPOSITORY_ROOT / "contracts" / "samples.json"

_KEYS = json.loads(
    (SERVER_PY / "tests" / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)
PUBLIC_KEY_PEM = _KEYS["cases"][0]["inputPem"]
#: 合成身份。**不用真实账号**：样本只需要一个形态合法的稳定主体。
IDENTITY = LoginIdentity(
    provider="github", subject="9001", email="sample@example.com", display_name="样例用户"
)
_VERIFIER = "v" * 43
_CHALLENGE = pkce_challenge(_VERIFIER)

#: 合成账户标识。**与 ``account_service._ACCOUNT_IDENTIFIER`` 同形**（大写字母数字与
#: 连字符 + 末尾三位数字）：形态不符会让样本采集在第一步就 400，而不是产出坏样本。
ACCOUNT_IDENTIFIER = "TST001"
#: 一份最小但合法的 Schwab 八列 CSV。日期用 Schwab 的 ``MM/DD/YYYY`` 形态。
_SAMPLE_CSV = (
    "Date,Action,Symbol,Description,Quantity,Price,Fees & Comm,Amount\n"
    '"01/05/2026","Buy","INTC","INTEL CORP","10","$40.00","","-$400.00"\n'
).encode()


class _ScriptedLoginClient:
    """按脚本返回来源身份的 OAuth 客户端替身。

    只实现路由层用得到的两个动作；参数校验与真实客户端无关，已由 `test_google_login`
    与 `test_oauth_login` 覆盖。
    """

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        """返回带 state 的固定授权地址，供回调步骤解析。"""
        return f"https://provider.example/authorize?state={state}"

    def exchange_code(self, **kwargs: Any) -> LoginIdentity:
        """返回脚本身份。"""
        return IDENTITY


def _record(
    name: str, method: str, path: str, response: Any
) -> dict[str, Any]:
    """把一条真实响应收成样本条目。

    @param name: 用例名，供客户端按下标查找。
    @param method: HTTP 方法。
    @param path: 请求路径（含查询串时如实记录）。
    @param response: ``httpx`` 响应对象。
    @returns 样本条目。
    """
    assert response.status_code in (200, 201), f"{name}: {response.status_code} {response.text}"
    return {
        "name": name,
        "method": method,
        "path": path,
        "status": response.status_code,
        "body": response.json(),
    }


def _capture(test_client: TestClient) -> list[dict[str, Any]]:
    """走一遍真实登录、建账户、账本、导入预览与行情设置，逐条收集响应。

    @param test_client: 已装配应用的测试客户端。
    @returns 样本条目列表；每条含名称、方法、路径、状态码与响应正文。
    """
    cases: list[dict[str, Any]] = []

    # 新身份注册必须凭邀请码：样本也要按客户端的真实流程拿一枚。
    _, invite_code = InviteCodeRepository(
        test_client.app.state.database.connection
    ).create(created_by=None, label="契约样本", now=datetime.now(UTC))
    started = test_client.post(
        "/v1/auth/github/start",
        json={
            "displayName": "样例设备",
            "publicKeyPem": PUBLIC_KEY_PEM,
            "codeChallenge": _CHALLENGE,
            "codeChallengeMethod": "S256",
            "inviteCode": invite_code,
        },
    )
    assert started.status_code == 200, started.text
    started_body = started.json()

    state = parse_qs(urlsplit(started_body["authorizationUrl"]).query)["state"][0]
    callback = test_client.get(
        "/v1/auth/github/callback", params={"state": state, "code": "sample-code"}
    )
    assert callback.status_code == 200, callback.text

    login = test_client.post(
        "/v1/auth/github/exchange",
        json={"loginId": started_body["loginId"], "codeVerifier": _VERIFIER},
    )
    cases.append(
        _record("login", "POST", "/v1/auth/github/exchange", login)
    )
    auth = {"Authorization": f"Bearer {login.json()['accessToken']}"}

    cases.append(_record("me", "GET", "/v1/me", test_client.get("/v1/me", headers=auth)))

    # 先建一个账户，后面的账本与导入样本才有非空内容——空账本能过解析，但覆盖不到
    # 真实字段（脱敏后缀、来源追溯等）。
    created = test_client.post(
        "/v1/ledger/accounts",
        json={
            "displayName": "样例账户",
            "accountIdentifier": ACCOUNT_IDENTIFIER,
            "baseCurrency": "USD",
        },
        headers=auth,
    )
    cases.append(_record("createAccount", "POST", "/v1/ledger/accounts", created))
    # 创建账户的响应是 ``{account}``（不带 ``{metadata,data}`` 信封），与账本读取不同。
    account_id = created.json()["account"]["id"]

    cases.append(
        _record(
            "ledgerAccounts",
            "GET",
            "/v1/ledger/accounts?scope=all",
            test_client.get("/v1/ledger/accounts", params={"scope": "all"}, headers=auth),
        )
    )
    cases.append(
        _record(
            "ledgerSummary",
            "GET",
            "/v1/ledger/summary?scope=all",
            test_client.get("/v1/ledger/summary", params={"scope": "all"}, headers=auth),
        )
    )
    cases.append(
        _record(
            "importPreview",
            "POST",
            "/v1/imports/preview?accountId=<上传的 CSV>",
            test_client.post(
                "/v1/imports/preview",
                params={"accountId": account_id},
                content=_SAMPLE_CSV,
                headers={**auth, "x-lyra-file-name": f"Individual_{ACCOUNT_IDENTIFIER}_Transactions_20260105.csv"},
            ),
        )
    )
    cases.append(
        _record(
            "marketSettings",
            "GET",
            "/v1/market/settings",
            test_client.get("/v1/market/settings", headers=auth),
        )
    )
    return cases


def build() -> dict[str, Any]:
    """在临时库上装配应用并导出样本。

    @returns 待写入的样本文档。
    """
    from lyra.protocol import CLIENT_PROTOCOL_HEADERS

    with tempfile.TemporaryDirectory() as directory:
        database_path = os.path.join(directory, "contract-samples.duckdb")
        database = open_lyra_database(database_path)
        try:
            settings = Settings(
                LYRA_DATABASE_PATH=database_path,
                LYRA_IMPORT_STAGING_DIR=os.path.join(directory, "staging"),
                LYRA_SECRET_MASTER_KEY="0" * 64,
            )
            application = create_app(settings, database)
            connection = database.connection
            # 注册门禁（邀请码）装在生产的签发函数里；样本生成必须走同一条路径，
            # 否则"样本产得出来"并不能说明客户端面对的真实契约。
            from lyra.main import _authorize_user_device

            async def authorize(**kwargs: Any) -> dict[str, Any]:
                """兑换成功后的用户解析与设备签发（直接复用生产装配）。"""
                return await _authorize_user_device(
                    database=database,
                    write_coordinator=application.state.write_coordinator,
                    **kwargs,
                )

            application.state.login_service = UserLoginService(
                [
                    ConfiguredLoginProvider(
                        id="github",
                        name="GitHub",
                        client=_ScriptedLoginClient(),
                    )
                ],
                authorize,
                InviteCodeRepository(connection),
            )
            with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
                cases = _capture(client)
        finally:
            database.close()

    return {
        "generatedBy": "server/scripts/export_contract_samples.py",
        "note": (
            "由服务端真实端点产出的合成响应样本，供 Flutter 的生产解析器回归使用。"
            "**不要手工编辑**：改协议时重跑生成器。样本只含合成数据。"
        ),
        "cases": cases,
    }


def main() -> int:
    """导出样本并写入 ``contracts/samples.json``。

    @returns 进程退出码；始终为 0（失败时抛异常）。
    """
    document = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    names = ", ".join(case["name"] for case in document["cases"])
    print(f"已写入 {OUTPUT.relative_to(REPOSITORY_ROOT)}（{len(document['cases'])} 条：{names}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
