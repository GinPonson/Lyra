# 共享协议样本

本目录存放**服务端与客户端共读**的协议样本，用于发现两侧的协议漂移。

## `samples.json`

由 [`server/scripts/export_contract_samples.py`](../server/scripts/export_contract_samples.py) 生成。

**为什么放在仓库根**：它既不属于服务端也不属于客户端。放在任一侧的测试夹具目录里，
另一侧就会把它当成「别人的夹具」而各自再手写一份——而**两边各自手写的样本可以同时
自洽却互相不符**，漂移正好被两边各自的测试掩盖。放在这里，两边读的是同一份字节。

**它是真实响应，不是手写样本**：生成器装配真实应用，只把 OAuth 来源客户端换成脚本
替身，然后走真实的 HTTP 端点抓取响应。目前覆盖 **7 条**用例，对应计划 §8 列出的五类：

| 用例 | 端点 |
| --- | --- |
| `login` | `POST /v1/auth/{provider}/exchange` |
| `me` | `GET /v1/me` |
| `createAccount` | `POST /v1/ledger/accounts` |
| `ledgerAccounts` | `GET /v1/ledger/accounts?scope=all` |
| `ledgerSummary` | `GET /v1/ledger/summary?scope=all` |
| `importPreview` | `POST /v1/imports/preview`（上传一份合成 Schwab CSV） |
| `marketSettings` | `GET /v1/market/settings` |

客户端侧由 `apps/mobile/test/contract_samples_test.dart` 用**生产解析器**逐条解析
（`LyraUserProfile`、`BrokerageAccount`、`LedgerSummaryRead`、`ImportPreviewView`、
`MarketSettings`）。

注意：创建类端点返回 **201**，读取类返回 **200**；且 `createAccount` 的正文直接是
`{account}`，**不带 `{metadata,data}` 信封**（与账本读取不同）。

**只含合成数据**：临时目录里的空库 + 脚本身份。不读取真实凭据或真实账本，不访问任何
外部服务。

## 改协议时怎么办

**不要手工编辑 `samples.json`**。改完服务端后在 `server/` 下重跑：

```bash
cd server
UV_CACHE_DIR=../.uv-tmp/cache UV_PYTHON_INSTALL_DIR=../.uv-tmp/python \
  uv run python scripts/export_contract_samples.py
```

然后跑客户端侧的消费测试：

```bash
cd apps/mobile
flutter test --no-pub test/contract_samples_test.dart
```

两侧任何一方改了字段名或形状，这条测试都会立刻失败——这正是它存在的唯一理由。
