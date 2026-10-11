import process from "node:process";

import { createGenkitRuntimeFromEnvironment } from "../infrastructure/agents/genkit-runtime.js";
import {
  GOOGLE_LOGIN_CALLBACK_PATH,
  loadGoogleLoginConfig,
} from "../infrastructure/auth/google-login-client.js";
import { environmentSchema } from "../infrastructure/config/server-environment.js";

const LOGIN_REQUIRED = [
  "LYRA_LOGIN_GOOGLE_CLIENT_ID",
  "LYRA_LOGIN_GOOGLE_CLIENT_SECRET",
  "LYRA_LOGIN_GOOGLE_REDIRECT_URI",
] as const;
const OWNER_VARIABLES = ["LYRA_OWNER_EMAIL", "LYRA_OWNER_GOOGLE_SUBJECT"] as const;
const LOGIN_VARIABLES = [...LOGIN_REQUIRED, ...OWNER_VARIABLES] as const;
const AI_VARIABLES = ["LYRA_GENKIT_BASE_URL", "LYRA_GENKIT_MODEL", "LYRA_GENKIT_API_KEY"] as const;
const AI_LIMIT_VARIABLES = ["LYRA_AGENT_TURN_TIMEOUT_MS", "LYRA_AGENT_MAX_TURNS"] as const;

type EnvironmentVariable = keyof typeof environmentSchema.shape
  | typeof LOGIN_VARIABLES[number]
  | typeof AI_VARIABLES[number]
  | typeof AI_LIMIT_VARIABLES[number]
  | "LYRA_PUBLIC_ORIGIN";

/** 输出只能来自固定文案与允许的变量名，绝不转发 URL/Zod/供应商的错误消息。 */
function report(label: string, errors: ReadonlySet<EnvironmentVariable>): boolean {
  if (errors.size > 0) {
    process.stderr.write(`[FAIL] ${label}: ${[...errors].join(", ")}\n`);
    return false;
  }
  process.stdout.write(`[PASS] ${label}\n`);
  return true;
}

function value(environment: NodeJS.ProcessEnv, name: EnvironmentVariable): string {
  return environment[name]?.trim() ?? "";
}

function isPlaceholder(input: string): boolean {
  return /^(?:change[-_\s]?me|replace[-_\s]?me|placeholder|example|todo|tbd)$/iu.test(input)
    || /(?:^|[-_\s])(?:replace[-_\s]with|your[-_\s]|change[-_\s]?me|placeholder(?:[-_\s]|$)|example[-_.\s])/iu.test(input)
    || /^(?:<[^>]+>|\$\{[^}]+\}|(?:sk-)?[x*\.]{3,})$/iu.test(input);
}

function isPlaceholderHostname(hostname: string): boolean {
  return hostname.toLowerCase().replace(/\.$/u, "").split(".")
    .some((part) => part === "example" || part === "invalid" || isPlaceholder(part));
}

function parseUrl(input: string): URL | null {
  try { return new URL(input); } catch { return null; }
}

/** 拒绝常见示例密钥；不声称能从一个字符串证明其随机性。 */
function isExampleMasterKey(key: string): boolean {
  if (!/^[a-f\d]{64}$/iu.test(key)) return false;
  const normalized = key.toLowerCase();
  for (let size = 1; size <= 32; size *= 2) {
    if (normalized.slice(0, size).repeat(64 / size) === normalized) return true;
  }
  return normalized === Array.from({ length: 32 }, (_, index) => index.toString(16).padStart(2, "0")).join("");
}

/** 仅提取允许的变量名；某些底层 URL 错误可能携带完整输入，不能打印。 */
function namedVariables(
  error: unknown,
  allowed: readonly EnvironmentVariable[],
): EnvironmentVariable[] {
  return error instanceof Error ? allowed.filter((name) => error.message.includes(name)) : [];
}

function checkBaseEnvironment(environment: NodeJS.ProcessEnv): boolean {
  const errors = new Set<EnvironmentVariable>();
  const result = environmentSchema.safeParse(environment);
  if (!result.success) {
    for (const issue of result.error.issues) {
      const name = issue.path[0];
      if (typeof name === "string" && Object.hasOwn(environmentSchema.shape, name)) {
        errors.add(name as keyof typeof environmentSchema.shape);
      }
    }
  }
  if (isExampleMasterKey(environment.LYRA_SECRET_MASTER_KEY ?? "")) {
    errors.add("LYRA_SECRET_MASTER_KEY");
  }
  return report("基础环境与主密钥", errors);
}

function checkGoogleLogin(environment: NodeJS.ProcessEnv): boolean {
  const errors = new Set<EnvironmentVariable>();
  for (const name of LOGIN_REQUIRED) {
    if (!value(environment, name)) errors.add(name);
  }
  if (OWNER_VARIABLES.every((name) => !value(environment, name))) {
    for (const name of OWNER_VARIABLES) errors.add(name);
  }
  for (const name of LOGIN_VARIABLES) {
    if (isPlaceholder(value(environment, name))) errors.add(name);
  }
  const ownerEmail = value(environment, "LYRA_OWNER_EMAIL");
  if (ownerEmail && isPlaceholderHostname(ownerEmail.slice(ownerEmail.lastIndexOf("@") + 1))) {
    errors.add("LYRA_OWNER_EMAIL");
  }
  try {
    // 与正式启动相同的 Google 配置校验；不实例化 OAuth client 或登录服务。
    loadGoogleLoginConfig(environment);
  } catch (error: unknown) {
    const names = namedVariables(error, LOGIN_VARIABLES);
    for (const name of names.length > 0 ? names : ["LYRA_LOGIN_GOOGLE_REDIRECT_URI" as const]) {
      errors.add(name);
    }
  }
  const callback = value(environment, "LYRA_LOGIN_GOOGLE_REDIRECT_URI");
  const url = parseUrl(callback);
  if (url === null || url.protocol !== "https:" || isPlaceholderHostname(url.hostname)
    || callback !== `${url.origin}${GOOGLE_LOGIN_CALLBACK_PATH}`) {
    errors.add("LYRA_LOGIN_GOOGLE_REDIRECT_URI");
  }
  return report("Google 登录与 owner", errors);
}

function checkPublicOrigin(environment: NodeJS.ProcessEnv): boolean {
  if (environment.LYRA_PUBLIC_ORIGIN === undefined) {
    process.stdout.write("[SKIP] 公开 HTTPS origin（未设置 LYRA_PUBLIC_ORIGIN）\n");
    return true;
  }
  const errors = new Set<EnvironmentVariable>();
  const origin = value(environment, "LYRA_PUBLIC_ORIGIN");
  const url = parseUrl(origin);
  if (url === null || url.protocol !== "https:" || origin !== url.origin
    || isPlaceholderHostname(url.hostname)) {
    errors.add("LYRA_PUBLIC_ORIGIN");
  } else if (value(environment, "LYRA_LOGIN_GOOGLE_REDIRECT_URI") !== `${origin}${GOOGLE_LOGIN_CALLBACK_PATH}`) {
    errors.add("LYRA_PUBLIC_ORIGIN");
    errors.add("LYRA_LOGIN_GOOGLE_REDIRECT_URI");
  }
  return report("公开 HTTPS origin 与 Google 回调", errors);
}

function checkGenkit(environment: NodeJS.ProcessEnv, requireAI: boolean): boolean {
  const errors = new Set<EnvironmentVariable>();
  const missing = AI_VARIABLES.filter((name) => !value(environment, name));
  const ledgerOnly = missing.length === AI_VARIABLES.length;
  if (missing.length > 0 && (!ledgerOnly || requireAI)) {
    for (const name of missing) errors.add(name);
  }
  for (const name of AI_VARIABLES) {
    if (isPlaceholder(value(environment, name))) errors.add(name);
  }
  const baseUrl = parseUrl(value(environment, "LYRA_GENKIT_BASE_URL"));
  if (baseUrl !== null && isPlaceholderHostname(baseUrl.hostname)) {
    errors.add("LYRA_GENKIT_BASE_URL");
  }
  try {
    // 正式启动的 Genkit 入口还会校验端点、超时和轮次；创建运行时不调用 executor。
    createGenkitRuntimeFromEnvironment(environment);
  } catch (error: unknown) {
    const names = namedVariables(error, [...AI_VARIABLES, ...AI_LIMIT_VARIABLES]);
    if (names.length > 0) {
      for (const name of names) errors.add(name);
    } else if (error instanceof TypeError) {
      errors.add("LYRA_GENKIT_BASE_URL");
    } else {
      const limits = AI_LIMIT_VARIABLES.filter((name) => value(environment, name));
      for (const name of limits.length > 0 ? limits : AI_VARIABLES) errors.add(name);
    }
  }
  return report(ledgerOnly && !requireAI ? "Genkit 配置（账本模式，AI 未启用）" : "Genkit 配置", errors);
}

function run(): number {
  const args = process.argv.slice(2);
  if (args.some((argument) => argument !== "--require-ai" && argument !== "--help")) {
    process.stderr.write("[FAIL] 命令参数；仅支持 --require-ai 与 --help\n");
    return 2;
  }
  if (args.includes("--help")) {
    process.stdout.write("用法：node dist/src/cli/check-config.js [--require-ai]\n"
      + "检查部署环境中的基础配置、Google owner 登录和可选 Genkit 配置；仅读取 process.env。\n"
      + "--require-ai 要求完整 AI 配置，否则 AI 三项全空时允许账本模式。\n");
    return 0;
  }
  // 不读取 .env、不引入 main、不打开数据库，不执行迁移、建账户或外部请求。
  const checks = [
    checkBaseEnvironment(process.env),
    checkGoogleLogin(process.env),
    checkPublicOrigin(process.env),
    checkGenkit(process.env, args.includes("--require-ai")),
  ];
  const passed = checks.every(Boolean);
  (passed ? process.stdout : process.stderr).write(`[${passed ? "PASS" : "FAIL"}] 部署配置检查\n`);
  return passed ? 0 : 1;
}

try {
  process.exitCode = run();
} catch {
  process.stderr.write("[FAIL] 部署配置检查\n");
  process.exitCode = 1;
}
