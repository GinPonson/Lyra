import type { AgentProvider } from "../domain/agent/index.js";
import type {
  AgentCliProbeFailure,
  AgentCliProbeResult,
} from "../infrastructure/agents/index.js";

/** Agent 切换器固定展示顺序，不能由异步探测完成顺序决定。 */
const AGENT_PROVIDER_ORDER: readonly AgentProvider[] = ["claude", "pi", "opencode"];

/** App 可公开的单个 Agent 状态，不包含可执行路径、stderr 或认证信息。 */
export type PublicAgentAvailability =
  | { provider: AgentProvider; status: "available"; version: string }
  | { provider: AgentProvider; status: "unavailable"; reason: AgentCliProbeFailure };

/** Agent 页面和会话创建共用的一次完整探测快照。 */
export interface AgentAvailabilitySnapshot {
  checkedAt: string;
  providers: readonly PublicAgentAvailability[];
}

/**
 * 将三套隔离 CLI 探测结果整理为 App 可公开的完整快照。
 *
 * 异步探测返回顺序不稳定，因此输出始终固定为 Claude、Pi、OpenCode。三者必须
 * 各有且仅有一条结果；缺失或重复代表服务端探测批次不完整，不能让 App 猜测。
 *
 * @param results - 同一探测批次的三套 CLI 脱敏结果。
 * @param checkedAt - 该批次完成时的服务端可信时钟。
 */
export function createAgentAvailabilitySnapshot(
  results: readonly AgentCliProbeResult[],
  checkedAt: Date,
): AgentAvailabilitySnapshot {
  const byProvider = new Map<AgentProvider, AgentCliProbeResult>();
  for (const result of results) {
    if (byProvider.has(result.provider)) {
      throw new RangeError(`Agent 探测结果重复: ${result.provider}`);
    }
    byProvider.set(result.provider, result);
  }
  const missing = AGENT_PROVIDER_ORDER.filter((provider) => !byProvider.has(provider));
  if (missing.length > 0) {
    throw new RangeError(`Agent 探测结果缺失: ${missing.join(", ")}`);
  }
  if (Number.isNaN(checkedAt.getTime())) {
    throw new RangeError("Agent 探测完成时间无效");
  }

  return {
    checkedAt: checkedAt.toISOString(),
    providers: AGENT_PROVIDER_ORDER.map((provider) => {
      const result = byProvider.get(provider);
      if (result === undefined) {
        // 上面的完整性检查已经保证命中，此分支只保护未来修改破坏不变量。
        throw new Error(`Agent 探测结果内部缺失: ${provider}`);
      }
      return result.status === "available"
        ? { provider, status: "available", version: result.version }
        : { provider, status: "unavailable", reason: result.reason };
    }),
  };
}
