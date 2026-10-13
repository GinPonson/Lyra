# Lyra 服务端（Python/FastAPI）

这是 TypeScript 服务端（`../server`）的**全量迁移目标**。当前进度与验收标准见
[迁移计划](../docs/plans/lyra-python-migration-20261012.md)。

> **当前状态：M3 领域层全部完成；M5（认证）进行中。**
> 服务能接管 TypeScript 创建的 schema 15 账本、暴露 `/v1/health` 与 `/v1/system/info`，
> 与 TypeScript 的 AES-256-GCM 密文**双向互通**；**账本 CSV 解析、FIFO 重放与
> 16 条分析命令均已与 oracle 逐字段一致**（19 份账本共 2771 个字段比对）；
> 设备认证模块已完成，三种登录来源（Google OIDC、LINUX DO、GitHub OAuth）均已实现。
> **尚未接管任何对外业务能力**，因此现在还不能面向 Flutter 提供服务——客户端仍
> 连接 TypeScript 服务端。
>
> 切换是**原子**的：DuckDB 同一时刻只允许一个进程打开账本文件，所以 TS 与 Python
> 不能同时服务同一个账本。M9 才是一次性切换。

## 运行

需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。环境变量与 TypeScript 版
**同名同约束**（`LYRA_HOST` / `LYRA_PORT` / `LYRA_DATABASE_PATH` /
`LYRA_IMPORT_STAGING_DIR` / `LYRA_SECRET_MASTER_KEY` / `LYRA_GOOGLE_*`），
因此同一份 `.env` 可以直接驱动两侧。

```bash
uv run pytest          # 测试

export LYRA_DATABASE_PATH=/var/lib/lyra/lyra.duckdb
export LYRA_IMPORT_STAGING_DIR=/tmp/lyra-import-staging
export LYRA_SECRET_MASTER_KEY=<64 位 hex>
uv run python -m lyra.main
```

默认监听 `127.0.0.1:3141`，与 TypeScript 版一致。

## 目录

```text
src/lyra/
├── main.py           应用工厂与 uvicorn 入口
├── config.py         环境校验（字段与 TypeScript 版一一对应）
├── protocol.py       协议版本与 /v1/system/info 元数据
├── db/
│   ├── migrations.py 15 条迁移清单与执行语义
│   ├── connection.py 打开账本、溢出目录、健康探测
│   └── sql/          迁移 SQL 副本（测试校验与 TypeScript 源文件逐字节一致）
├── security/
│   └── secret_cipher.py  AES-256-GCM 秘密加密（与 TS 密文双向互通）
└── api/              health / system 端点
```

## 迁移期间的两条纪律

1. **不修改 schema（保持 15）、不修改客户端协议（保持 2）。**
2. **迁移 SQL 的唯一来源仍是 `../server/src/infrastructure/db/`。** 本目录下的
   副本由 `tests/test_migrations.py` 逐字节校验；M9 删除 TypeScript 服务端后，
   副本转为唯一来源，该校验自动跳过。
