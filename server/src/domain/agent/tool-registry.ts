import type { AccountScope } from "../analytics/index.js";
import { CASH_ACTIONS, POSITION_ACTIONS } from "../ledger/index.js";

/** REST 与 Genkit 共用的对象输入 JSON Schema。 */
export interface FinancialToolInputSchema {
  type: "object";
  properties: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
  required?: readonly string[];
  additionalProperties: false;
}

/** 业务工具与审计共用的只读能力标记。 */
export interface FinancialToolAnnotations {
  readOnlyHint: true;
  destructiveHint: false;
  idempotentHint: true;
  openWorldHint: boolean;
}

/** 单一金融工具定义；不包含 handler 或任何会话授权数据。 */
export interface FinancialToolDefinition {
  name: string;
  description: string;
  inputSchema: FinancialToolInputSchema;
  annotations: FinancialToolAnnotations;
}

const READ_ONLY_ANNOTATIONS = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
} as const;
const EMPTY_INPUT_SCHEMA = {
  type: "object",
  properties: {},
  additionalProperties: false,
} as const;
const DATE_RANGE_PROPERTIES = {
  fromDate: { type: "string", format: "date", description: "可选闭区间起始日期" },
  toDate: { type: "string", format: "date", description: "可选闭区间截止日期" },
} as const;
const SOURCE_TYPE_PROPERTY = {
  type: "string",
  enum: ["csv", "excel", "gmail"],
  description: "可选权威数据来源",
} as const;
const PAGINATION_PROPERTIES = {
  limit: { type: "integer", minimum: 1, maximum: 200 },
  cursor: { type: "string", minLength: 1, maxLength: 1024 },
} as const;

/**
 * 账本分析与外部行情估值共用的唯一只读工具定义。
 *
 * 账户范围、账本版本和 asOf 由服务端会话凭据注入，因此不会出现在模型可控
 * schema 中。Genkit 注册、REST 调用和审计展示都必须读取本清单。
 */
export const FINANCIAL_TOOL_DEFINITIONS = [
  {
    name: "accounts",
    description: "列出当前设备获授权的 Schwab 账户及其状态，不返回完整账户号码。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "summary",
    description: "汇总授权账户范围内的入金、已实现损益、费用、现金和交易数量。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "positions",
    description: "查询当前未平 FIFO 持仓和历史成本；结果不是实时市值。",
    inputSchema: {
      type: "object",
      properties: {
        underlying: { type: "string", minLength: 1, maxLength: 32 },
      },
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "expiring",
    description: "查询服务端截止日之后指定天数内到期或已到期的未平期权。",
    inputSchema: {
      type: "object",
      properties: {
        days: { type: "integer", minimum: 0, maximum: 366 },
      },
      required: ["days"],
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "trades",
    description: "按标的、动作和日期查询标准交易流水，结果数量有明确上限。",
    inputSchema: {
      type: "object",
      properties: {
        symbol: { type: "string", minLength: 1, maxLength: 64 },
        action: {
          type: "string",
          enum: [...POSITION_ACTIONS, ...CASH_ACTIONS],
          description: "可选 Schwab 标准交易动作",
        },
        sourceType: SOURCE_TYPE_PROPERTY,
        ...DATE_RANGE_PROPERTIES,
        ...PAGINATION_PROPERTIES,
      },
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "realized",
    description: "查询 FIFO 已实现损益明细及筛选范围合计。",
    inputSchema: {
      type: "object",
      properties: {
        symbol: { type: "string", minLength: 1, maxLength: 64 },
        ...DATE_RANGE_PROPERTIES,
        ...PAGINATION_PROPERTIES,
      },
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "cashflow",
    description: "查询现金类交易，并按 Schwab 动作汇总现金变化。",
    inputSchema: {
      type: "object",
      properties: {
        sourceType: SOURCE_TYPE_PROPERTY,
        ...DATE_RANGE_PROPERTIES,
        ...PAGINATION_PROPERTIES,
      },
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "monthly",
    description: "按平仓月份汇总 FIFO 已实现绩效、胜率、费用和盈亏比。",
    inputSchema: {
      type: "object",
      properties: DATE_RANGE_PROPERTIES,
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "allocation",
    description: "按标的或资产类型分析未平仓历史成本配置，不代表实时市值配置。",
    inputSchema: {
      type: "object",
      properties: {
        groupBy: { type: "string", enum: ["underlying", "asset-type"] },
      },
      required: ["groupBy"],
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "attribution",
    description: "按指定维度拆解 FIFO 已实现损益贡献、胜率和费用。",
    inputSchema: {
      type: "object",
      properties: {
        groupBy: {
          type: "string",
          enum: ["underlying", "asset-type", "direction", "close-action"],
        },
        ...DATE_RANGE_PROPERTIES,
      },
      required: ["groupBy"],
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "drawdown",
    description: "计算累计已实现 FIFO 损益曲线与峰谷回撤，不代表账户净值回撤。",
    inputSchema: {
      type: "object",
      properties: DATE_RANGE_PROPERTIES,
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "risk",
    description: "按到期窗口分析未平期权历史权利金风险，不使用实时市场价格。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "holding-period",
    description: "按实际持有天数区间汇总 FIFO 已实现绩效。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "extremes",
    description: "按平仓交易净损益列出最佳和最差各指定数量的交易。",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", minimum: 1, maximum: 50 },
      },
      required: ["limit"],
      additionalProperties: false,
    },
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "streaks",
    description: "统计按平仓交易排列的最长连续盈利与连续亏损。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "stress",
    description: "模拟当前长期权权利金全部归零的历史成本压力，不含行情估值。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "audit",
    description: "检查当前账本版本的 FIFO 产物完整性、孤立记录和非法数量。",
    inputSchema: EMPTY_INPUT_SCHEMA,
    annotations: READ_ONLY_ANNOTATIONS,
  },
  {
    name: "market-positions",
    description: "查询授权当前持仓的外部延迟或历史报价、参考市值与未实现盈亏（不含费用）。须说明每项报价日期、价格口径、缺失和陈旧状态；不同报价时点的估值不是历史净值或今日账户盈亏。",
    inputSchema: {
      type: "object",
      properties: { underlying: { type: "string", minLength: 1, maxLength: 32 } },
      additionalProperties: false,
    },
    annotations: { ...READ_ONLY_ANNOTATIONS, openWorldHint: true },
  },
] as const satisfies readonly FinancialToolDefinition[];

/** 编译期与运行时共享的金融工具名。 */
export type FinancialToolName = typeof FINANCIAL_TOOL_DEFINITIONS[number]["name"];

/** 保持公开定义顺序的金融工具名清单。 */
export const FINANCIAL_TOOL_NAMES: readonly FinancialToolName[] =
  FINANCIAL_TOOL_DEFINITIONS.map((definition) => definition.name);

/** 服务端授权后注入每次 Agent 工具调用的不可变数据口径。 */
export interface AgentToolContext {
  accountScope: AccountScope;
  ledgerVersion: number;
  asOf: string;
}

/** 实际 handler 只接收已移除越权范围字段的参数。 */
export interface AgentToolInvocation {
  arguments: Readonly<Record<string, unknown>>;
  context: AgentToolContext;
}

/** 一个金融工具的应用层执行函数。 */
export type AgentToolHandler = (invocation: AgentToolInvocation) => Promise<unknown>;

/** Agent 请求了未知、禁止或尚未配置的工具。 */
export class AgentToolPolicyError extends Error {
  readonly code = "AGENT_TOOL_FORBIDDEN" as const;

  /**
   * @param message - 可写入安全审计日志的策略拒绝原因。
   */
  constructor(message: string) {
    super(message);
    this.name = "AgentToolPolicyError";
  }
}

/**
 * 只暴露设计方案中确认过的只读金融工具。
 *
 * REST 与 Genkit 使用同一个实例。注册表在调用 handler 前删除模型
 * 传入的账户范围、账本版本和截止时间字段，并以服务端上下文单独注入，防止
 * 模型通过参数跨账户或查询未授权账本版本。
 */
export class FinancialToolRegistry {
  private readonly handlers = new Map<FinancialToolName, AgentToolHandler>();

  /**
   * @param handlers - 当前已连接到应用查询用例的工具处理函数。
   */
  constructor(handlers: Partial<Record<FinancialToolName, AgentToolHandler>>) {
    for (const name of FINANCIAL_TOOL_NAMES) {
      const handler = handlers[name];
      if (handler !== undefined) {
        this.handlers.set(name, handler);
      }
    }
  }

  /** 返回适配器可以公开的已配置工具名，顺序固定。 */
  availableTools(): readonly FinancialToolName[] {
    return FINANCIAL_TOOL_NAMES.filter((name) => this.handlers.has(name));
  }

  /** 返回 Genkit 可以公开的已配置工具定义，禁止为供应商复制另一份 schema。 */
  availableToolDefinitions(): readonly FinancialToolDefinition[] {
    return FINANCIAL_TOOL_DEFINITIONS.filter((definition) => (
      this.handlers.has(definition.name)
    ));
  }

  /**
   * 执行一个通过白名单的只读工具。
   *
   * @param name - Agent 请求的原始工具名。
   * @param arguments_ - 已经完成具体 schema 校验的工具参数。
   * @param context - 服务端会话授权确定的账户范围和账本基线。
   * @returns 对应确定性查询用例的结果。
   * @throws AgentToolPolicyError 当工具不在白名单或未配置时抛出。
   */
  async invoke(
    name: string,
    arguments_: Readonly<Record<string, unknown>>,
    context: AgentToolContext,
  ): Promise<unknown> {
    if (!isFinancialToolName(name)) {
      throw new AgentToolPolicyError(`Agent 工具不在只读白名单中: ${name}`);
    }
    const handler = this.handlers.get(name);
    if (handler === undefined) {
      throw new AgentToolPolicyError(`Agent 工具尚未配置: ${name}`);
    }

    const authorizedArguments = { ...arguments_ };
    for (const reserved of [
      "account_scope", "accountScope",
      "ledger_version", "ledgerVersion",
      "as_of", "asOf",
    ]) {
      delete authorizedArguments[reserved];
    }
    return handler({ arguments: authorizedArguments, context });
  }
}

/** 在运行时把任意工具名收窄到只读白名单。 */
function isFinancialToolName(name: string): name is FinancialToolName {
  return (FINANCIAL_TOOL_NAMES as readonly string[]).includes(name);
}
