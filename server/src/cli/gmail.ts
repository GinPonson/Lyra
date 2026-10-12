import { spawn } from "node:child_process";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import process from "node:process";

import {
  GmailService,
  GmailSyncService,
  WriteCoordinator,
  loadGmailGoogleOAuthConfig,
} from "../application/index.js";
import type { GmailSyncHttpResponse, PublicGmailConnection } from "../application/index.js";
import { ApplicationError } from "../application/app-error.js";
import { environmentSchema } from "../infrastructure/config/server-environment.js";
import { openLyraDatabase } from "../infrastructure/db/database.js";
import { installEnvironmentProxy } from "../infrastructure/net/http-proxy.js";
import { SecretCipher } from "../infrastructure/security/index.js";

/**
 * Gmail 命令行：在没有 App 登录的情况下，于本机完成 Google 授权并把邮件同步进账本。
 *
 * 与 App 走的是同一套应用服务（GmailService / GmailSyncService），因此账户映射、
 * 幂等导入与游标推进规则完全一致；这里只补两件 App 侧尚未实现的事——启动 loopback
 * 回调接收授权码，以及把结果打到终端。
 *
 * 注意：DuckDB 同一时刻只允许一个写进程，运行前需先停掉正式服务。
 */

/**
 * 授权事务在创建与消费时必须使用同一 deviceId（见 consumeGoogleOAuthTransaction）。
 * CLI 不持有设备密钥，用固定标识表示"本机命令行发起"，不参与任何设备鉴权。
 */
const CLI_DEVICE_ID = "lyra-cli";

/** 与 GmailService 的 10 分钟事务 TTL 对齐：事务失效后再等回调已无意义。 */
const CALLBACK_TIMEOUT_MS = 10 * 60 * 1000;

/** 命令行自身的用法错误；与业务错误分开，避免把参数问题伪装成服务端故障。 */
class CliUsageError extends Error {}

/** 回调服务器收到的查询参数。 */
interface CallbackQuery {
  code: string;
  state: string;
}

function usage(): string {
  return [
    "用法：node --env-file=server/.env server/dist/src/cli/gmail.js <命令> [选项]",
    "",
    "命令：",
    "  connect        在本机浏览器完成 Google 授权并保存 Gmail 连接",
    "  connections    列出已保存的 Gmail 连接",
    "  sync           同步邮件并导入账本（默认同步全部连接）",
    "",
    "选项：",
    "  --connection <id>          仅同步指定连接",
    "  --account <uuid>           指定账本账户，用于邮件无法自动映射时",
    "  --rescan-after <ISO 日期>  确认从指定时间重扫（Gmail history 失效后需要）",
    "  --no-open                  只打印授权地址，不自动打开浏览器",
    "  --help                     显示本说明",
    "",
    "运行前请先停止 Lyra 正式服务：DuckDB 同一时刻只允许一个写进程。",
  ].join("\n");
}

/** 解析后的命令行参数；未出现的选项一律为 null。 */
interface CliOptions {
  command: "connect" | "connections" | "sync" | "help";
  connectionId: string | null;
  explicitAccountId: string | null;
  rescanAfterDate: string | null;
  openBrowser: boolean;
}

/**
 * 解析命令行参数。
 *
 * @param argv - 不含 node 与脚本路径的参数列表。
 * @returns 结构化选项；`--help` 与缺省命令都映射为 help。
 * @throws CliUsageError 未知命令、未知选项、缺少取值或取值非法时抛出。
 */
function parseArguments(argv: readonly string[]): CliOptions {
  const options: CliOptions = {
    command: argv.length === 0 || argv[0] === "--help" ? "help" : toCommand(argv[0]),
    connectionId: null,
    explicitAccountId: null,
    rescanAfterDate: null,
    openBrowser: true,
  };
  for (let index = 1; index < argv.length; index += 1) {
    const argument = argv[index];
    switch (argument) {
      case "--help":
        return { ...options, command: "help" };
      case "--no-open":
        options.openBrowser = false;
        break;
      case "--connection":
        options.connectionId = requireValue(argv, (index += 1), argument);
        break;
      case "--account":
        options.explicitAccountId = requireValue(argv, (index += 1), argument);
        break;
      case "--rescan-after":
        options.rescanAfterDate = requireValue(argv, (index += 1), argument);
        break;
      default:
        throw new CliUsageError(`未知选项 ${argument}`);
    }
  }
  if (options.command === "connect" && (options.connectionId !== null
    || options.explicitAccountId !== null || options.rescanAfterDate !== null)) {
    throw new CliUsageError("connect 不接受连接或同步相关选项");
  }
  return options;
}

function toCommand(input: string | undefined): CliOptions["command"] {
  switch (input) {
    case "connect":
    case "connections":
    case "sync":
    case "help":
      return input;
    default:
      throw new CliUsageError(`未知命令 ${input ?? ""}`);
  }
}

function requireValue(argv: readonly string[], index: number, option: string): string {
  const value = argv[index];
  if (value === undefined || value.startsWith("--")) {
    throw new CliUsageError(`${option} 缺少取值`);
  }
  return value;
}

/**
 * 校验基础环境变量并解出数据库路径与主密钥。
 *
 * 复用正式入口的同一 schema，保证命令行与服务端读到的是同一套配置语义。
 *
 * @returns 解析后的环境配置。
 * @throws CliUsageError 缺少必需变量或格式非法时抛出。
 */
function requireEnvironment(): ReturnType<typeof environmentSchema.parse> {
  const parsed = environmentSchema.safeParse(process.env);
  if (!parsed.success) {
    const names = [...new Set(parsed.error.issues
      .map((issue) => issue.path.join("."))
      .filter((name) => name.length > 0))];
    throw new CliUsageError(
      `环境变量不完整或格式非法：${names.join(", ")}；请用 --env-file 指向 server/.env`,
    );
  }
  return parsed.data;
}

/** 本机回调地址的监听参数。 */
interface LoopbackTarget {
  host: string;
  port: number;
  path: string;
}

/**
 * 从回调地址解出本机监听参数。
 *
 * 只接受 loopback 主机：命令行无法接收公网地址上的回调，这类配置必须显式报错，
 * 而不是启动一个永远收不到授权的等待。
 *
 * @param redirectUri - LYRA_GOOGLE_REDIRECT_URI 的值。
 * @returns 监听主机、端口与路径；非 loopback 或无法解析时返回 null。
 */
function readLoopbackTarget(redirectUri: string): LoopbackTarget | null {
  let url: URL;
  try {
    url = new URL(redirectUri);
  } catch {
    return null;
  }
  if (url.protocol !== "http:") {
    return null;
  }
  // URL.hostname 对 IPv6 保留方括号，监听时需要去掉。
  const listener = url.hostname === "[::1]" ? "::1" : url.hostname;
  if (!["127.0.0.1", "localhost", "::1"].includes(listener)) {
    return null;
  }
  // localhost 可能同时解析到 IPv6 与 IPv4；固定绑 127.0.0.1 以免浏览器连到另一个族。
  return {
    host: listener === "localhost" ? "127.0.0.1" : listener,
    port: url.port.length > 0 ? Number(url.port) : 80,
    path: url.pathname,
  };
}

/** 一次性回调接收器。 */
interface CallbackReceiver {
  /** Google 带 code 返回时的结果；出错或超时则 reject。 */
  received: Promise<CallbackQuery>;
  /** 关闭监听并清理定时器；可重复调用。 */
  close: () => Promise<void>;
}

/**
 * 在 loopback 上启动只服务一次授权回调的 HTTP 服务。
 *
 * 只回应配置路径：其他路径返回 404，避免同端口上意外把无关请求当成回调。
 * state 由调用方比对，这里只负责把参数原样取出。
 *
 * @param target - 监听主机、端口与回调路径。
 * @returns 结果 Promise 与关闭函数。
 * @throws CliUsageError 端口被占用等无法监听时抛出。
 */
async function startCallbackReceiver(target: LoopbackTarget): Promise<CallbackReceiver> {
  let settle: ((query: CallbackQuery) => void) | null = null;
  let fail: ((error: Error) => void) | null = null;
  let timer: NodeJS.Timeout | null = null;
  const received = new Promise<CallbackQuery>((resolve, reject) => {
    settle = resolve;
    fail = reject;
  });

  const server = createServer((request: IncomingMessage, response: ServerResponse) => {
    const url = new URL(request.url ?? "/", `http://${target.host}:${target.port}`);
    if (url.pathname !== target.path) {
      response.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("Not found");
      return;
    }
    const error = url.searchParams.get("error");
    const code = url.searchParams.get("code") ?? "";
    const state = url.searchParams.get("state") ?? "";
    if (error !== null || code.length === 0 || state.length === 0) {
      respondWithPage(response, "授权未完成", "Google 未返回授权码，可以关闭此页面并重新运行命令。");
      fail?.(new CliUsageError(`Google 授权未完成：${error ?? "缺少 code 或 state"}`));
      return;
    }
    respondWithPage(response, "授权已完成", "可以关闭此页面，回到终端继续。");
    settle?.({ code, state });
  });

  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(target.port, target.host, () => {
        server.off("error", reject);
        resolve();
      });
    });
  } catch (error: unknown) {
    throw new CliUsageError(
      `无法监听 ${target.host}:${target.port}：${error instanceof Error ? error.message : "未知错误"}`,
    );
  }

  timer = setTimeout(() => {
    fail?.(new CliUsageError("等待 Google 授权回调超时（10 分钟），请重新运行命令"));
  }, CALLBACK_TIMEOUT_MS);

  return {
    received,
    close: async () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      await new Promise<void>((resolve) => {
        server.close(() => resolve());
        // keep-alive 连接会让 close 一直等待，主动断开以保证退出。
        server.closeAllConnections();
      });
    },
  };
}

function respondWithPage(response: ServerResponse, title: string, detail: string): void {
  response.writeHead(200, {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
  }).end(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>${title}</title>`
    + "</head><body style=\"font-family:system-ui;padding:2rem\">"
    + `<h1 style="font-size:1.25rem">${title}</h1><p>${detail}</p></body></html>`);
}

/**
 * 尽力在系统默认浏览器里打开授权地址。
 *
 * 打不开不算失败：地址已经打印在终端，用户可以手动复制。
 *
 * @param url - Google 授权地址。
 * @returns 是否成功发起打开动作。
 */
function openInBrowser(url: string): boolean {
  const command = process.platform === "darwin"
    ? "open"
    : process.platform === "win32" ? "explorer" : "xdg-open";
  try {
    const child = spawn(command, [url], { stdio: "ignore", detached: true });
    child.on("error", () => undefined);
    child.unref();
    return true;
  } catch {
    return false;
  }
}

/** `connect`：完成一次本机授权并把连接写入数据库。 */
async function runConnect(options: CliOptions): Promise<void> {
  const environment = requireEnvironment();
  const googleOAuth = loadGmailGoogleOAuthConfig(environment);
  if (googleOAuth === null) {
    throw new CliUsageError("未配置 Gmail OAuth：需要 LYRA_GOOGLE_CLIENT_ID 与 LYRA_GOOGLE_REDIRECT_URI");
  }
  const target = readLoopbackTarget(googleOAuth.redirectUri);
  if (target === null) {
    throw new CliUsageError(
      `LYRA_GOOGLE_REDIRECT_URI 必须是本机 loopback 地址（127.0.0.1 / localhost / [::1] 且为 http），当前为 ${googleOAuth.redirectUri}`,
    );
  }

  const database = await openLyraDatabase(environment.LYRA_DATABASE_PATH);
  try {
    const cipher = new SecretCipher(Buffer.from(environment.LYRA_SECRET_MASTER_KEY, "hex"));
    const gmailService = new GmailService({ database, cipher, googleOAuth });
    const transaction = gmailService.createAuthorizationTransaction(CLI_DEVICE_ID);
    if (transaction === null) {
      throw new CliUsageError("Google OAuth 客户端未生效，无法创建授权事务");
    }

    const receiver = await startCallbackReceiver(target);
    try {
      process.stdout.write("请在浏览器中完成 Google 授权（只申请 Gmail 只读权限）。\n");
      process.stdout.write(`授权地址：${transaction.authorizationUrl}\n`);
      if (options.openBrowser && !openInBrowser(transaction.authorizationUrl)) {
        process.stdout.write("未能自动打开浏览器，请手动复制上方地址。\n");
      }

      const callback = await receiver.received;
      // 事务对象只公开授权地址，state 内嵌在其中；用它与回调比对，
      // 避免被引导提交他人构造的授权码。
      const expectedState = new URL(transaction.authorizationUrl).searchParams.get("state") ?? "";
      if (expectedState.length === 0 || callback.state !== expectedState) {
        throw new CliUsageError("回调 state 与本次授权事务不一致，已拒绝");
      }

      const connection = await gmailService.completeAuthorizationTransaction({
        deviceId: CLI_DEVICE_ID,
        state: callback.state,
        authorizationCode: callback.code,
      });
      process.stdout.write(`已保存 Gmail 连接：${describeConnection(connection)}\n`);
    } finally {
      await receiver.close();
    }
  } finally {
    database.close();
  }
}

/** `connections`：列出已保存的连接。 */
async function runConnections(): Promise<void> {
  const environment = requireEnvironment();
  const googleOAuth = loadGmailGoogleOAuthConfig(environment);
  if (googleOAuth === null) {
    throw new CliUsageError("未配置 Gmail OAuth：需要 LYRA_GOOGLE_CLIENT_ID 与 LYRA_GOOGLE_REDIRECT_URI");
  }
  const database = await openLyraDatabase(environment.LYRA_DATABASE_PATH);
  try {
    const cipher = new SecretCipher(Buffer.from(environment.LYRA_SECRET_MASTER_KEY, "hex"));
    const gmailService = new GmailService({ database, cipher, googleOAuth });
    const connections = await gmailService.listConnections();
    if (connections.length === 0) {
      process.stdout.write("尚无 Gmail 连接；先运行 connect。\n");
      return;
    }
    for (const connection of connections) {
      process.stdout.write(`${describeConnection(connection)}\n`);
    }
  } finally {
    database.close();
  }
}

/** `sync`：同步邮件并导入账本。 */
async function runSync(options: CliOptions): Promise<void> {
  const environment = requireEnvironment();
  const googleOAuth = loadGmailGoogleOAuthConfig(environment);
  if (googleOAuth === null) {
    throw new CliUsageError("未配置 Gmail OAuth：需要 LYRA_GOOGLE_CLIENT_ID 与 LYRA_GOOGLE_REDIRECT_URI");
  }
  const database = await openLyraDatabase(environment.LYRA_DATABASE_PATH);
  const writeCoordinator = new WriteCoordinator();
  try {
    const cipher = new SecretCipher(Buffer.from(environment.LYRA_SECRET_MASTER_KEY, "hex"));
    const gmailService = new GmailService({ database, cipher, googleOAuth });
    const syncService = new GmailSyncService({ database, writeCoordinator, cipher, googleOAuth });

    const connections = await gmailService.listConnections();
    const targets = options.connectionId === null
      ? connections
      : connections.filter((connection) => connection.id === options.connectionId);
    if (targets.length === 0) {
      throw new CliUsageError(options.connectionId === null
        ? "尚无 Gmail 连接；先运行 connect"
        : `找不到连接 ${options.connectionId}；用 connections 查看现有连接`);
    }

    let failed = 0;
    for (const connection of targets) {
      try {
        const result = await syncService.sync({
          connectionId: connection.id,
          confirmedRescanAfterDate: options.rescanAfterDate,
          explicitAccountId: options.explicitAccountId,
        });
        process.stdout.write(`${describeConnection(connection)}\n`);
        process.stdout.write(`  同步结果：${describeSyncResult(result)}\n`);
      } catch (error: unknown) {
        // 单条连接失败不阻断其余连接，但必须如实打印并让退出码非零。
        failed += 1;
        process.stderr.write(`[FAIL] ${connection.id}（${connection.googleEmail}）：${describeError(error)}\n`);
      }
    }
    if (failed > 0) {
      process.exitCode = 1;
    }
  } finally {
    await writeCoordinator.close();
    database.close();
  }
}

function describeConnection(connection: PublicGmailConnection): string {
  const lastSync = connection.lastSyncAt ?? "从未同步";
  const error = connection.lastErrorCode === null ? "" : `，错误 ${connection.lastErrorCode}`;
  return `${connection.id}｜${connection.googleEmail}｜状态 ${connection.status}｜上次同步 ${lastSync}${error}`;
}

function describeSyncResult(result: GmailSyncHttpResponse): string {
  return [
    `模式 ${result.mode}${result.truncated ? "（已截断）" : ""}`,
    `导入 ${result.importedMessages}`,
    `已存在 ${result.alreadyImportedMessages}`,
    `冲突 ${result.conflictMessages}`,
    `拒绝 ${result.rejectedMessages}`,
    `忽略 ${result.ignoredMessages}`,
    `连接状态 ${result.connection.status}`,
  ].join("，");
}

/** 取出错误对象上的稳定错误码；没有或不合法时返回 null。 */
function readStableCode(error: Error): string | null {
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" && code.length > 0 ? code : null;
}

/**
 * 收集异常的原因链消息。
 *
 * 底层原因在服务端是刻意收敛掉的（例如只保留 Google 端点的 HTTP 状态），
 * 但命令行由本人执行、且结果不会回传给任何页面，看不到状态就无法定位故障。
 *
 * @param error - 起始异常。
 * @returns 最多 4 层的原因消息，外层在前。
 */
function readCauses(error: Error): readonly string[] {
  const causes: string[] = [];
  let current: unknown = error.cause;
  for (let depth = 0; depth < 4 && current instanceof Error; depth += 1) {
    causes.push(current.message);
    current = current.cause;
  }
  return causes;
}

/**
 * 把异常还原为可操作的诊断信息。
 *
 * 首行是稳定错误码，其后按因果顺序附上原因链；不使用 Google 响应正文，
 * 只使用仓库自身生成的说明文本。
 */
function describeError(error: unknown): string {
  if (error instanceof CliUsageError) {
    return error.message;
  }
  if (!(error instanceof Error)) {
    return "未知错误";
  }
  const code = readStableCode(error);
  const headline = error instanceof ApplicationError
    ? `${error.code}：${error.message}`
    : code === null ? error.name : `${error.name}（${code}）`;
  const causes = readCauses(error);
  return causes.length === 0 ? headline : `${headline}\n  ↳ ${causes.join("\n  ↳ ")}`;
}

async function run(): Promise<number> {
  let options: CliOptions;
  try {
    options = parseArguments(process.argv.slice(2));
    // 出站请求（换码、取信）都在此之后发生，先装好代理 dispatcher。
    const proxy = installEnvironmentProxy();
    if (proxy !== null) {
      // 代理地址可能内嵌凭据，这里只回显协议与主机端口。
      const parsed = new URL(proxy);
      process.stdout.write(`已启用出站代理：${parsed.protocol}//${parsed.host}\n`);
    }
  } catch (error: unknown) {
    process.stderr.write(`[FAIL] ${describeError(error)}\n\n${usage()}\n`);
    return 2;
  }
  if (options.command === "help") {
    process.stdout.write(`${usage()}\n`);
    return 0;
  }
  try {
    switch (options.command) {
      case "connect":
        await runConnect(options);
        break;
      case "connections":
        await runConnections();
        break;
      case "sync":
        await runSync(options);
        break;
      default:
        process.stdout.write(`${usage()}\n`);
    }
    return typeof process.exitCode === "number" ? process.exitCode : 0;
  } catch (error: unknown) {
    process.stderr.write(`[FAIL] ${describeError(error)}\n`);
    return 1;
  }
}

// 与 check-config 一致：只用固定文案与变量名，不转发底层错误正文。
process.exitCode = await run();
