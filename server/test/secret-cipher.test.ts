import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  SecretCipher,
  SecretDecryptionError,
  parseSecretEncryptionKey,
} from "../src/infrastructure/security/index.js";

describe("SecretCipher", () => {
  it("使用随机 IV 和连接上下文完成 AES-256-GCM 往返", () => {
    const keyText = Buffer.alloc(32, 7).toString("base64url");
    const cipher = new SecretCipher(parseSecretEncryptionKey(keyText));
    const token = "google-refresh-token-sensitive";
    const context = "gmail-connection:connection-1";

    const first = cipher.encrypt(token, context);
    const second = cipher.encrypt(token, context);

    assert.notEqual(first, second);
    assert.equal(first.includes(token), false);
    assert.equal(cipher.decrypt(first, context), token);
    assert.equal(cipher.decrypt(second, context), token);
  });

  it("拒绝错误上下文、篡改密文和非 256 位主密钥", () => {
    const cipher = new SecretCipher(Buffer.alloc(32, 3));
    const envelope = cipher.encrypt("refresh-token", "gmail-connection:one");
    const parts = envelope.split(".");
    const authTag = parts[3];
    assert.notEqual(authTag, undefined);
    if (authTag === undefined) {
      throw new Error("测试密文缺少认证标签");
    }
    parts[3] = `${authTag.startsWith("A") ? "B" : "A"}${authTag.slice(1)}`;

    assert.throws(
      () => cipher.decrypt(envelope, "gmail-connection:two"),
      SecretDecryptionError,
    );
    assert.throws(
      () => cipher.decrypt(parts.join("."), "gmail-connection:one"),
      SecretDecryptionError,
    );
    assert.throws(() => new SecretCipher(Buffer.alloc(16)), RangeError);
  });
});
