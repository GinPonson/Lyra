import { ApplicationError } from "../../application/app-error.js";
import type { GoogleLoginIdentity } from "../auth/google-login-client.js";
import type { LyraDatabase } from "./database.js";

/** 必须在单写队列的凭据签发事务内调用；邮箱只允许首次绑定指定的 owner。 */
export async function claimServerOwnerIdentity(
  database: LyraDatabase,
  identity: GoogleLoginIdentity,
  now: Date,
): Promise<void> {
  const reader = await database.connection.runAndReadAll(
    "SELECT provider, subject FROM server_owner_identity WHERE singleton_key = 1",
  );
  const owner = reader.getRowObjectsJson()[0];
  if (owner !== undefined && (owner.provider !== identity.provider || owner.subject !== identity.subject)) {
    throw new ApplicationError({
      code: "LOGIN_DENIED",
      message: "此 Google 账户不是这台 Lyra 服务已绑定的所有者",
      statusCode: 403,
      retryable: false,
    });
  }
  if (owner === undefined) {
    await database.connection.run(`
      INSERT INTO server_owner_identity
        (singleton_key, provider, subject, email, display_name, created_at, updated_at)
      VALUES (1, $provider, $subject, $email, $display_name,
        CAST($now AS TIMESTAMPTZ), CAST($now AS TIMESTAMPTZ))
    `, {
      provider: identity.provider,
      subject: identity.subject,
      email: identity.email,
      display_name: identity.displayName,
      now: now.toISOString(),
    });
  } else {
    await database.connection.run(`
      UPDATE server_owner_identity SET email = $email, display_name = $display_name,
        updated_at = CAST($now AS TIMESTAMPTZ)
      WHERE singleton_key = 1
    `, { email: identity.email, display_name: identity.displayName, now: now.toISOString() });
  }
}

/** 同一安装的公钥指纹保持设备 ID；重新登录先撤销旧凭据再签发新族。 */
export async function findDeviceIdByPublicKey(
  database: LyraDatabase,
  fingerprint: string,
): Promise<string | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT CAST(id AS VARCHAR) AS id FROM paired_devices
    WHERE public_key_fingerprint = $fingerprint
  `, { fingerprint });
  const id = reader.getRowObjectsJson()[0]?.id;
  return typeof id === "string" ? id : null;
}

/** 调用者持有数据库事务与单写队列；同时失效短期 token 与每代 refresh。 */
export async function revokeDeviceCredentials(
  database: LyraDatabase,
  deviceId: string,
  now: Date,
): Promise<void> {
  const parameters = { device_id: deviceId, now: now.toISOString() };
  await database.connection.run(`
    UPDATE device_access_tokens SET status = 'revoked', revoked_at = CAST($now AS TIMESTAMPTZ)
    WHERE device_id = CAST($device_id AS UUID) AND status = 'active'
  `, parameters);
  await database.connection.run(`
    UPDATE device_refresh_credentials SET status = 'revoked', revoked_at = CAST($now AS TIMESTAMPTZ)
    WHERE device_id = CAST($device_id AS UUID) AND status <> 'revoked'
  `, parameters);
}
