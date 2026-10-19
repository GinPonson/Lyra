# Lyra 服务端镜像。
#
# **Python 版**。镜像里不含 TypeScript 工具链：服务端已完全迁移，保留 Node 只会让镜像
# 变大、攻击面变宽，并留下一条已无人使用的启动路径。

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_VERSION=0.9

# ---------------------------------------------------------------------------
# 构建阶段：装依赖（含在镜像里编译 wheel 所需的最小工具链）
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

ARG UV_VERSION
ENV UV_CACHE_DIR=/tmp/uv-cache \
    UV_PYTHON_INSTALL_DIR=/usr/local \
    UV_PYTHON=python3.12 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir "uv==${UV_VERSION}.*"

WORKDIR /app

# **先只复制依赖清单**：源码改动不会让依赖层失效，重建时这一层命中缓存。
COPY server/pyproject.toml server/uv.lock ./

# 运行期不需要开发依赖（pytest / ruff）。
RUN uv sync --frozen --no-install-project --no-dev

COPY server/src ./src
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# 运行阶段
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    LYRA_HOST=0.0.0.0 \
    LYRA_PORT=3141 \
    LYRA_DATABASE_PATH=/var/lib/lyra/lyra.duckdb \
    LYRA_IMPORT_STAGING_DIR=/var/lib/lyra/import-staging

WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/src ./src
COPY server/pyproject.toml ./pyproject.toml
COPY server/scripts/verify_running_service.py ./scripts/verify_running_service.py

# 账本目录归运行用户所有：**DuckDB 需要写权限来建库与写 WAL**，
# 属主不对会在首次启动时才暴露，而那时容器已经起来了。
RUN useradd --create-home --uid 10001 lyra \
    && mkdir -p /var/lib/lyra \
    && chown -R lyra:lyra /var/lib/lyra /app
USER lyra

EXPOSE 3141

# 健康检查直接打端点，而不是跑一个脚本：**镜像里少一个可执行文件就少一处失效点**。
# start-period 覆盖首次启动的 schema 迁移；期间失败不计入 retries，
# 否则编排器会在迁移尚未结束时就把服务判为不健康。
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=4 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3141/v1/health', timeout=4).status == 200 else 1)"

# 具名入口，与本地开发一致（`lyra-serve` = `lyra.main:run`）。
CMD ["lyra-serve"]
