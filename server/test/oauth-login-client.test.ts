import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ApplicationError } from "../src/application/app-error.js";
import type { LoginIdentity } from "../src/infrastructure/auth/login-provider.js";
import {
  OAuthLoginClient,
  assertOAuthConfiguredOwner,
  loadOAuthLoginConfig,
  validateOAuthLoginConfig,
  type OAuthLoginConfig,
} from "../src/infrastructure/auth/oauth-login-client.js";

const providers = ["github", "linuxdo"] as const;
const exchangeInput = {
  code: "one-time-private-code",
  codeVerifier: "server-held-code-verifier",
  nonceHash: "server-held-nonce-hash",
  now: new Date("2026-10-11T09:00:00Z"),
};
const tokenBody = {
  access_token: "access-token-must-stay-private",
  token_type: "Bearer",
  refresh_token: "refresh-token-must-not-be-used",
};

function configuration(provider: OAuthLoginConfig["provider"]): OAuthLoginConfig {
  return {
    provider,
    clientId: `${provider}-test-client`,
    clientSecret: `${provider}-client-secret-must-stay-private`,
    redirectUri: `https://lyra.example/v1/auth/${provider}/callback`,
    ownerSubject: "42",
  };
}

function environment(config: OAuthLoginConfig): Record<string, string> {
  const provider = config.provider.toUpperCase();
  return {
    [`LYRA_LOGIN_${provider}_CLIENT_ID`]: config.clientId,
    [`LYRA_LOGIN_${provider}_CLIENT_SECRET`]: config.clientSecret,
    [`LYRA_LOGIN_${provider}_REDIRECT_URI`]: config.redirectUri,
    [`LYRA_OWNER_${provider}_SUBJECT`]: config.ownerSubject,
  };
}

function profileClient(provider: OAuthLoginConfig["provider"], profile: unknown): OAuthLoginClient {
  let request = 0;
  return new OAuthLoginClient(configuration(provider), {
    fetch: async () => Response.json(request++ === 0 ? tokenBody : profile),
  });
}

function failedWithoutSecrets(error: unknown): boolean {
  assert.ok(error instanceof ApplicationError);
  assert.equal(error.code, "LOGIN_FAILED");
  assert.equal(error.statusCode, 401);
  assert.equal(error.retryable, false);
  assert.equal(error.cause, undefined);
  assert.equal(error.details, null);
  const publicError = `${String(error)} ${JSON.stringify(error)}`;
  for (const secret of [
    exchangeInput.code,
    exchangeInput.codeVerifier,
    tokenBody.access_token,
    tokenBody.refresh_token,
    "raw-upstream-secret",
    configuration("github").clientSecret,
    configuration("linuxdo").clientSecret,
  ]) {
    assert.equal(publicError.includes(secret), false);
  }
  return true;
}

describe("GitHub and LINUX DO login OAuth boundaries", () => {
  it("keeps unconfigured providers disabled and rejects each partial private-owner configuration", () => {
    for (const provider of providers) {
      const config = configuration(provider);
      assert.equal(loadOAuthLoginConfig({}, provider), null);
      assert.equal(loadOAuthLoginConfig({ LYRA_OWNER_EMAIL: "owner@example.com" }, provider), null);
      assert.deepEqual(loadOAuthLoginConfig(environment(config), provider), config);
      for (const name of Object.keys(environment(config))) {
        assert.throws(() => loadOAuthLoginConfig({ ...environment(config), [name]: " " }, provider), RangeError);
        assert.throws(() => loadOAuthLoginConfig({ [name]: environment(config)[name] }, provider), RangeError);
      }
      const padded = Object.fromEntries(Object.entries(environment(config)).map(([name, value]) => [name, ` ${value} `]));
      assert.deepEqual(loadOAuthLoginConfig(padded, provider), config);
      assert.equal(loadOAuthLoginConfig(environment(configuration(provider === "github" ? "linuxdo" : "github")), provider), null);
    }
  });

  it("requires a canonical, safe positive owner ID and exact HTTPS callback without credentials or query", () => {
    for (const provider of providers) {
      const config = configuration(provider);
      for (const ownerSubject of ["", "0", "-1", "+42", "042", "42.0", "4.2e1", "owner", "owner@example.com", "9007199254740992"]) {
        assert.throws(() => validateOAuthLoginConfig({ ...config, ownerSubject }), RangeError);
      }
      for (const redirectUri of [
        `http://lyra.example/v1/auth/${provider}/callback`,
        `http://localhost.evil.example/v1/auth/${provider}/callback`,
        `https://raw-upstream-secret@lyra.example/v1/auth/${provider}/callback`,
        `${config.redirectUri}?next=https://attacker.example`,
        `${config.redirectUri}#raw-upstream-secret`,
        `${config.redirectUri}?`,
        `${config.redirectUri}#`,
        `https://lyra.example/v1/auth/${provider === "github" ? "linuxdo" : "github"}/callback`,
        "not-a-url-raw-upstream-secret",
      ]) {
        assert.throws(() => validateOAuthLoginConfig({ ...config, redirectUri }), (error: unknown) => {
          assert.ok(error instanceof RangeError);
          assert.equal(String(error).includes("raw-upstream-secret"), false);
          assert.equal(error.cause, undefined);
          return true;
        });
      }
      for (const host of ["localhost", "127.0.0.1", "[::1]"]) {
        assert.doesNotThrow(() => validateOAuthLoginConfig({
          ...config, redirectUri: `http://${host}:3000/v1/auth/${provider}/callback`,
        }));
      }
    }
  });

  for (const provider of providers) {
    it(`${provider} sends only its documented authorization parameters and fixed callback`, () => {
      const config = configuration(provider);
      const client = new OAuthLoginClient(config);
      const url = new URL(client.authorizationUrl({ state: "state & bound", nonce: "unused-oidc-nonce", codeChallenge: "pkce-challenge" }));
      assert.equal(url.origin + url.pathname, provider === "github"
        ? "https://github.com/login/oauth/authorize"
        : "https://connect.linux.do/oauth2/authorize");
      assert.equal(url.searchParams.get("client_id"), config.clientId);
      assert.equal(url.searchParams.get("redirect_uri"), config.redirectUri);
      assert.equal(url.searchParams.get("response_type"), "code");
      assert.equal(url.searchParams.get("scope"), provider === "github" ? "read:user" : "user");
      assert.equal(url.searchParams.get("state"), "state & bound");
      assert.equal(url.searchParams.get("code_challenge"), provider === "github" ? "pkce-challenge" : null);
      assert.equal(url.searchParams.get("code_challenge_method"), provider === "github" ? "S256" : null);
      for (const field of ["nonce", "client_secret", "ownerSubject", "access_type", "login", "user:email"]) {
        assert.equal(url.searchParams.has(field), false);
      }
      assert.equal(url.toString().includes(config.clientSecret), false);
    });

    it(`${provider} exchanges server-held credentials then verifies identity without returning tokens or email`, async () => {
      const config = configuration(provider);
      const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
      const client = new OAuthLoginClient(config, {
        fetch: async (input, init) => {
          calls.push({ url: input.toString(), init });
          return Response.json(calls.length === 1 ? tokenBody : {
            id: 42,
            name: " Owner ",
            login: "github-owner",
            username: "linux-owner",
            email: "untrusted@example.com",
            email_verified: true,
            active: true,
            silenced: false,
            api_key: "raw-upstream-secret",
            external_ids: { google: "must-not-link" },
          });
        },
      });
      const identity = await client.exchangeCode(exchangeInput);
      assert.deepEqual(identity, { provider, subject: "42", email: null, displayName: "Owner" });
      assert.equal(calls.length, 2);
      assert.equal(calls[0]?.url, provider === "github"
        ? "https://github.com/login/oauth/access_token"
        : "https://connect.linux.do/oauth2/token");
      assert.equal(calls[1]?.url, provider === "github" ? "https://api.github.com/user" : "https://connect.linux.do/api/user");
      assert.equal(calls[0]?.init?.method, "POST");
      assert.equal(calls[1]?.init?.method, "GET");
      const form = calls[0]?.init?.body;
      assert.ok(form instanceof URLSearchParams);
      assert.deepEqual(Object.fromEntries(form), {
        grant_type: "authorization_code",
        code: exchangeInput.code,
        client_id: config.clientId,
        client_secret: config.clientSecret,
        redirect_uri: config.redirectUri,
        ...(provider === "github" ? { code_verifier: exchangeInput.codeVerifier } : {}),
      });
      assert.equal(new Headers(calls[0]?.init?.headers).get("accept"), "application/json");
      assert.equal(new Headers(calls[0]?.init?.headers).get("content-type"), "application/x-www-form-urlencoded");
      const userHeaders = new Headers(calls[1]?.init?.headers);
      assert.equal(userHeaders.get("authorization"), `Bearer ${tokenBody.access_token}`);
      assert.equal(userHeaders.get("user-agent"), "Lyra");
      assert.equal(userHeaders.get("x-github-api-version"), provider === "github" ? "2026-03-10" : null);
      for (const call of calls) {
        assert.equal(call.init?.redirect, "error");
        assert.equal(call.init?.credentials, "omit");
        assert.equal(call.init?.cache, "no-store");
        assert.ok(call.init.signal instanceof AbortSignal);
      }
      assert.equal(calls[0]?.init?.signal, calls[1]?.init?.signal);
    });

    it(`${provider} accepts private or absent email and changing names without changing owner identity`, async () => {
      for (const profile of [
        { id: 42, name: null, login: "new-username", username: "new-username", email: null },
        { id: 42, name: " ", login: "new-username", username: "new-username" },
        { id: 42, name: "new-username", email: "claimed@example.com", email_verified: false },
      ]) {
        const identity = await profileClient(provider, profile).exchangeCode(exchangeInput);
        assert.equal(identity.subject, "42");
        assert.equal(identity.email, null);
        assert.equal(identity.displayName, "new-username");
        assert.doesNotThrow(() => assertOAuthConfiguredOwner(identity, configuration(provider)));
      }
    });

    it(`${provider} rejects fabricated, fractional, string, nonpositive and unsafe numeric identities`, async () => {
      for (const id of [null, undefined, "42", 0, -1, 42.5, Number.MAX_SAFE_INTEGER + 1, true]) {
        await assert.rejects(profileClient(provider, { id, name: "Owner" }).exchangeCode(exchangeInput), failedWithoutSecrets);
      }
      await assert.rejects(profileClient(provider, { id: 42, error: "raw-upstream-secret" }).exchangeCode(exchangeInput), failedWithoutSecrets);
      for (const profile of [null, [], "raw-upstream-secret"]) {
        await assert.rejects(profileClient(provider, profile).exchangeCode(exchangeInput), failedWithoutSecrets);
      }
    });
  }

  it("never authorizes another provider or another ID by matching the configured owner's display name or email", () => {
    for (const provider of providers) {
      const identity: LoginIdentity = { provider, subject: "42", displayName: "Owner", email: "owner@example.com" };
      const config = configuration(provider);
      assert.doesNotThrow(() => assertOAuthConfiguredOwner(identity, config));
      for (const invalid of [
        { ...identity, subject: "43" },
        { ...identity, subject: "042" },
        { ...identity, provider: "google" as const },
        { ...identity, provider: provider === "github" ? "linuxdo" as const : "github" as const },
      ]) {
        assert.throws(() => assertOAuthConfiguredOwner(invalid, config), (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "LOGIN_DENIED");
          assert.equal(error.statusCode, 403);
          assert.equal(error.message.includes("owner@example.com"), false);
          assert.equal(error.message.includes("42"), false);
          return true;
        });
      }
    }
  });

  it("rejects explicit inactive or malformed LINUX DO status but does not equate forum silence to login denial", async () => {
    for (const fields of [{ active: false }, { active: "true" }, { active: null }, { silenced: "false" }, { silenced: null }]) {
      await assert.rejects(profileClient("linuxdo", { id: 42, ...fields }).exchangeCode(exchangeInput), failedWithoutSecrets);
    }
    for (const fields of [{}, { active: true }, { active: true, silenced: true }, { silenced: false }]) {
      assert.equal((await profileClient("linuxdo", { id: 42, ...fields }).exchangeCode(exchangeInput)).subject, "42");
    }
  });

  it("rejects token errors, malformed bearer tokens and malformed JSON without making an identity request", async () => {
    for (const payload of [
      { ...tokenBody, error: "raw-upstream-secret" },
      { ...tokenBody, token_type: "Basic" },
      { access_token: tokenBody.access_token },
      { token_type: "bearer" },
      { ...tokenBody, access_token: "" },
      { ...tokenBody, access_token: "token\r\nX-Header: raw-upstream-secret" },
      { ...tokenBody, access_token: "a".repeat(8_195) },
      [],
      null,
    ]) {
      let requests = 0;
      const client = new OAuthLoginClient(configuration("github"), {
        fetch: async () => { requests += 1; return Response.json(payload); },
      });
      await assert.rejects(client.exchangeCode(exchangeInput), failedWithoutSecrets);
      assert.equal(requests, 1);
    }
    const malformed = new OAuthLoginClient(configuration("linuxdo"), {
      fetch: async () => new Response("not-json raw-upstream-secret"),
    });
    await assert.rejects(malformed.exchangeCode(exchangeInput), failedWithoutSecrets);
  });

  it("rejects redirects at either endpoint and drops upstream HTTP/network/abort error content", async () => {
    for (const provider of providers) {
      for (const failingRequest of [1, 2]) {
        for (const status of [302, 401, 429, 500]) {
          let requests = 0;
          const client = new OAuthLoginClient(configuration(provider), {
            fetch: async (_input, init) => {
              assert.equal(init?.redirect, "error");
              requests += 1;
              return requests === failingRequest
                ? new Response("raw-upstream-secret", { status, headers: { location: "https://attacker.example/collect" } })
                : Response.json(tokenBody);
            },
          });
          await assert.rejects(client.exchangeCode(exchangeInput), failedWithoutSecrets);
          assert.equal(requests, failingRequest);
        }
      }
      for (const error of [new Error("raw-upstream-secret"), new DOMException("raw-upstream-secret", "TimeoutError")]) {
        const client = new OAuthLoginClient(configuration(provider), { fetch: async () => { throw error; } });
        await assert.rejects(client.exchangeCode(exchangeInput), failedWithoutSecrets);
      }
      const followed = new OAuthLoginClient(configuration(provider), {
        fetch: async () => {
          const response = Response.json(tokenBody);
          Object.defineProperty(response, "redirected", { value: true });
          return response;
        },
      });
      await assert.rejects(followed.exchangeCode(exchangeInput), failedWithoutSecrets);
    }
  });

  it("caps streamed bytes at both endpoints even when Content-Length understates the response, and cancels the stream", async () => {
    for (const failingRequest of [1, 2]) {
      let requests = 0;
      let cancelled = false;
      const client = new OAuthLoginClient(configuration("github"), {
        fetch: async () => {
          requests += 1;
          if (requests !== failingRequest) return Response.json(tokenBody);
          return new Response(new ReadableStream<Uint8Array>({
            start(controller) { controller.enqueue(new Uint8Array(64 * 1_024 + 1)); },
            cancel() { cancelled = true; },
          }), { headers: { "content-length": "1" } });
        },
      });
      await assert.rejects(client.exchangeCode(exchangeInput), failedWithoutSecrets);
      assert.equal(cancelled, true);
      assert.equal(requests, failingRequest);
    }
    for (const response of [
      new Response("{}", { headers: { "content-length": "65537" } }),
      new Response(Uint8Array.of(0xff, 0xfe)),
      new Response(null, { status: 204 }),
    ]) {
      const client = new OAuthLoginClient(configuration("linuxdo"), { fetch: async () => response });
      await assert.rejects(client.exchangeCode(exchangeInput), failedWithoutSecrets);
    }
  });
});
