"""当前 schema 版本常量。

**单独一个模块是为了让它可被无依赖地导入。**

这个常量原本定义在 :mod:`lyra.db.migrations` 里，而那个模块 import 了 ``duckdb``。
于是任何"只想读一下版本号"的调用方都被迫先有完整的服务端依赖——实测踩到的是
``scripts/verify_running_service.py``：它在宿主机上以系统 python 运行（容器里才有
``duckdb``），一旦 import 迁移模块就 ``ModuleNotFoundError: No module named 'duckdb'``，
而**自检脚本恰恰要在"服务是否真的可用"这个问题上给出答案**，不能反过来依赖
"这台机器装没装 venv"。

因此：常量放这里（纯标准库），:mod:`lyra.db.migrations` 继续从本模块导入并再导出，
既有调用方无需改动。
"""

from __future__ import annotations

# 当前服务端代码能够迁移并读取的最新 DuckDB schema 版本。
CURRENT_SCHEMA_VERSION = 16
