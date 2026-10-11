// Container liveness checks only the local ledger service; optional integrations
// may be degraded without making the database unavailable.
const port = Number(process.env.LYRA_PORT ?? 3141);
try {
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) throw new Error();
  const response = await fetch(`http://127.0.0.1:${port}/v1/health`, {
    signal: AbortSignal.timeout(4000),
    redirect: "error",
  });
  const health = await response.json();
  if (!response.ok
      || !["healthy", "degraded"].includes(health.status)
      || health.process?.status !== "available"
      || health.database?.status !== "available"
      || health.writeQueue?.status !== "available") throw new Error();
} catch {
  // Docker retains healthcheck output: never emit an upstream body or env value.
  process.stderr.write("Lyra ledger healthcheck failed\n");
  process.exitCode = 1;
}
