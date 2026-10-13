# Lyra 服务端（Python/FastAPI）

Lyra 的**唯一**服务端实现。TypeScript/Fastify 版已删除（提交 `ed522c7`），只存在于
git 历史里（最后一个 TS 提交是 `6887bec`）；迁移过程与验收标准见
[迁移计划](../docs/plans/lyra-python-migration-20261012.md)，切流与回退步骤见
[切流运行手册](../docs/plans/lyra-cutover-runbook-20261013.md)。

> **当前状态：服务端已整体切换。** 账本 schema 保持 **15**、客户端协议保持 **2**
> （Flutter 端无需改协议）；Python 侧能打开 TypeScript 创建的 schema 15 库——证据是
> `tests/fixtures/typescript-ledger-schema15.duckdb`（迁移前由 TS 服务端创建，28 张表、
> 无数据）。切换是**原子**的：DuckDB 同一时刻只允许一个进程打开账本文件。
>
> 客户端连接的就是本服务；部署方式见[服务端部署](../docs/deployment.md)。

## 运行

需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。环境变量沿用 `LYRA_*` 命名
（`LYRA_HOST` / `LYRA_PORT` / `LYRA_DATABASE_PATH` / `LYRA_IMPORT_STAGING_DIR` /
`LYRA_SECRET_MASTER_KEY` 以及各登录来源与 Genkit 组），字段与必填性由
`tests/test_config.py` 的契约固定。

**服务只读进程环境变量，不解析 `.env` 文件**；部署侧的环境变量模板是
`../deploy/server.env.example`（由 `../deploy/init-env.py` 生成为 `deploy/server.env`，
权限 0600）。

```bash
uv run pytest          # 测试
uv run ruff check .    # 错误类静态检查（项目级；规则见 pyproject.toml）

export LYRA_DATABASE_PATH=/var/lib/lyra/lyra.duckdb
export LYRA_IMPORT_STAGING_DIR=/tmp/lyra-import-staging
export LYRA_SECRET_MASTER_KEY=<64 位 hex>
uv run python -m lyra.main
```

正式入口是 `uv run lyra-serve`（`pyproject.toml` 的 `[project.scripts]`），与
`uv run python -m lyra.main` 等价。默认监听 `127.0.0.1:3141`，与 TypeScript 版一致。
配置缺失、主密钥形态不对、数据库打不开或迁移失败时，`run()` 直接退出，不进入"半启动"。

## 目录

```text
src/lyra/
├── main.py           应用工厂与 uvicorn 入口（`lyra-serve` = `lyra.main:run`）
├── config.py         环境校验（字段与 TypeScript 版一一对应，见 tests/test_config.py）
├── protocol.py       协议版本与 /v1/system/info 元数据、客户端协议头常量
├── db/
│   ├── migrations.py 15 条迁移清单与执行语义
│   ├── connection.py 打开账本、溢出目录、健康探测
│   └── sql/          迁移 SQL（**唯一来源**）
├── api/              health / system / 认证 / 账本 / 导入 / 行情 / Agent / Gmail 路由
├── application/      用例编排（导入、配对、行情设置、Agent 回合等）
├── domain/           账本、FIFO、分析与 Agent 领域逻辑
├── infrastructure/   仓储、认证客户端、行情与模型运行时
└── security/         AES-256-GCM 秘密加密（与 TS 密文双向互通）
scripts/              预检 / 备份 / 回退 / 运行自检（见运行手册）
tests/                测试与冻结夹具（`fixtures/`）
```

## 两条纪律

1. **不修改 schema（保持 15）、不修改客户端协议（保持 2）。**
2. **迁移 SQL 的唯一来源是 `src/lyra/db/sql/`。** 此前与之逐字节对照的 TypeScript
   源文件（`../server/src/infrastructure/db/`）已随迁移删除，那条对照测试也一并移除；
   现在由 `tests/test_migrations.py` 断言每条声明的迁移都有非空 SQL 文件，并用冻结夹具
   验证"能打开既有 schema 15 库"。
