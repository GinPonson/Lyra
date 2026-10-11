import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { copyFile, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

// Tests run from dist/test; exercise the shipped scripts, not compiled copies.
const deployDirectory = fileURLToPath(new URL("../../../deploy/", import.meta.url));
const probeScript = join(deployDirectory, "probe.mjs");
const healthcheckScript = fileURLToPath(new URL("../../scripts/healthcheck.mjs", import.meta.url));
const responseSecret = "private-upstream-body-and-redirect-token";

async function runScript(script: string, args: string[] = [], overrides: NodeJS.ProcessEnv = {}) {
  const child = spawn(process.execPath, [script, ...args], {
    // Never inherit real deployment settings or provider credentials.
    env: { PATH: process.env.PATH, ...overrides },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (data: Buffer) => { stdout += data.toString(); });
  child.stderr.on("data", (data: Buffer) => { stderr += data.toString(); });
  const deadline = setTimeout(() => child.kill("SIGKILL"), 10_000);
  deadline.unref();
  try {
    const [code, signal] = await once(child, "close") as [number | null, NodeJS.Signals | null];
    assert.equal(signal, null, "Deployment script did not finish within its deadline");
    return { code, stdout, stderr };
  } finally {
    clearTimeout(deadline);
  }
}

function assertNoSecret(result: { stdout: string; stderr: string }, secret = responseSecret) {
  assert.equal(result.stdout.includes(secret), false, "stdout must not expose sensitive values");
  assert.equal(result.stderr.includes(secret), false, "stderr must not expose sensitive values");
}

async function initFixture() {
  const directory = await mkdtemp(join(tmpdir(), "lyra-deployment-env-"));
  await Promise.all(["init-env.mjs", ".env.example", "server.env.example"].map((name) =>
    copyFile(join(deployDirectory, name), join(directory, name))));
  return {
    directory,
    script: join(directory, "init-env.mjs"),
    close: () => rm(directory, { recursive: true, force: true }),
  };
}

const serviceInformation = {
  server_version: "deployment-test",
  protocol_version: 2,
  min_supported_protocol_version: 2,
  max_supported_protocol_version: 2,
  schema_version: 15,
  detail: responseSecret,
};
const degradedHealth = {
  status: "degraded",
  process: { status: "available" },
  database: { status: "available" },
  writeQueue: { status: "available" },
  genkit: { status: "unavailable" },
  detail: responseSecret,
};

interface ServiceOptions {
  information?: Record<string, unknown>;
  health?: Record<string, unknown>;
  anonymousLedgerStatus?: number;
  missingProtocolStatus?: number;
  mcpPostStatus?: number;
  redirectPaths?: string[];
}

async function startService(options: ServiceOptions = {}) {
  const requests: string[] = [];
  const server = createServer((request, response) => {
    const path = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    requests.push(path);
    if (options.redirectPaths?.includes(path)) {
      response.writeHead(302, {
        location: `/redirected?token=${responseSecret}`,
        "content-type": "text/plain",
      });
      response.end(responseSecret);
      return;
    }
    let status = 200;
    let body: unknown;
    switch (path) {
      case "/v1/system/info":
        body = options.information ?? serviceInformation;
        break;
      case "/v1/health":
        body = options.health ?? degradedHealth;
        break;
      case "/v1/ledger/accounts":
        status = request.headers["x-lyra-protocol-version"] === "2"
          ? options.anonymousLedgerStatus ?? 401
          : options.missingProtocolStatus ?? 426;
        body = { detail: responseSecret };
        break;
      case "/mcp":
        status = request.method === "POST" ? options.mcpPostStatus ?? 404 : 404;
        body = { detail: responseSecret };
        break;
      case "/redirected":
        // A followed redirect could otherwise look like a successful service.
        body = { ...serviceInformation, ...degradedHealth };
        break;
      default:
        status = 404;
        body = { detail: responseSecret };
    }
    response.writeHead(status, { "content-type": "application/json" });
    response.end(JSON.stringify(body));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address !== null && typeof address !== "string");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    port: String(address.port),
    requests,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
      server.closeAllConnections();
    }),
  };
}

describe("deployment tooling", () => {
  it("creates private environment files with a generated 64-hex master key without printing it", async () => {
    const fixture = await initFixture();
    try {
      const result = await runScript(fixture.script);
      assert.equal(result.code, 0);
      assert.equal(result.stderr, "");
      for (const name of [".env", "server.env"]) {
        assert.equal((await stat(join(fixture.directory, name))).mode & 0o777, 0o600);
      }
      const content = await readFile(join(fixture.directory, "server.env"), "utf8");
      const key = /^LYRA_SECRET_MASTER_KEY=([a-f0-9]{64})$/m.exec(content)?.[1];
      assert.ok(key, "A new deployment needs a generated 256-bit master key");
      assert.doesNotMatch(content, /=GENERATE_ONCE/);
      assertNoSecret(result, key);
    } finally {
      await fixture.close();
    }
  });

  it("leaves existing environment files and the original encryption key unchanged on repeated initialization", async () => {
    const fixture = await initFixture();
    const key = "a1".repeat(32);
    const existing = {
      ".env": "LYRA_DOMAIN=existing.example.test\n# Preserve this local configuration.\n",
      "server.env": `LYRA_SECRET_MASTER_KEY=${key}\nLYRA_GENKIT_API_KEY=$existing-private-key\n`,
    };
    try {
      for (const [name, content] of Object.entries(existing)) {
        await writeFile(join(fixture.directory, name), content, { mode: 0o600 });
      }
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const result = await runScript(fixture.script);
        assert.equal(result.code, 0);
        for (const [name, content] of Object.entries(existing)) {
          assert.equal(await readFile(join(fixture.directory, name), "utf8"), content);
        }
        assertNoSecret(result, key);
        assertNoSecret(result, "$existing-private-key");
      }
    } finally {
      await fixture.close();
    }
  });

  it("accepts degraded optional integrations when the ledger remains available", async () => {
    const service = await startService();
    try {
      const [probe, health] = await Promise.all([
        runScript(probeScript, [service.origin]),
        runScript(healthcheckScript, [], { LYRA_PORT: service.port }),
      ]);
      assert.equal(probe.code, 0);
      assert.match(probe.stdout, /Service checks passed/);
      assert.equal(probe.stderr, "");
      assert.equal(health.code, 0);
      assert.equal(health.stdout, "");
      assert.equal(health.stderr, "");
      assertNoSecret(probe);
      assertNoSecret(health);
    } finally {
      await service.close();
    }
  });

  it("fails both checks for an unavailable database even when health responds HTTP 200", async () => {
    const service = await startService({
      health: { ...degradedHealth, database: { status: "unavailable", detail: responseSecret } },
    });
    try {
      const results = await Promise.all([
        runScript(probeScript, [service.origin]),
        runScript(healthcheckScript, [], { LYRA_PORT: service.port }),
      ]);
      for (const result of results) {
        assert.equal(result.code, 1);
        assert.match(result.stderr, /health/);
        assertNoSecret(result);
      }
    } finally {
      await service.close();
    }
  });

  it("rejects incompatible App protocols and a service that omits the client protocol gate", async () => {
    const cases: Array<{ options: ServiceOptions; failure: RegExp }> = [
      {
        options: { information: {
          ...serviceInformation,
          protocol_version: 3,
          min_supported_protocol_version: 3,
          max_supported_protocol_version: 3,
        } },
        failure: /service information/,
      },
      { options: { missingProtocolStatus: 200 }, failure: /client protocol gate/ },
    ];
    for (const { options, failure } of cases) {
      const service = await startService(options);
      try {
        const result = await runScript(probeScript, [service.origin]);
        assert.equal(result.code, 1);
        assert.match(result.stderr, failure);
        assertNoSecret(result);
      } finally {
        await service.close();
      }
    }
  });

  it("rejects a service that exposes the ledger to an unauthenticated request", async () => {
    const service = await startService({ anonymousLedgerStatus: 200 });
    try {
      const result = await runScript(probeScript, [service.origin]);
      assert.equal(result.code, 1);
      assert.match(result.stderr, /unauthenticated ledger protection/);
      assertNoSecret(result);
    } finally {
      await service.close();
    }
  });

  it("rejects unsafe probe targets before any request without echoing supplied credentials", async () => {
    const service = await startService();
    try {
      const targets = [
        service.origin.replace("http:", "ftp:"),
        service.origin.replace("http://", `http://owner:${responseSecret}@`),
        `${service.origin}/?token=${responseSecret}`,
        `${service.origin}/unexpected-path`,
      ];
      for (const target of targets) {
        const result = await runScript(probeScript, [target]);
        assert.equal(result.code, 2);
        assertNoSecret(result);
      }
      assert.deepEqual(service.requests, []);
    } finally {
      await service.close();
    }
  });

  it("does not follow redirects or disclose redirect targets and response bodies", async () => {
    const service = await startService({ redirectPaths: ["/v1/system/info", "/v1/health"] });
    try {
      const results = await Promise.all([
        runScript(probeScript, [service.origin]),
        runScript(healthcheckScript, [], { LYRA_PORT: service.port }),
      ]);
      for (const result of results) {
        assert.equal(result.code, 1);
        assertNoSecret(result);
      }
      assert.equal(service.requests.includes("/redirected"), false);
    } finally {
      await service.close();
    }
  });

  it("rejects a POST-only MCP endpoint even when GET returns 404 without exposing its response", async () => {
    const service = await startService({ mcpPostStatus: 200 });
    try {
      const result = await runScript(probeScript, [service.origin]);
      assert.equal(result.code, 1);
      assert.match(result.stderr, /removed MCP endpoint/);
      assertNoSecret(result);
    } finally {
      await service.close();
    }
  });
});
