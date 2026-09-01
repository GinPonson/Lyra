import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { describe, it } from "node:test";

import {
  DeviceAuthError,
  authenticateDeviceAccessToken,
  consumePairingChallenge,
  createPairedDeviceRecord,
  createOpaqueCredential,
  createPairingChallenge,
  issueDeviceRefreshCredential,
  issueDeviceAccessToken,
  normalizeDevicePublicKey,
  planDeviceRevocation,
  planDeviceAccessTokenRevocation,
  planPairedDeviceActivityUpdate,
  planRefreshCredentialReuseRevocation,
  rotateDeviceRefreshCredential,
  verifyOpaqueCredential,
} from "../src/infrastructure/security/index.js";

describe("device pairing security", () => {
  it("生成短时配对码并一次性消费", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const challenge = createPairingChallenge(now, 5 * 60 * 1000);

    assert.match(challenge.code, /^[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}$/);
    assert.equal(challenge.record.codeHash.includes(challenge.code), false);
    const consumed = consumePairingChallenge(challenge.record, challenge.code, now);
    assert.equal(consumed.consumedAt, now.toISOString());
    assert.equal(challenge.record.consumedAt, null);
    assert.throws(
      () => consumePairingChallenge(consumed, challenge.code, now),
      (error: unknown) => error instanceof DeviceAuthError && error.code === "PAIRING_CODE_USED",
    );
  });

  it("区分错误和过期配对码", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const challenge = createPairingChallenge(now, 1_000);
    assert.throws(
      () => consumePairingChallenge(challenge.record, "AAAAA-AAAAA", now),
      (error: unknown) => error instanceof DeviceAuthError && error.code === "PAIRING_CODE_INVALID",
    );
    assert.throws(
      () => consumePairingChallenge(
        challenge.record,
        challenge.code,
        new Date("2026-08-30T00:00:01Z"),
      ),
      (error: unknown) => error instanceof DeviceAuthError && error.code === "PAIRING_CODE_EXPIRED",
    );
  });

  it("签发随机不透明凭据并只用哈希验证", () => {
    const first = createOpaqueCredential();
    const second = createOpaqueCredential();

    assert.notEqual(first.token, second.token);
    assert.notEqual(first.token, first.tokenHash);
    assert.equal(verifyOpaqueCredential(first.token, first.tokenHash), true);
    assert.equal(verifyOpaqueCredential(second.token, first.tokenHash), false);
  });

  it("规范化设备公钥并拒绝私钥上传", () => {
    const { publicKey, privateKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
    const publicPem = publicKey.export({ type: "spki", format: "pem" }).toString();
    const privatePem = privateKey.export({ type: "pkcs8", format: "pem" }).toString();
    const normalized = normalizeDevicePublicKey(publicPem);

    assert.equal(normalized.algorithm, "ec");
    assert.match(normalized.fingerprint, /^[a-f0-9]{64}$/);
    assert.equal(normalizeDevicePublicKey(normalized.pem).fingerprint, normalized.fingerprint);
    assert.throws(
      () => normalizeDevicePublicKey(privatePem),
      (error: unknown) => (
        error instanceof DeviceAuthError
        && error.code === "DEVICE_PUBLIC_KEY_INVALID"
      ),
    );
  });

  it("配对成功后创建不含私钥的活动设备持久化记录", () => {
    const { publicKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
    const normalized = normalizeDevicePublicKey(
      publicKey.export({ type: "spki", format: "pem" }).toString(),
    );
    const record = createPairedDeviceRecord(
      " device-1 ",
      " Gin 的 Pixel ",
      normalized,
      new Date("2026-08-31T06:00:00Z"),
    );

    assert.deepEqual(record, {
      id: "device-1",
      displayName: "Gin 的 Pixel",
      publicKeyPem: normalized.pem,
      publicKeyFingerprint: normalized.fingerprint,
      keyAlgorithm: "ec",
      status: "active",
      pairedAt: "2026-08-31T06:00:00.000Z",
      lastSeenAt: null,
      revokedAt: null,
      updatedAt: "2026-08-31T06:00:00.000Z",
    });
    assert.equal(JSON.stringify(record).includes("PRIVATE KEY"), false);
    assert.throws(
      () => createPairedDeviceRecord("device-2", " ", normalized, new Date()),
      RangeError,
    );
    assert.throws(
      () => createPairedDeviceRecord("device-2", "Pixel", normalized, new Date("invalid")),
      RangeError,
    );
  });

  it("签发并轮换刷新凭据且保持同一凭据族", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const issued = issueDeviceRefreshCredential("device-1", now, 86_400_000);
    const rotated = rotateDeviceRefreshCredential(
      { id: "device-1", status: "active", revokedAt: null },
      issued.record,
      issued.token,
      new Date("2026-08-30T01:00:00Z"),
      86_400_000,
    );

    assert.equal(rotated.previous.status, "rotated");
    assert.equal(rotated.next.status, "active");
    assert.equal(rotated.next.familyId, issued.record.familyId);
    assert.equal(rotated.next.generation, 2);
    assert.equal(verifyOpaqueCredential(rotated.token, rotated.next.tokenHash), true);
    assert.equal(rotated.next.tokenHash.includes(rotated.token), false);
  });

  it("拒绝过期、重用及已撤销设备的刷新请求", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const issued = issueDeviceRefreshCredential("device-1", now, 1_000);
    const activeDevice = { id: "device-1", status: "active" as const, revokedAt: null };

    assert.throws(
      () => rotateDeviceRefreshCredential(
        activeDevice,
        issued.record,
        issued.token,
        new Date("2026-08-30T00:00:01Z"),
        1_000,
      ),
      (error: unknown) => (
        error instanceof DeviceAuthError && error.code === "DEVICE_CREDENTIAL_EXPIRED"
      ),
    );

    const rotatedRecord = { ...issued.record, status: "rotated" as const };
    assert.throws(
      () => rotateDeviceRefreshCredential(activeDevice, rotatedRecord, issued.token, now, 1_000),
      (error: unknown) => (
        error instanceof DeviceAuthError && error.code === "DEVICE_CREDENTIAL_REUSED"
      ),
    );
    assert.throws(
      () => rotateDeviceRefreshCredential(
        { id: "device-1", status: "revoked", revokedAt: now.toISOString() },
        issued.record,
        issued.token,
        now,
        1_000,
      ),
      (error: unknown) => error instanceof DeviceAuthError && error.code === "DEVICE_REVOKED",
    );
  });

  it("检测到旧刷新凭据重用后撤销同一凭据族的全部代次", () => {
    const issued = issueDeviceRefreshCredential(
      "device-1",
      new Date("2026-08-30T00:00:00Z"),
      86_400_000,
    );
    const rotated = rotateDeviceRefreshCredential(
      { id: "device-1", status: "active", revokedAt: null },
      issued.record,
      issued.token,
      new Date("2026-08-30T01:00:00Z"),
      86_400_000,
    );
    const plan = planRefreshCredentialReuseRevocation(
      rotated.previous,
      [rotated.previous, rotated.next],
      new Date("2026-08-30T02:00:00Z"),
    );

    assert.equal(plan.deviceId, "device-1");
    assert.equal(plan.familyId, issued.record.familyId);
    assert.deepEqual(plan.credentials.map((credential) => credential.status), [
      "revoked",
      "revoked",
    ]);
    assert.deepEqual(plan.credentials.map((credential) => credential.revokedAt), [
      "2026-08-30T02:00:00.000Z",
      "2026-08-30T02:00:00.000Z",
    ]);
    assert.equal(rotated.previous.status, "rotated");
    assert.equal(rotated.next.status, "active");

    const repeated = planRefreshCredentialReuseRevocation(
      plan.credentials[0]!,
      plan.credentials,
      new Date("2026-08-30T03:00:00Z"),
    );
    assert.equal(repeated.credentials[0], plan.credentials[0]);
    assert.equal(repeated.credentials[1], plan.credentials[1]);
  });

  it("拒绝活动凭据触发以及跨设备或跨凭据族的重用撤销计划", () => {
    const issued = issueDeviceRefreshCredential(
      "device-1",
      new Date("2026-08-30T00:00:00Z"),
      86_400_000,
    );
    const reused = {
      ...issued.record,
      status: "rotated" as const,
      rotatedAt: "2026-08-30T01:00:00.000Z",
    };

    assert.throws(
      () => planRefreshCredentialReuseRevocation(
        issued.record,
        [issued.record],
        new Date("2026-08-30T02:00:00Z"),
      ),
      RangeError,
    );
    assert.throws(
      () => planRefreshCredentialReuseRevocation(
        reused,
        [reused, { ...issued.record, id: "other", deviceId: "device-2" }],
        new Date("2026-08-30T02:00:00Z"),
      ),
      RangeError,
    );
    assert.throws(
      () => planRefreshCredentialReuseRevocation(
        reused,
        [reused, { ...issued.record, id: "other", familyId: "family-2" }],
        new Date("2026-08-30T02:00:00Z"),
      ),
      RangeError,
    );
  });

  it("撤销设备时同时撤销仍有效的刷新凭据", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const issued = issueDeviceRefreshCredential("device-1", now, 86_400_000);
    const rotated = { ...issued.record, id: "old", status: "rotated" as const };
    const plan = planDeviceRevocation(
      { id: "device-1", status: "active", revokedAt: null },
      [issued.record, rotated],
      new Date("2026-08-30T01:00:00Z"),
    );

    assert.equal(plan.device.status, "revoked");
    assert.equal(plan.credentials[0]?.status, "revoked");
    assert.equal(plan.credentials[1]?.status, "rotated");
  });

  it("签发并认证短期访问令牌且只更新不可变副本", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const device = { id: "device-1", status: "active" as const, revokedAt: null };
    const issued = issueDeviceAccessToken(device, now, 15 * 60 * 1000);
    const authenticated = authenticateDeviceAccessToken(
      device,
      issued.record,
      issued.token,
      new Date("2026-08-30T00:01:00Z"),
    );

    assert.equal(verifyOpaqueCredential(issued.token, issued.record.tokenHash), true);
    assert.equal(issued.record.tokenHash.includes(issued.token), false);
    assert.equal(authenticated.lastSeenAt, "2026-08-30T00:01:00.000Z");
    assert.equal(issued.record.lastSeenAt, null);
  });

  it("成功认证后低频更新设备活动时间并保持时间单调", () => {
    const device = {
      id: "device-1",
      displayName: "Pixel",
      publicKeyPem: "PUBLIC KEY",
      publicKeyFingerprint: "a".repeat(64),
      keyAlgorithm: "ec",
      status: "active" as const,
      pairedAt: "2026-08-30T00:00:00.000Z",
      lastSeenAt: null,
      revokedAt: null,
      updatedAt: "2026-08-30T00:00:00.000Z",
    };
    const first = planPairedDeviceActivityUpdate(
      device,
      new Date("2026-08-30T00:01:00Z"),
    );
    assert.equal(first.lastSeenAt, "2026-08-30T00:01:00.000Z");
    assert.equal(device.lastSeenAt, null);

    const throttled = planPairedDeviceActivityUpdate(
      first,
      new Date("2026-08-30T00:03:00Z"),
    );
    assert.equal(throttled, first);
    const next = planPairedDeviceActivityUpdate(
      first,
      new Date("2026-08-30T00:06:00Z"),
    );
    assert.equal(next.lastSeenAt, "2026-08-30T00:06:00.000Z");
    assert.throws(
      () => planPairedDeviceActivityUpdate(
        { ...device, status: "revoked", revokedAt: "2026-08-30T00:02:00.000Z" },
        new Date("2026-08-30T00:03:00Z"),
      ),
      (error: unknown) => error instanceof DeviceAuthError
        && error.code === "DEVICE_REVOKED",
    );
  });

  it("拒绝过期和已撤销访问令牌并生成批量撤销计划", () => {
    const now = new Date("2026-08-30T00:00:00Z");
    const device = { id: "device-1", status: "active" as const, revokedAt: null };
    const issued = issueDeviceAccessToken(device, now, 1_000);

    assert.throws(
      () => authenticateDeviceAccessToken(
        device,
        issued.record,
        issued.token,
        new Date("2026-08-30T00:00:01Z"),
      ),
      (error: unknown) => (
        error instanceof DeviceAuthError && error.code === "DEVICE_CREDENTIAL_EXPIRED"
      ),
    );

    const revoked = planDeviceAccessTokenRevocation(
      "device-1",
      [issued.record],
      new Date("2026-08-30T00:02:00Z"),
    );
    assert.equal(revoked[0]?.status, "revoked");
    assert.throws(
      () => authenticateDeviceAccessToken(device, revoked[0]!, issued.token, now),
      (error: unknown) => (
        error instanceof DeviceAuthError && error.code === "DEVICE_CREDENTIAL_INVALID"
      ),
    );
  });
});
