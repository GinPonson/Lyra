import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { access, mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

async function availablePort(): Promise<number> {
  const listener = createServer();
  listener.listen(0, "127.0.0.1");
  await once(listener, "listening");
  const address = listener.address();
  assert.ok(address !== null && typeof address !== "string");
  await new Promise<void>((resolve, reject) => listener.close((error) => error ? reject(error) : resolve()));
  return address.port;
}

async function startProcess(overrides: NodeJS.ProcessEnv = {}) {
  const directory = await mkdtemp(join(tmpdir(), "lyra-startup-"));
  const port = await availablePort();
  const databasePath = join(directory, "ledger.duckdb");
  const child = spawn(process.execPath, [fileURLToPath(new URL("../src/main.js", import.meta.url))], {
    // Do not inherit real provider credentials or local deployment configuration.
    env: {
      PATH: process.env.PATH,
      LYRA_HOST: "127.0.0.1",
      LYRA_PORT: String(port),
      LYRA_DATABASE_PATH: databasePath,
      LYRA_IMPORT_STAGING_DIR: join(directory, "imports"),
      LYRA_SECRET_MASTER_KEY: "0".repeat(64),
      ...overrides,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (data: Buffer) => { stderr += data.toString(); });
  child.stdout.resume();
  const exited = once(child, "exit");
  const deadline = setTimeout(() => child.kill("SIGKILL"), 20_000);
  deadline.unref();
  const baseUrl = `http://127.0.0.1:${port}`;
  return {
    child,
    exited,
    databasePath,
    baseUrl,
    stderr: () => stderr,
    async ready() {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        if (child.exitCode !== null) throw new Error(`Server exited before listening: ${stderr}`);
        try {
          const response = await fetch(`${baseUrl}/v1/system/info`, { signal: AbortSignal.timeout(500) });
          if (response.ok) return response;
        } catch {
          // The process is still starting; retry only the local discovery endpoint.
        }
        await delay(25);
      }
      throw new Error("Local Lyra process did not become ready");
    },
    async close() {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
      const force = setTimeout(() => child.kill("SIGKILL"), 2_000);
      force.unref();
      try { await exited; } finally {
        clearTimeout(deadline);
        clearTimeout(force);
        await rm(directory, { recursive: true, force: true });
      }
    },
  };
}

describe("real server startup", () => {
  it("boots schema13/protocol2 with Genkit unavailable and keeps OAuth-only enrollment closed until configured", async () => {
    const server = await startProcess();
    try {
      const info = await (await server.ready()).json() as Record<string, unknown>;
      assert.equal(info.protocol_version, 2);
      assert.equal(info.schema_version, 13);
      const headers = { "x-lyra-protocol-version": "2", "content-type": "application/json" };
      const health = await fetch(`${server.baseUrl}/v1/health`);
      assert.equal(health.status, 200);
      assert.equal((await health.json() as Record<string, unknown>).status, "degraded");
      const login = await fetch(`${server.baseUrl}/v1/auth/google/start`, {
        method: "POST", headers,
        body: JSON.stringify({ displayName: "Smoke", publicKeyPem: "unused", codeChallenge: "a".repeat(43), codeChallengeMethod: "S256" }),
      });
      assert.equal(login.status, 503);
      assert.equal((await login.json() as Record<string, unknown>).code, "AUTH_NOT_CONFIGURED");
      for (const path of ["/v1/devices/pairing-codes", "/v1/devices/pair", "/mcp"]) {
        assert.equal((await fetch(`${server.baseUrl}${path}`, { method: "POST", headers, body: "{}" })).status, 404);
      }
      assert.equal((await fetch(`${server.baseUrl}/v1/ledger/summary?scope=all`, { headers })).status, 401);
    } finally {
      await server.close();
    }
  });

  it("rejects partial Google login configuration before creating or migrating the database", async () => {
    const server = await startProcess({ LYRA_OWNER_EMAIL: "owner@example.com" });
    try {
      const [code] = await server.exited;
      assert.equal(code, 1);
      assert.match(server.stderr(), /server startup failed/);
      assert.equal(server.stderr().includes("owner@example.com"), false);
      await assert.rejects(access(server.databasePath), { code: "ENOENT" });
    } finally {
      await server.close();
    }
  });
});
