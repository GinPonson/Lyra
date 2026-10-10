/** App 可公开的唯一助手状态。available 表示本地配置完整，不表示远程 API 探测成功。 */
export type PublicAgentAvailability =
  | { provider: "genkit"; name: "Lyra"; status: "available"; version: "Genkit" }
  | { provider: "genkit"; name: "Lyra"; status: "unavailable"; reason: "not_configured" };

export interface AgentAvailabilitySnapshot {
  checkedAt: string;
  providers: readonly PublicAgentAvailability[];
}

/** 保持已有 API 容器结构，里面始终只有一个 Lyra 助手。 */
export function createAgentAvailabilitySnapshot(
  configured: boolean,
  checkedAt: Date,
): AgentAvailabilitySnapshot {
  if (Number.isNaN(checkedAt.getTime())) {
    throw new RangeError("Agent 状态检查时间无效");
  }
  return {
    checkedAt: checkedAt.toISOString(),
    providers: [configured
      ? { provider: "genkit", name: "Lyra", status: "available", version: "Genkit" }
      : { provider: "genkit", name: "Lyra", status: "unavailable", reason: "not_configured" }],
  };
}
