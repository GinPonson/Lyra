import type { MarketQuoteIssue } from "../../domain/market-data/types.js";

/** The only network boundary; tests inject responses without real credentials. */
export type MarketDataFetch = (input: string | URL, init?: RequestInit) => Promise<Response>;

export const MARKET_DATA_TIMEOUT_MILLISECONDS = 10_000;
export const MARKET_DATA_MAX_BODY_BYTES = 64 * 1024;

export type MarketJsonResult =
  | { ok: true; status: number; payload: unknown }
  | { ok: false; reason: MarketQuoteIssue };

/** Auth stays in headers, redirects are forbidden, and the deadline includes body reads. */
export async function requestMarketJson(
  url: URL,
  headers: Readonly<Record<string, string>>,
  callerSignal: AbortSignal,
  fetchFn: MarketDataFetch,
): Promise<MarketJsonResult> {
  if (callerSignal.aborted) return { ok: false, reason: "timeout" };
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(), MARKET_DATA_TIMEOUT_MILLISECONDS);
  timer.unref?.();
  const signal = AbortSignal.any([callerSignal, deadline.signal]);
  let receivedResponse: Response | undefined;
  try {
    const response = await withAbort(fetchFn(url, {
      method: "GET",
      headers: { accept: "application/json", ...headers },
      redirect: "error",
      signal,
    }).then((received) => {
      receivedResponse = received;
      // A fake or non-cooperating transport may settle after the deadline.
      if (signal.aborted) discardBody(received);
      return received;
    }), signal);

    const failure = httpFailure(response.status);
    if (failure !== null) {
      discardBody(response);
      return { ok: false, reason: failure };
    }
    // A 404 body is needed to distinguish MarketData no_data from endpoint errors.
    const payload = await readBoundedJson(response, signal);
    return { ok: true, status: response.status, payload };
  } catch (error) {
    if (signal.aborted || (error instanceof Error && error.name === "TimeoutError")) {
      return { ok: false, reason: "timeout" };
    }
    return {
      ok: false,
      reason: error instanceof InvalidMarketPayload ? "invalid_response" : "provider_unavailable",
    };
  } finally {
    clearTimeout(timer);
    deadline.abort();
    // Cancellation may win after fetch resolves but before its value reaches the await.
    if (receivedResponse !== undefined) discardBody(receivedResponse);
  }
}

/** No provider body or transport exception is copied into a user-visible error. */
function httpFailure(status: number): MarketQuoteIssue | null {
  if (status === 401 || status === 403) return "credentials_rejected";
  if (status === 429) return "rate_limited";
  if (status === 200 || status === 203 || status === 404) return null;
  return "provider_unavailable";
}

class InvalidMarketPayload extends Error {}

async function readBoundedJson(response: Response, signal: AbortSignal): Promise<unknown> {
  const contentLength = response.headers.get("content-length");
  if (contentLength !== null && Number(contentLength) > MARKET_DATA_MAX_BODY_BYTES) {
    discardBody(response);
    throw new InvalidMarketPayload();
  }
  if (response.body === null) throw new InvalidMarketPayload();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  let completed = false;
  try {
    while (true) {
      const part = await withAbort(reader.read(), signal);
      if (part.done) {
        completed = true;
        break;
      }
      length += part.value.byteLength;
      if (length > MARKET_DATA_MAX_BODY_BYTES) throw new InvalidMarketPayload();
      chunks.push(part.value);
    }
  } finally {
    if (!completed) void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    throw new InvalidMarketPayload();
  }
}

function discardBody(response: Response): void {
  void response.body?.cancel().catch(() => {});
}

/** Bound non-cooperating injected fetch/body implementations as well as native fetch. */
function withAbort<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(new DOMException("Market data request stopped", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    operation.then((value) => {
      signal.removeEventListener("abort", abort);
      if (signal.aborted) abort();
      else resolve(value);
    }, (error: unknown) => {
      signal.removeEventListener("abort", abort);
      reject(error);
    });
    if (signal.aborted) abort();
  });
}
