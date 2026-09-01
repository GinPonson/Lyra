import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createPublicDeviceList } from "../src/application/index.js";
import type { PairedDeviceRecord } from "../src/infrastructure/security/index.js";

const DEVICES: readonly PairedDeviceRecord[] = [
  {
    id: "device-revoked",
    displayName: "旧手机",
    publicKeyPem: "SENSITIVE PUBLIC KEY 1",
    publicKeyFingerprint: "a".repeat(64),
    keyAlgorithm: "ec",
    status: "revoked",
    pairedAt: "2026-08-01T00:00:00.000Z",
    lastSeenAt: "2026-08-02T00:00:00.000Z",
    revokedAt: "2026-08-03T00:00:00.000Z",
    updatedAt: "2026-08-03T00:00:00.000Z",
  },
  {
    id: "device-new",
    displayName: "Pixel 10",
    publicKeyPem: "SENSITIVE PUBLIC KEY 2",
    publicKeyFingerprint: "b".repeat(64),
    keyAlgorithm: "ec",
    status: "active",
    pairedAt: "2026-08-31T00:00:00.000Z",
    lastSeenAt: null,
    revokedAt: null,
    updatedAt: "2026-08-31T00:00:00.000Z",
  },
  {
    id: "device-old",
    displayName: "Pixel 9",
    publicKeyPem: "SENSITIVE PUBLIC KEY 3",
    publicKeyFingerprint: "c".repeat(64),
    keyAlgorithm: "ec",
    status: "active",
    pairedAt: "2026-08-20T00:00:00.000Z",
    lastSeenAt: "2026-08-30T00:00:00.000Z",
    revokedAt: null,
    updatedAt: "2026-08-30T00:00:00.000Z",
  },
];

describe("public paired device list", () => {
  it("稳定展示设备状态且不公开公钥、指纹或算法", () => {
    const result = createPublicDeviceList(DEVICES);

    assert.deepEqual(result.map((device) => device.id), [
      "device-new",
      "device-old",
      "device-revoked",
    ]);
    assert.equal(result[1]?.lastSeenAt, "2026-08-30T00:00:00.000Z");
    const serialized = JSON.stringify(result);
    for (const forbidden of [
      "SENSITIVE PUBLIC KEY",
      "publicKeyPem",
      "publicKeyFingerprint",
      "keyAlgorithm",
    ]) {
      assert.equal(serialized.includes(forbidden), false);
    }
    assert.deepEqual(DEVICES.map((device) => device.id), [
      "device-revoked",
      "device-new",
      "device-old",
    ]);
  });
});
