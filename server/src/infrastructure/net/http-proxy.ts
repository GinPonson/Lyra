import { ProxyAgent, setGlobalDispatcher } from "undici";

/**
 * 按优先级排列的代理环境变量；大小写两种写法都接受，取第一个非空项。
 * 沿用标准变量名，因此可以直接写进 `server/.env`（`--env-file` 会把它们加载进 process.env）。
 */
const PROXY_VARIABLES = [
  "https_proxy",
  "HTTPS_PROXY",
  "http_proxy",
  "HTTP_PROXY",
  "all_proxy",
  "ALL_PROXY",
] as const;

/**
 * 在设置了 HTTP 代理环境变量时，把进程内的全局 fetch 指向该代理。
 *
 * Node 自带的 fetch（undici）默认**不读取** `*_proxy` 环境变量，
 * 所以在不允许直连外网的环境里，OAuth 换码等出站请求会直接连接超时。
 * undici 也没有 `no_proxy` 语义，故只在命令行工具中调用：
 * 正式服务运行在具备出口的机器上，不应让全局 dispatcher 影响它的请求。
 *
 * @param environment - 环境变量来源，默认 process.env。
 * @returns 实际生效的代理地址；未配置代理时为 null。
 * @throws RangeError 代理地址非法，或配置的协议不是 undici 支持的 http(s)。
 */
export function installEnvironmentProxy(
  environment: Readonly<Record<string, string | undefined>> = process.env,
): string | null {
  for (const name of PROXY_VARIABLES) {
    const raw = environment[name]?.trim() ?? "";
    if (raw.length === 0) {
      continue;
    }
    let url: URL;
    try {
      url = new URL(raw);
    } catch (error: unknown) {
      throw new RangeError(`${name} 不是合法的代理地址`, { cause: error });
    }
    // socks 等协议 undici 的 ProxyAgent 不支持；与其静默退回直连后超时，
    // 不如明确报错，让使用者改成 http(s) 代理。
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new RangeError(`${name} 只支持 http/https 代理，当前为 ${url.protocol}`);
    }
    setGlobalDispatcher(new ProxyAgent(url.toString()));
    return url.toString();
  }
  return null;
}
