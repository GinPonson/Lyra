import type { DeviceAccessTokenRecord } from "../security/device-auth.js";
import type { PairedDeviceAuthRecord } from "../security/device-auth.js";
import type { LyraDatabase } from "./database.js";

function parseTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new Error(`${label}必须是时间字符串`);
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    throw new Error(`${label}无效`);
  }
  return timestamp.toISOString();
}

function parseTokenStatus(value: unknown): "active" | "revoked" {
  if (value === "active" || value === "revoked") {
    return value;
  }
  throw new Error("设备访问令牌状态与数据库 schema 不一致");
}

function parseDeviceStatus(value: unknown): "active" | "revoked" {
  if (value === "active" || value === "revoked") {
    return value;
  }
  throw new Error("配对设备状态与数据库 schema 不一致");
}

/** 按令牌哈希恢复访问令牌记录；数据库只保存哈希，不保存明文。 */
export async function loadDeviceAccessTokenRecord(
  database: LyraDatabase,
  tokenHash: string,
): Promise<DeviceAccessTokenRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      CAST(device_id AS VARCHAR) AS device_id,
      token_hash,
      status,
      expires_at,
      last_seen_at,
      revoked_at,
      issued_at
    FROM device_access_tokens
    WHERE token_hash = $token_hash
  `, { token_hash: tokenHash });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined) {
    return null;
  }
  return {
    id: requireText(row.id, "访问令牌 ID"),
    deviceId: requireText(row.device_id, "访问令牌设备 ID"),
    tokenHash: requireText(row.token_hash, "访问令牌哈希"),
    status: parseTokenStatus(row.status),
    expiresAt: parseTimestamp(row.expires_at, "访问令牌过期时间"),
    lastSeenAt: row.last_seen_at === null
      ? null
      : parseTimestamp(row.last_seen_at, "访问令牌最近使用时间"),
    revokedAt: row.revoked_at === null
      ? null
      : parseTimestamp(row.revoked_at, "访问令牌撤销时间"),
    issuedAt: parseTimestamp(row.issued_at, "访问令牌签发时间"),
  };
}

/** 按设备 ID 恢复配对设备认证记录；不含公钥等非认证字段。 */
export async function loadPairedDeviceAuthRecord(
  database: LyraDatabase,
  deviceId: string,
): Promise<PairedDeviceAuthRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      status,
      revoked_at
    FROM paired_devices
    WHERE id = CAST($device_id AS UUID)
  `, { device_id: deviceId });
  const row = reader.getRowObjectsJson()[0];
  if (row === undefined) {
    return null;
  }
  return {
    id: requireText(row.id, "配对设备 ID"),
    status: parseDeviceStatus(row.status),
    revokedAt: row.revoked_at === null
      ? null
      : parseTimestamp(row.revoked_at, "设备撤销时间"),
  };
}

function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label}与数据库 schema 不一致`);
  }
  return value;
}
