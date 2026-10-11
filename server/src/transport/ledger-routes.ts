import { Ajv, type ValidateFunction } from "ajv";
import addFormats from "ajv-formats";
import type { FastifyInstance, FastifyRequest } from "fastify";

import type { FinancialToolName, FinancialToolRegistry } from "../domain/agent/index.js";
import { ApplicationError } from "../application/index.js";
import {
  createLedgerReadResult,
  resolveLedgerReadScope,
} from "../application/index.js";
import { readCurrentLedgerVersion } from "../infrastructure/db/agent-session-repository.js";
import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";

/** 账本与分析只读路由；路径与 Tool Registry 的金融工具一一对应。 */
export interface LedgerRouteOptions {
  registry: FinancialToolRegistry;
  database: LyraDatabase;
  authenticateDevice: (token: string) => Promise<PairedDeviceAuthRecord>;
  now?: () => Date;
}

/** 显式声明的只读路由表；保持与设计方案 §14.2 的路由面一致。 */
const LEDGER_TOOL_ROUTES: readonly { path: string; tool: FinancialToolName }[] = [
  { path: "/v1/ledger/accounts", tool: "accounts" },
  { path: "/v1/ledger/summary", tool: "summary" },
  { path: "/v1/ledger/positions", tool: "positions" },
  { path: "/v1/ledger/expiring", tool: "expiring" },
  { path: "/v1/ledger/trades", tool: "trades" },
  { path: "/v1/ledger/realized", tool: "realized" },
  { path: "/v1/ledger/cashflow", tool: "cashflow" },
  { path: "/v1/ledger/audit", tool: "audit" },
  { path: "/v1/analytics/monthly", tool: "monthly" },
  { path: "/v1/analytics/allocation", tool: "allocation" },
  { path: "/v1/analytics/attribution", tool: "attribution" },
  { path: "/v1/analytics/drawdown", tool: "drawdown" },
  { path: "/v1/analytics/risk", tool: "risk" },
  { path: "/v1/analytics/holding-period", tool: "holding-period" },
  { path: "/v1/analytics/extremes", tool: "extremes" },
  { path: "/v1/analytics/streaks", tool: "streaks" },
  { path: "/v1/analytics/stress", tool: "stress" },
  { path: "/v1/market/positions", tool: "market-positions" },
];

/**
 * 注册账本与分析只读路由。
 *
 * 金融计算复用 Tool Registry 的唯一执行路径，App 与 Agent 消费同一份结果；
 * 账户范围必须显式携带，账本版本、截止时间与获取时间由服务端权威生成并附
 * 在响应元数据中，客户端不得用请求参数覆盖。
 */
export function registerLedgerRoutes(
  app: FastifyInstance,
  options: LedgerRouteOptions,
): void {
  const now = options.now ?? (() => new Date());
  const validatorProvider = new Ajv({ allErrors: true, strict: false });
  addFormats.default(validatorProvider);
  const definitions = new Map(
    options.registry.availableToolDefinitions().map((definition) => [
      definition.name,
      definition,
    ]),
  );
  const validators = new Map<FinancialToolName, ValidateFunction>();

  const authenticate = async (request: FastifyRequest): Promise<void> => {
    const header = request.headers.authorization;
    const token = typeof header === "string" && header.startsWith("Bearer ")
      ? header.slice("Bearer ".length)
      : null;
    if (token === null) {
      throw new ApplicationError({
        code: "DEVICE_CREDENTIAL_INVALID",
        message: "设备访问令牌无效",
        statusCode: 401,
        retryable: false,
      });
    }
    await options.authenticateDevice(token);
  };

  for (const route of LEDGER_TOOL_ROUTES) {
    app.get(route.path, { preHandler: authenticate }, async (request) => {
      const definition = definitions.get(route.tool);
      if (definition === undefined) {
        throw new ApplicationError({
          code: "AGENT_UNAVAILABLE",
          message: "账本查询暂不可用",
          statusCode: 503,
          retryable: false,
        });
      }
      const query = request.query as Readonly<Record<string, unknown>>;
      const requestedScope = parseScopeQuery(query.scope);
      const args = coerceQueryArguments(
        definition.inputSchema as unknown as Readonly<Record<string, unknown>>,
        query,
      );
      const validator = validators.get(route.tool)
        ?? validatorProvider.compile(definition.inputSchema);
      validators.set(route.tool, validator);
      if (!validator(args)) {
        throw new ApplicationError({
          code: "INVALID_ARGUMENT",
          message: "账本查询参数不符合公开定义",
          statusCode: 400,
          retryable: false,
        });
      }

      const ledgerVersion = await readCurrentLedgerVersion(options.database);
      const asOf = now().toISOString();
      const scope = await resolveLedgerReadScope(
        options.database,
        requestedScope,
        ledgerVersion,
      );
      const data = await options.registry.invoke(route.tool, args, {
        accountScope: requestedScope,
        ledgerVersion,
        asOf,
      });
      return createLedgerReadResult({
        data,
        scope,
        ledgerVersion,
        asOf,
        fetchedAt: now(),
      });
    });
  }
}

/** 解析显式账户范围查询参数；缺省即拒绝，不做隐式全部账户。 */
function parseScopeQuery(value: unknown): { kind: "all" } | { kind: "account"; account: string } {
  if (value === "all") {
    return { kind: "all" };
  }
  if (
    typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(value)
  ) {
    return { kind: "account", account: value };
  }
  throw new ApplicationError({
    code: "ACCOUNT_SCOPE_REQUIRED",
    message: "请求必须明确指定单个账户或全部活跃账户",
    statusCode: 400,
    retryable: false,
  });
}

/**
 * 按工具公开 schema 把数值型查询字符串收窄为数字，其余保持文本。
 *
 * 重复查询参数会被解析为数组（`?underlying=A&underlying=B`）。静默跳过非字符串值等于
 * 丢弃用户的筛选条件并返回未过滤结果，因此这里显式以 INVALID_ARGUMENT 拒绝，而不是忽略。
 * `scope` 由 parseScopeQuery 单独校验。
 */
function coerceQueryArguments(
  schema: Readonly<Record<string, unknown>>,
  query: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const properties = (schema.properties ?? {}) as Readonly<Record<string, {
    type?: string;
  }>>;
  const args: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(query)) {
    if (key === "scope") {
      continue;
    }
    if (typeof value !== "string") {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT",
        message: "账本查询参数必须是单个字符串",
        statusCode: 400,
        retryable: false,
      });
    }
    const propertyType = properties[key]?.type;
    if ((propertyType === "integer" || propertyType === "number")
      && /^-?\d+(?:\.\d+)?$/.test(value)) {
      args[key] = Number(value);
      continue;
    }
    args[key] = value;
  }
  return args;
}
