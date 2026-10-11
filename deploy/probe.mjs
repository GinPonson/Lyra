// No business writes, OAuth, or model calls. An empty, non-RPC POST additionally
// checks that the removed MCP transport cannot still accept POST requests.
const argument = process.argv[2];
let origin;
try {
  if (!argument || process.argv.length !== 3) throw new Error();
  const url = new URL(argument);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && local))
      || url.username || url.password || url.search || url.hash
      || (url.pathname !== "/" && url.pathname !== "")) throw new Error();
  origin = url.origin;
} catch {
  process.stderr.write("Usage: node deploy/probe.mjs https://your-lyra-host (HTTP is allowed only for loopback).\n");
  process.exitCode = 2;
}

if (origin) {
  const request = async (path, protocol = false) => fetch(`${origin}${path}`, {
    headers: protocol ? { "X-Lyra-Protocol-Version": "2" } : {},
    signal: AbortSignal.timeout(5000),
    redirect: "error",
  });
  let phase = "public service information";
  try {
    const response = await request("/v1/system/info");
    const info = await response.json();
    if (!response.ok || typeof info.server_version !== "string"
        || !Number.isInteger(info.protocol_version)
        || !Number.isInteger(info.min_supported_protocol_version)
        || !Number.isInteger(info.max_supported_protocol_version)
        || info.min_supported_protocol_version > 2 || info.max_supported_protocol_version < 2
        || !Number.isInteger(info.schema_version) || info.schema_version < 14) throw new Error();
    process.stdout.write("PASS service information and App protocol 2\n");

    phase = "ledger health";
    const healthResponse = await request("/v1/health");
    const health = await healthResponse.json();
    if (!healthResponse.ok || !["healthy", "degraded"].includes(health.status)
        || health.database?.status !== "available"
        || health.process?.status !== "available"
        || health.writeQueue?.status !== "available") throw new Error();
    process.stdout.write(health.status === "degraded"
      ? "PASS ledger health; an optional integration is not ready\n"
      : "PASS ledger health (integration status reflects local configuration only)\n");

    phase = "unauthenticated ledger protection";
    if ((await request("/v1/ledger/accounts", true)).status !== 401) throw new Error();
    process.stdout.write("PASS unauthenticated ledger access is rejected\n");

    phase = "client protocol gate";
    if ((await request("/v1/ledger/accounts")).status !== 426) throw new Error();
    process.stdout.write("PASS missing client protocol is rejected\n");

    phase = "removed MCP endpoint";
    if ((await request("/mcp")).status !== 404) throw new Error();
    const legacyPost = await fetch(`${origin}/mcp`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Lyra-Protocol-Version": "2" },
      body: "{}",
      signal: AbortSignal.timeout(5000),
      redirect: "error",
    });
    if (legacyPost.status !== 404) throw new Error();
    process.stdout.write("PASS legacy MCP endpoint is absent\n");
    process.stdout.write("Service checks passed. Real Google sign-in, model tool calling, and Android/WebSocket verification still require the App.\n");
  } catch {
    // Never print response bodies, headers, URLs containing credentials, or env.
    process.stderr.write(`FAIL ${phase}\n`);
    process.exitCode = 1;
  }
}
