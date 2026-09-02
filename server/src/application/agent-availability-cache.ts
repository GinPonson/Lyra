import type { AgentProvider } from "../domain/agent/index.js";
import type { AgentAvailabilitySnapshot } from "./agent-availability.js";
import { createAgentAvailabilitySnapshot } from "./agent-availability.js";
import { probeAgentCli } from "../infrastructure/agents/index.js";

/** 默认探测缓存时长；探测批量运行三个 CLI --version，不能每个请求都执行。 */
const DEFAULT_CACHE_MILLISECONDS = 5 * 60 * 1000;
const DEFAULT_VERSION_ARGUMENTS = ["--version"] as const;

/**
 * 懒探测并缓存三 Agent 可用性快照。
 *
 * 启动不阻塞探测（设计 §21.1）；首个需要可用性的请求触发同一隔离探测批次，
 * 完成前到达的并发请求共享同一次探测，缓存过期后下一次请求重新探测。
 */
export function createAgentAvailabilityProvider(input: {
  executables: Readonly<Record<AgentProvider, string>>;
  /** 经适配器契约核对的精确版本白名单；空名单按配置错误拒绝。 */
  allowedVersions: Readonly<Record<AgentProvider, readonly string[]>>;
  versionArguments?: readonly string[];
  probeTimeoutMilliseconds: number;
  cacheMilliseconds?: number;
  now?: () => Date;
}): () => Promise<AgentAvailabilitySnapshot> {
  const cacheMilliseconds = input.cacheMilliseconds ?? DEFAULT_CACHE_MILLISECONDS;
  const versionArguments = input.versionArguments ?? DEFAULT_VERSION_ARGUMENTS;
  const now = input.now ?? (() => new Date());
  let cached: { snapshot: AgentAvailabilitySnapshot; expiresAt: number } | null = null;
  let inFlight: Promise<AgentAvailabilitySnapshot> | null = null;

  return () => {
    const current = cached;
    if (current !== null && current.expiresAt > now().getTime()) {
      return Promise.resolve(current.snapshot);
    }
    if (inFlight !== null) {
      return inFlight;
    }
    inFlight = Promise.all([
      probeAgentCli({
        provider: "claude",
        executablePath: input.executables.claude,
        versionArguments,
        allowedVersions: input.allowedVersions.claude,
        timeoutMilliseconds: input.probeTimeoutMilliseconds,
      }),
      probeAgentCli({
        provider: "pi",
        executablePath: input.executables.pi,
        versionArguments,
        allowedVersions: input.allowedVersions.pi,
        timeoutMilliseconds: input.probeTimeoutMilliseconds,
      }),
      probeAgentCli({
        provider: "opencode",
        executablePath: input.executables.opencode,
        versionArguments,
        allowedVersions: input.allowedVersions.opencode,
        timeoutMilliseconds: input.probeTimeoutMilliseconds,
      }),
    ])
      .then((results) => {
        const snapshot = createAgentAvailabilitySnapshot(results, now());
        cached = { snapshot, expiresAt: now().getTime() + cacheMilliseconds };
        return snapshot;
      })
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };
}
