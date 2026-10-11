import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { access, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const PUBLIC_ORIGIN = "https://lyra.ginponson.net";
const AI_MISSING = {
  LYRA_GENKIT_BASE_URL: undefined,
  LYRA_GENKIT_MODEL: undefined,
  LYRA_GENKIT_API_KEY: undefined,
};
const GOOGLE_MISSING = {
  LYRA_LOGIN_GOOGLE_CLIENT_ID: undefined,
  LYRA_LOGIN_GOOGLE_CLIENT_SECRET: undefined,
  LYRA_LOGIN_GOOGLE_REDIRECT_URI: undefined,
  LYRA_OWNER_EMAIL: undefined,
  LYRA_OWNER_GOOGLE_SUBJECT: undefined,
};

async function checkConfiguration(options: {
  overrides?: NodeJS.ProcessEnv;
  args?: readonly string[];
  existingDatabase?: boolean;
} = {}): Promise<{ code: number; stdout: string; stderr: string; output: string }> {
  const directory = await mkdtemp(join(tmpdir(), "lyra-config-check-"));
  const stateDirectory = join(directory, "state");
  const databasePath = join(stateDirectory, "ledger.duckdb");
  const stagingPath = join(stateDirectory, "imports");
  const networkMarker = join(directory, "unexpected-network");
  const networkGuard = join(directory, "guard-network.mjs");
  const databaseBytes = Buffer.concat([Buffer.from("existing-ledger-must-not-be-opened\0"), randomBytes(128)]);
  const validEnvironment: NodeJS.ProcessEnv = {
    PATH: process.env.PATH,
    NODE_ENV: "production",
    LYRA_HOST: "0.0.0.0",
    LYRA_PORT: "3141",
    LYRA_DATABASE_PATH: databasePath,
    LYRA_IMPORT_STAGING_DIR: stagingPath,
    LYRA_SECRET_MASTER_KEY: randomBytes(32).toString("hex"),
    LYRA_LOGIN_GOOGLE_CLIENT_ID: "987654321-lyra.apps.googleusercontent.com",
    LYRA_LOGIN_GOOGLE_CLIENT_SECRET: "google-client-secret-must-stay-private",
    LYRA_LOGIN_GOOGLE_REDIRECT_URI: `${PUBLIC_ORIGIN}/v1/auth/google/callback`,
    LYRA_OWNER_EMAIL: "lyra-owner@ginponson.net",
    LYRA_PUBLIC_ORIGIN: PUBLIC_ORIGIN,
    LYRA_GENKIT_BASE_URL: "http://127.0.0.1:9/v1",
    LYRA_GENKIT_MODEL: "lyra/finance-model",
    LYRA_GENKIT_API_KEY: "sk-lyra-model-secret-must-stay-private",
  };
  const environment = { ...validEnvironment, ...options.overrides };
  try {
    // A complete .env must never repair missing values in the actual process environment.
    await writeFile(join(directory, ".env"), Object.entries(validEnvironment)
      .filter(([name, entry]) => name.startsWith("LYRA_") && entry !== undefined)
      .map(([name, entry]) => `${name}=${entry}`).join("\n"));
    // A preloaded guard catches both fetch and native HTTP(S)/TCP attempts, without real credentials.
    await writeFile(networkGuard, `
      import { writeFileSync } from "node:fs";
      import { Socket } from "node:net";
      const deny = () => {
        writeFileSync(${JSON.stringify(networkMarker)}, "network-attempt");
        throw new Error("Configuration checks must not request the network");
      };
      globalThis.fetch = deny;
      Socket.prototype.connect = deny;
    `);
    if (options.existingDatabase) {
      await mkdir(stateDirectory);
      await writeFile(databasePath, databaseBytes);
    }
    const result = await new Promise<{ code: number; stdout: string; stderr: string }>((resolve, reject) => {
      execFile(process.execPath, [
        "--import", pathToFileURL(networkGuard).href,
        fileURLToPath(new URL("../src/cli/check-config.js", import.meta.url)),
        ...(options.args ?? []),
      ], { cwd: directory, env: environment, timeout: 15_000, maxBuffer: 64 * 1024 }, (error, stdout, stderr) => {
        if (error) {
          if (typeof error.code !== "number" || error.killed || error.signal) {
            reject(error);
            return;
          }
          resolve({ code: error.code, stdout, stderr });
          return;
        }
        resolve({ code: 0, stdout, stderr });
      });
    });
    const output = result.stdout + result.stderr;
    for (const [name, entry] of Object.entries(environment)) {
      if (name.startsWith("LYRA_") && entry) {
        assert.equal(output.includes(entry), false, `Configuration value leaked for ${name}`);
      }
    }
    await assert.rejects(access(networkMarker), { code: "ENOENT" });
    await assert.rejects(access(stagingPath), { code: "ENOENT" });
    if (options.existingDatabase) {
      assert.deepEqual(await readFile(databasePath), databaseBytes);
      assert.deepEqual(await readdir(stateDirectory), ["ledger.duckdb"]);
    } else {
      await assert.rejects(access(stateDirectory), { code: "ENOENT" });
    }
    assert.deepEqual((await readdir(directory)).sort(), options.existingDatabase
      ? [".env", "guard-network.mjs", "state"]
      : [".env", "guard-network.mjs"]);
    return { ...result, output };
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

/**
 * 成功路径要求配置 CLI 自身不向 stderr 输出任何诊断。
 *
 * Node 会为第三方依赖（genkit → compat-oai → openai → node-fetch@2 →
 * whatwg-url@5 → 内建 punycode）打印 `DEP0040` 之类的运行时警告，这属于
 * 运行环境噪声而非 CLI 输出，把它算作失败只会让断言随 Node 版本漂移。
 */
function cliStderr(stderr: string): string {
  return stderr
    .split("\n")
    .filter((line) => !/^\(node:\d+\)\s/u.test(line) && !/^\(Use `node --trace/u.test(line))
    .join("\n")
    .trim();
}

describe("deployment configuration CLI", () => {
  it("checks a complete deployment in a real child process without database, staging or network side effects", async () => {
    const result = await checkConfiguration({ args: ["--require-ai"] });
    assert.equal(result.code, 0, result.output);
    assert.equal(cliStderr(result.stderr), "");
    assert.match(result.stdout, /\[PASS\] 基础环境与主密钥/u);
    assert.match(result.stdout, /\[PASS\] Google 登录与 owner/u);
    assert.match(result.stdout, /\[PASS\] 公开 HTTPS origin 与 Google 回调/u);
    assert.match(result.stdout, /\[PASS\] Genkit 配置/u);
    assert.match(result.stdout, /\[PASS\] 部署配置检查/u);
  });

  it("does not open, migrate or overwrite an existing database during successful checks", async () => {
    const result = await checkConfiguration({ existingDatabase: true });
    assert.equal(result.code, 0, result.output);
  });

  it("accepts Google subject ownership and an omitted optional public origin", async () => {
    const result = await checkConfiguration({ overrides: {
      LYRA_OWNER_EMAIL: undefined,
      LYRA_OWNER_GOOGLE_SUBJECT: "102938475601928374650",
      LYRA_PUBLIC_ORIGIN: undefined,
    } });
    assert.equal(result.code, 0, result.output);
    assert.match(result.stdout, /\[SKIP\].*LYRA_PUBLIC_ORIGIN/u);
  });

  it("explicitly permits ledger mode only when all AI values are empty, unless AI is required", async () => {
    for (const overrides of [AI_MISSING, {
      LYRA_GENKIT_BASE_URL: "",
      LYRA_GENKIT_MODEL: "",
      LYRA_GENKIT_API_KEY: "",
    }]) {
      const result = await checkConfiguration({ overrides });
      assert.equal(result.code, 0, result.output);
      assert.match(result.stdout, /账本模式，AI 未启用/u);
    }
    const required = await checkConfiguration({ overrides: AI_MISSING, args: ["--require-ai"] });
    assert.equal(required.code, 1);
    assert.match(required.stderr, /LYRA_GENKIT_BASE_URL.*LYRA_GENKIT_MODEL.*LYRA_GENKIT_API_KEY/u);
  });

  it("rejects partial AI configuration instead of silently switching to ledger mode", async () => {
    const result = await checkConfiguration({ overrides: { LYRA_GENKIT_API_KEY: undefined } });
    assert.equal(result.code, 1);
    assert.match(result.stderr, /Genkit 配置: LYRA_GENKIT_API_KEY/u);
    assert.doesNotMatch(result.stdout, /账本模式/u);
  });

  it("requires Google login and private owner configuration from process.env despite a complete .env file", async () => {
    const noLogin = await checkConfiguration({ overrides: GOOGLE_MISSING });
    assert.equal(noLogin.code, 1);
    assert.match(noLogin.stderr, /LYRA_LOGIN_GOOGLE_CLIENT_ID/u);
    assert.match(noLogin.stderr, /LYRA_OWNER_EMAIL/u);
    const noOwner = await checkConfiguration({ overrides: { LYRA_OWNER_EMAIL: undefined } });
    assert.equal(noOwner.code, 1);
    assert.match(noOwner.stderr, /LYRA_OWNER_EMAIL.*LYRA_OWNER_GOOGLE_SUBJECT/u);
    const noSecret = await checkConfiguration({ overrides: { LYRA_LOGIN_GOOGLE_CLIENT_SECRET: undefined } });
    assert.equal(noSecret.code, 1);
    assert.match(noSecret.stderr, /LYRA_LOGIN_GOOGLE_CLIENT_SECRET/u);
  });

  it("applies the startup schema, Google owner validator and Genkit bounds without exposing rejected input", async () => {
    for (const [name, entry] of [
      ["LYRA_PORT", "port-invalid-and-private"],
      ["LYRA_DATABASE_PATH", ""],
      ["LYRA_SECRET_MASTER_KEY", "master-key-invalid-and-private"],
      ["LYRA_OWNER_EMAIL", "malformed-owner-and-private"],
      ["LYRA_OWNER_GOOGLE_SUBJECT", "private subject with spaces"],
      ["LYRA_GENKIT_BASE_URL", "https://user:private-password@model.ginponson.net/v1"],
      ["LYRA_AGENT_MAX_TURNS", "33"],
      ["LYRA_AGENT_TURN_TIMEOUT_MS", "999"],
    ] as const) {
      const result = await checkConfiguration({ overrides: { [name]: entry } });
      assert.equal(result.code, 1, name);
      assert.ok(result.stderr.includes(name), name);
    }
    const invalidLedgerMode = await checkConfiguration({ overrides: { ...AI_MISSING, LYRA_AGENT_MAX_TURNS: "33" } });
    assert.equal(invalidLedgerMode.code, 1);
    assert.match(invalidLedgerMode.stderr, /LYRA_AGENT_MAX_TURNS/u);
  });

  it("rejects example master keys, OAuth credentials, owner identities and model placeholders", async () => {
    for (const [name, entry] of [
      ["LYRA_SECRET_MASTER_KEY", "0".repeat(64)],
      ["LYRA_SECRET_MASTER_KEY", "0123456789abcdef".repeat(4)],
      ["LYRA_LOGIN_GOOGLE_CLIENT_ID", "replace-with-google-web-client-id"],
      ["LYRA_LOGIN_GOOGLE_CLIENT_SECRET", "replace-with-google-web-client-secret"],
      ["LYRA_LOGIN_GOOGLE_REDIRECT_URI", "https://lyra.example.com/v1/auth/google/callback"],
      ["LYRA_OWNER_EMAIL", "your-google-account@example.com"],
      ["LYRA_OWNER_GOOGLE_SUBJECT", "replace-with-your-google-subject"],
      ["LYRA_GENKIT_BASE_URL", "https://your-model-provider.example/v1"],
      ["LYRA_GENKIT_MODEL", "your-model-id"],
      ["LYRA_GENKIT_API_KEY", "replace-with-provider-api-key"],
    ] as const) {
      const result = await checkConfiguration({ overrides: { [name]: entry } });
      assert.equal(result.code, 1, name);
      assert.ok(result.stderr.includes(name), name);
    }
  });

  it("accepts only a canonical HTTPS origin and a byte-matching Google callback", async () => {
    for (const origin of [
      "",
      "http://lyra.ginponson.net",
      `${PUBLIC_ORIGIN}/`,
      `${PUBLIC_ORIGIN}/app`,
      "https://user:private-password@lyra.ginponson.net",
      `${PUBLIC_ORIGIN}?key=private-query`,
      `${PUBLIC_ORIGIN}#private-fragment`,
      "https://lyra.example.com",
      "https://lyra.example",
      "https://example",
    ]) {
      const result = await checkConfiguration({ overrides: { LYRA_PUBLIC_ORIGIN: origin } });
      assert.equal(result.code, 1, "invalid public origin");
      assert.match(result.stderr, /LYRA_PUBLIC_ORIGIN/u);
    }
    const mismatch = await checkConfiguration({ overrides: {
      LYRA_LOGIN_GOOGLE_REDIRECT_URI: "https://another.ginponson.net/v1/auth/google/callback",
    } });
    assert.equal(mismatch.code, 1);
    assert.match(mismatch.stderr, /LYRA_PUBLIC_ORIGIN.*LYRA_LOGIN_GOOGLE_REDIRECT_URI/u);
    for (const callback of [
      "http://localhost/v1/auth/google/callback",
      `${PUBLIC_ORIGIN}/v1/auth/google/wrong-path`,
      `${PUBLIC_ORIGIN}/v1/auth/google/callback?key=callback-private`,
      `${PUBLIC_ORIGIN}/v1/auth/google/callback?`,
      `${PUBLIC_ORIGIN}/app/../v1/auth/google/callback`,
    ]) {
      const result = await checkConfiguration({ overrides: { LYRA_LOGIN_GOOGLE_REDIRECT_URI: callback } });
      assert.equal(result.code, 1, "invalid Google callback");
      assert.match(result.stderr, /LYRA_LOGIN_GOOGLE_REDIRECT_URI/u);
    }
  });

  it("accepts loopback HTTP only when no public origin is configured, matching runtime behaviour", async () => {
    // 运行时（GoogleLoginClient）允许本机开发使用 loopback HTTP 回调；预检必须
    // 使用同一判定，否则一个服务端能启动的配置会被预检误判为失败。
    const loopback = await checkConfiguration({ overrides: {
      LYRA_PUBLIC_ORIGIN: undefined,
      LYRA_LOGIN_GOOGLE_REDIRECT_URI: "http://127.0.0.1:3141/v1/auth/google/callback",
    } });
    assert.equal(loopback.code, 0, loopback.output);
    assert.match(loopback.stdout, /\[SKIP\].*LYRA_PUBLIC_ORIGIN/u);

    // 非 loopback 的明文回调仍然必须被拒绝。
    const remotePlaintext = await checkConfiguration({ overrides: {
      LYRA_PUBLIC_ORIGIN: undefined,
      LYRA_LOGIN_GOOGLE_REDIRECT_URI: "http://lyra.ginponson.net/v1/auth/google/callback",
    } });
    assert.equal(remotePlaintext.code, 1);
    assert.match(remotePlaintext.stderr, /LYRA_LOGIN_GOOGLE_REDIRECT_URI/u);
  });

  it("provides help and rejects unknown arguments without reflecting their values", async () => {
    const help = await checkConfiguration({ args: ["--help"] });
    assert.equal(help.code, 0);
    assert.match(help.stdout, /--require-ai/u);
    const invalid = await checkConfiguration({ args: ["--private-argument-value"] });
    assert.equal(invalid.code, 2);
    assert.doesNotMatch(invalid.output, /private-argument-value/u);
  });

  it("validates optional market routing offline and never exposes provider credentials", async () => {
    const valid = await checkConfiguration({ overrides: {
      LYRA_MARKET_STOCK_PROVIDERS: "alpaca,marketdata",
      LYRA_MARKET_OPTION_PROVIDERS: "marketdata",
      LYRA_ALPACA_API_KEY: "alpaca-private-key-value",
      LYRA_ALPACA_SECRET_KEY: "alpaca-private-secret-value",
      LYRA_MARKETDATA_API_TOKEN: "market-private-token-value",
    } });
    assert.equal(valid.code, 0, valid.output);
    assert.match(valid.stdout, /\[PASS\] 行情数据源配置/u);
    for (const overrides of [
      { LYRA_MARKET_STOCK_PROVIDERS: "unknown-private-provider" },
      { LYRA_MARKET_STOCK_PROVIDERS: "alpaca" },
      { LYRA_ALPACA_API_KEY: "partial-private-key" },
      { LYRA_MARKET_OPTION_PROVIDERS: "marketdata" },
      { LYRA_MARKETDATA_API_TOKEN: "replace-with-marketdata-api-token" },
    ]) {
      const invalid = await checkConfiguration({ overrides });
      assert.equal(invalid.code, 1);
      assert.match(invalid.stderr, /行情数据源配置/u);
    }
  });
});
