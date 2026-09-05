import type {
  DeviceRefreshCredentialRecord,
  PairedDeviceRecord,
  RefreshCredentialFamilyRevocationPlan,
  RotatedDeviceRefreshCredential,
} from "../security/device-auth.js";
import { ApplicationError } from "../../application/app-error.js";
import type { LyraDatabase } from "./database.js";

/** 创建已配对设备记录；公钥指纹唯一约束由数据库把关。 */
export async function createPairedDeviceRow(
  database: LyraDatabase,
  record: PairedDeviceRecord,
): Promise<void> {
  await database.connection.run(`
    INSERT INTO paired_devices (
      id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
      status, paired_at, last_seen_at, revoked_at, updated_at
    ) VALUES (
      CAST($id AS UUID), $display_name, $public_key_pem, $public_key_fingerprint,
      $key_algorithm, $status, CAST($paired_at AS TIMESTAMPTZ), $last_seen_at,
      $revoked_at, CAST($updated_at AS TIMESTAMPTZ)
    )
  `, {
    id: record.id,
    display_name: record.displayName,
    public_key_pem: record.publicKeyPem,
    public_key_fingerprint: record.publicKeyFingerprint,
    key_algorithm: record.keyAlgorithm,
    status: record.status,
    paired_at: record.pairedAt,
    last_seen_at: record.lastSeenAt,
    revoked_at: record.revokedAt,
    updated_at: record.updatedAt,
  });
}

/** 持久化一枚新代次的刷新凭据；只保存哈希。 */
export async function createDeviceRefreshCredentialRow(
  database: LyraDatabase,
  record: DeviceRefreshCredentialRecord,
): Promise<void> {
  await database.connection.run(`
    INSERT INTO device_refresh_credentials (
      id, device_id, family_id, generation, token_hash, status,
      expires_at, last_used_at, rotated_at, revoked_at, created_at
    ) VALUES (
      CAST($id AS UUID), CAST($device_id AS UUID), CAST($family_id AS UUID),
      $generation, $token_hash, $status, CAST($expires_at AS TIMESTAMPTZ),
      $last_used_at, $rotated_at, $revoked_at, CAST($created_at AS TIMESTAMPTZ)
    )
  `, {
    id: record.id,
    device_id: record.deviceId,
    family_id: record.familyId,
    generation: record.generation,
    token_hash: record.tokenHash,
    status: record.status,
    expires_at: record.expiresAt,
    last_used_at: record.lastUsedAt,
    rotated_at: record.rotatedAt,
    revoked_at: record.revokedAt,
    created_at: record.createdAt,
  });
}

/** 持久化一枚短期访问令牌；只保存哈希。 */
export async function createDeviceAccessTokenRow(
  database: LyraDatabase,
  record: {
    id: string;
    deviceId: string;
    tokenHash: string;
    expiresAt: string;
    issuedAt: string;
  },
): Promise<void> {
  await database.connection.run(`
    INSERT INTO device_access_tokens (
      id, device_id, token_hash, status, expires_at, issued_at
    ) VALUES (
      CAST($id AS UUID), CAST($device_id AS UUID), $token_hash, 'active',
      CAST($expires_at AS TIMESTAMPTZ), CAST($issued_at AS TIMESTAMPTZ)
    )
  `, {
    id: record.id,
    device_id: record.deviceId,
    token_hash: record.tokenHash,
    expires_at: record.expiresAt,
    issued_at: record.issuedAt,
  });
}

/** 按令牌哈希读取刷新凭据；不存在返回 null。 */
export async function loadDeviceRefreshCredentialRow(
  database: LyraDatabase,
  tokenHash: string,
): Promise<DeviceRefreshCredentialRecord | null> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      CAST(device_id AS VARCHAR) AS device_id,
      CAST(family_id AS VARCHAR) AS family_id,
      generation,
      token_hash,
      status,
      expires_at,
      last_used_at,
      rotated_at,
      revoked_at,
      created_at
    FROM device_refresh_credentials
    WHERE token_hash = $token_hash
  `, { token_hash: tokenHash });
  const row = reader.getRowObjectsJson()[0];
  return row === undefined ? null : parseRefreshCredentialRow(row);
}

/**
 * 在单事务中完成刷新轮换：旧代以 CAS 标记 rotated，新代插入。
 *
 * 并发轮换或旧令牌重放都会让 CAS 失配，由调用方按凭据重用流程收口。
 */
export async function rotateDeviceRefreshCredentialRows(
  database: LyraDatabase,
  rotation: RotatedDeviceRefreshCredential,
): Promise<void> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    const rotated = await connection.run(`
      UPDATE device_refresh_credentials
      SET status = 'rotated',
          last_used_at = CAST($last_used_at AS TIMESTAMPTZ),
          rotated_at = CAST($rotated_at AS TIMESTAMPTZ)
      WHERE id = CAST($id AS UUID)
        AND status = 'active'
        AND generation = $generation
    `, {
      id: rotation.previous.id,
      generation: rotation.previous.generation,
      last_used_at: rotation.previous.lastUsedAt,
      rotated_at: rotation.previous.rotatedAt,
    });
    if (rotated.rowsChanged !== 1) {
      throw new ApplicationError({
        code: "DEVICE_CREDENTIAL_REUSED",
        message: "设备刷新凭据已使用或撤销",
        statusCode: 401,
        retryable: false,
      });
    }
    await createDeviceRefreshCredentialRow(database, rotation.next);
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "刷新凭据轮换失败且回滚失败");
    }
    throw error;
  }
}

/** 读取一个凭据族的全部代次；用于重用检测后的同族撤销。 */
export async function loadDeviceRefreshCredentialFamily(
  database: LyraDatabase,
  deviceId: string,
  familyId: string,
): Promise<DeviceRefreshCredentialRecord[]> {
  const reader = await database.connection.runAndReadAll(`
    SELECT
      CAST(id AS VARCHAR) AS id,
      CAST(device_id AS VARCHAR) AS device_id,
      CAST(family_id AS VARCHAR) AS family_id,
      generation,
      token_hash,
      status,
      expires_at,
      last_used_at,
      rotated_at,
      revoked_at,
      created_at
    FROM device_refresh_credentials
    WHERE device_id = CAST($device_id AS UUID)
      AND family_id = CAST($family_id AS UUID)
    ORDER BY generation
  `, { device_id: deviceId, family_id: familyId });
  return reader.getRowObjectsJson().map(parseRefreshCredentialRow);
}

/** 在单事务中应用同族全代次撤销计划；已撤销记录幂等保持。 */
export async function applyRefreshCredentialFamilyRevocation(
  database: LyraDatabase,
  plan: RefreshCredentialFamilyRevocationPlan,
): Promise<void> {
  const connection = database.connection;
  await connection.run("BEGIN TRANSACTION");
  try {
    for (const credential of plan.credentials) {
      await connection.run(`
        UPDATE device_refresh_credentials
        SET status = 'revoked',
            last_used_at = CAST($last_used_at AS TIMESTAMPTZ),
            revoked_at = CAST($revoked_at AS TIMESTAMPTZ)
        WHERE id = CAST($id AS UUID)
          AND status <> 'revoked'
      `, {
        id: credential.id,
        last_used_at: credential.lastUsedAt,
        revoked_at: credential.revokedAt,
      });
    }
    await connection.run("COMMIT");
  } catch (error: unknown) {
    try {
      await connection.run("ROLLBACK");
    } catch (rollbackError: unknown) {
      throw new AggregateError([error, rollbackError], "凭据族撤销失败且回滚失败");
    }
    throw error;
  }
}

function parseRefreshCredentialRow(
  row: Readonly<Record<string, unknown>>,
): DeviceRefreshCredentialRecord {
  const status = row.status;
  if (
    status !== "active" && status !== "rotated" && status !== "revoked"
  ) {
    throw new Error("刷新凭据状态与数据库 schema 不一致");
  }
  return {
    id: requireText(row.id, "刷新凭据 ID"),
    deviceId: requireText(row.device_id, "刷新凭据设备 ID"),
    familyId: requireText(row.family_id, "刷新凭据族 ID"),
    generation: Number(row.generation),
    tokenHash: requireText(row.token_hash, "刷新凭据哈希"),
    status,
    expiresAt: parseTimestamp(row.expires_at, "刷新凭据过期时间"),
    lastUsedAt: row.last_used_at === null
      ? null
      : parseTimestamp(row.last_used_at, "刷新凭据最近使用时间"),
    rotatedAt: row.rotated_at === null
      ? null
      : parseTimestamp(row.rotated_at, "刷新凭据轮换时间"),
    revokedAt: row.revoked_at === null
      ? null
      : parseTimestamp(row.revoked_at, "刷新凭据撤销时间"),
    createdAt: parseTimestamp(row.created_at, "刷新凭据创建时间"),
  };
}

/**
 * 把 DuckDB TIMESTAMPTZ 文本（如 "2026-09-05 10:08:23.879941+08"）规范为保留
 * 微秒的 UTC ISO 文本；微秒精度对凭据状态的乐观锁比较至关重要。
 */
function parseTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new Error(`${label}必须是时间字符串`);
  }
  const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:([+-])(\d{2})(?::?(\d{2}))?|Z|UTC)?$/.exec(
    value.trim(),
  );
  if (match === null) {
    throw new Error(`${label}无效`);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const fraction = match[7];
  const sign = match[8];
  const offsetHour = match[9] === undefined ? 0 : Number(match[9]);
  const offsetMinute = match[10] === undefined ? 0 : Number(match[10]);
  const offsetMinutes = sign === undefined || sign === "+"
    ? offsetHour * 60 + offsetMinute
    : -(offsetHour * 60 + offsetMinute);
  const utcMillis = Date.UTC(year, month - 1, day, hour, minute, second)
    - offsetMinutes * 60_000;
  const base = new Date(utcMillis);
  if (Number.isNaN(base.getTime())) {
    throw new Error(`${label}无效`);
  }
  const micros = (fraction ?? "0").padEnd(6, "0").slice(0, 6);
  return base.toISOString().replace(/\.\d{3}Z$/, `.${micros}Z`);
}

function requireText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label}与数据库 schema 不一致`);
  }
  return value;
}
