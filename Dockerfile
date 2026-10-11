ARG NODE_IMAGE=node:24-bookworm-slim

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY server/package.json ./server/package.json
RUN npm ci --no-audit --no-fund
COPY server/tsconfig.json ./server/tsconfig.json
COPY server/scripts ./server/scripts
COPY server/src ./server/src
COPY server/test ./server/test
RUN npm run build \
    && npm prune --omit=dev --no-audit --no-fund \
    && mkdir -p server/node_modules

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production \
    LYRA_HOST=0.0.0.0 \
    LYRA_PORT=3141 \
    LYRA_DATABASE_PATH=/var/lib/lyra/lyra.duckdb \
    LYRA_IMPORT_STAGING_DIR=/tmp/lyra-import-staging
WORKDIR /app
COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/server/package.json ./server/package.json
COPY --from=build /app/server/node_modules ./server/node_modules
COPY --from=build /app/server/dist/src ./server/dist/src
COPY --from=build /app/server/scripts/healthcheck.mjs ./server/scripts/healthcheck.mjs
COPY --from=build /app/server/scripts/start-container.sh ./server/scripts/start-container.sh
RUN mkdir -p /var/lib/lyra && chown node:node /var/lib/lyra
USER node
WORKDIR /app/server
RUN node --input-type=module -e 'import { DuckDBInstance } from "@duckdb/node-api"; const db = await DuckDBInstance.create(":memory:"); const connection = await db.connect(); await connection.run("SELECT 1"); connection.closeSync(); db.closeSync();'
WORKDIR /app
EXPOSE 3141
# start-period 覆盖首次启动的 schema 迁移；期间的健康检查失败不计入 retries，
# 否则 `compose up --wait` 会在迁移尚未结束时就把服务判为不健康。
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=4 \
    CMD ["node", "server/scripts/healthcheck.mjs"]
CMD ["/bin/sh", "server/scripts/start-container.sh"]
