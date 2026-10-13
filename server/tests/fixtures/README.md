# 测试夹具

## `typescript-ledger-schema15.duckdb`

**来源**：迁移前的 TypeScript 服务端创建（`server/data/lyra.duckdb`），在移除该服务端
之前**原样冻结**至此。

**内容**：schema 版本 15、28 张表、15 条迁移记录、**无任何数据**
（账户 0 / 交易 0 / 配对设备 0 / Gmail 连接 0）。因此它可以安全签入仓库。

**为什么要冻结而不是实时读取**：它证明的是「**Python 能无损打开一个不是自己创建的
schema 15 库**」——这是切换后**唯一不能重试**的能力。若继续实时读取，TypeScript 服务端
一移除，这份证据就随之消失。

读取它的测试：`tests/test_migrations.py`。

**它由只读方式打开，且测试先复制到临时目录再打开**——DuckDB 同一时刻只允许一个进程
打开一个文件，直接打开会让并行测试相互冲突。

## `parity-fixtures.json`

迁移期的差分对照语料：每条用例含输入与**真实执行结果**（由 oracle `schwab-cli` 录制）。

覆盖 29 份 CSV、11 组 FIFO、19 个账本 × 16 条命令，合计 **2771 个字段**逐项比对。

**局限（重要）**：差分测试只能证明「在这些输入上一致」，**不能证明「规则一致」**。
迁移期实测确认过这一点——把排序键改错时，差分测试 19 项全过，因为语料里没有跨账户的
键碰撞。因此本目录的路由清单与 `tests/test_route_manifest.py` 之类的**契约测试**是
互补的，不是重复的。

## `route-manifest.json`

路由契约：47 个 (方法, 路径) 对 + 1 个 WebSocket，即迁移期与 TypeScript 版逐项对账的
结果。

**记录的是 (方法, 路径) 对而不是路径集合**——只比路径会漏掉「某条路径上少了一个方法」，
而这正是迁移期真实发生过的误判（`POST /v1/ledger/accounts` 整块缺失）。

新增或删除路由时必须同时改它，由 `tests/test_route_manifest.py` 守住。

## 其他

- `crypto-fixtures.json`、`device-keys.json`：密钥与凭据测试用例。

**本目录不含 `application-gin.yml` 或任何真实凭据。**
