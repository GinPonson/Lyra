import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from "node:crypto";

/** AES-GCM 推荐的 96 位随机 nonce 长度。 */
const IV_BYTES = 12;

/** AES-256-GCM 认证标签长度。 */
const AUTH_TAG_BYTES = 16;

/** 当前持久化密文格式版本。 */
const ENVELOPE_VERSION = "v1";

/** 密文格式、上下文或认证标签无法通过验证。 */
export class SecretDecryptionError extends Error {
  /**
   * @param message - 不包含密钥、令牌或原始密文的安全错误说明。
   * @param cause - 底层加密库错误，仅供服务端诊断链使用。
   */
  constructor(message: string, cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "SecretDecryptionError";
  }
}

/**
 * 加密 Gmail refresh token 等仅服务端可读的持久化秘密。
 *
 * 每次加密生成独立随机 IV，并使用调用方提供的连接上下文作为 AAD。数据库中
 * 即使发生密文串换，错误连接 ID 也无法完成认证解密。
 */
export class SecretCipher {
  private readonly key: Buffer;

  /**
   * @param key - 恰好 32 字节的 AES-256 主密钥；构造时复制，调用方后续修改无效。
   */
  constructor(key: Uint8Array) {
    if (key.byteLength !== 32) {
      throw new RangeError("AES-256 主密钥必须恰好为 32 字节");
    }
    this.key = Buffer.from(key);
  }

  /**
   * @param plaintext - 待加密的 refresh token 或其他秘密。
   * @param context - 稳定且不敏感的 AAD，例如 `gmail-connection:<uuid>`。
   * @returns 可直接存入 VARCHAR 的版本化密文信封。
   */
  encrypt(plaintext: string, context: string): string {
    if (plaintext.length === 0 || context.trim().length === 0) {
      throw new RangeError("待加密秘密和加密上下文不能为空");
    }
    const iv = randomBytes(IV_BYTES);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv, {
      authTagLength: AUTH_TAG_BYTES,
    });
    cipher.setAAD(Buffer.from(context, "utf8"));
    const ciphertext = Buffer.concat([
      cipher.update(plaintext, "utf8"),
      cipher.final(),
    ]);
    const authTag = cipher.getAuthTag();
    return [
      ENVELOPE_VERSION,
      iv.toString("base64url"),
      ciphertext.toString("base64url"),
      authTag.toString("base64url"),
    ].join(".");
  }

  /**
   * @param envelope - `encrypt` 生成的版本化密文信封。
   * @param context - 必须与加密时完全一致的连接上下文。
   * @returns 通过 GCM 认证后的 UTF-8 明文。
   * @throws SecretDecryptionError 当格式、版本、长度、上下文或认证标签无效时抛出。
   */
  decrypt(envelope: string, context: string): string {
    const [version, ivRaw, ciphertextRaw, authTagRaw, extra] = envelope.split(".");
    if (
      version !== ENVELOPE_VERSION
      || ivRaw === undefined
      || ciphertextRaw === undefined
      || authTagRaw === undefined
      || extra !== undefined
      || context.trim().length === 0
    ) {
      throw new SecretDecryptionError("加密秘密的信封格式或版本无效");
    }

    const iv = decodeBase64Url(ivRaw, "IV");
    const ciphertext = decodeBase64Url(ciphertextRaw, "ciphertext");
    const authTag = decodeBase64Url(authTagRaw, "auth tag");
    if (iv.byteLength !== IV_BYTES || authTag.byteLength !== AUTH_TAG_BYTES) {
      throw new SecretDecryptionError("加密秘密的 IV 或认证标签长度无效");
    }

    try {
      const decipher = createDecipheriv("aes-256-gcm", this.key, iv, {
        authTagLength: AUTH_TAG_BYTES,
      });
      decipher.setAAD(Buffer.from(context, "utf8"));
      decipher.setAuthTag(authTag);
      return Buffer.concat([
        decipher.update(ciphertext),
        decipher.final(),
      ]).toString("utf8");
    } catch (error: unknown) {
      throw new SecretDecryptionError("加密秘密认证失败", error);
    }
  }
}

/**
 * 把部署配置中的 Base64URL 主密钥解析为 32 字节。
 *
 * @param encoded - 不带填充的 Base64URL 字符串。
 * @returns 可传给 `SecretCipher` 的独立 Buffer。
 */
export function parseSecretEncryptionKey(encoded: string): Buffer {
  if (!/^[A-Za-z0-9_-]+$/.test(encoded) || encoded.length % 4 === 1) {
    throw new RangeError("秘密加密主密钥不是有效的 Base64URL");
  }
  const key = Buffer.from(encoded, "base64url");
  if (key.byteLength !== 32) {
    throw new RangeError("秘密加密主密钥解码后必须恰好为 32 字节");
  }
  return key;
}

/** 严格解码密文信封中的 Base64URL 段。 */
function decodeBase64Url(value: string, label: string): Buffer {
  if (!/^[A-Za-z0-9_-]*$/.test(value) || value.length % 4 === 1) {
    throw new SecretDecryptionError(`加密秘密的 ${label} 不是有效 Base64URL`);
  }
  return Buffer.from(value, "base64url");
}
