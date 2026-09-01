import { copyFile, mkdir } from "node:fs/promises";

const sqlFiles = [
  "schema.sql",
  "gmail-schema.sql",
  "auth-schema.sql",
  "audit-schema.sql",
  "agent-schema.sql",
  "access-token-schema.sql",
  "import-preview-schema.sql",
  "reconciliation-reminder-schema.sql",
  "transaction-source-view-schema.sql",
  "idempotency-schema.sql",
  "agent-session-identity-schema.sql",
];

const sourceDirectory = new URL("../src/infrastructure/db/", import.meta.url);
const targetDirectory = new URL("../dist/src/infrastructure/db/", import.meta.url);

// TypeScript 不复制非代码资源；构建阶段显式携带固定迁移清单，避免运行时依赖源码目录。
await mkdir(targetDirectory, { recursive: true });
for (const fileName of sqlFiles) {
  await copyFile(
    new URL(fileName, sourceDirectory),
    new URL(fileName, targetDirectory),
  );
}
