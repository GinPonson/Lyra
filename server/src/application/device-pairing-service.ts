import { randomUUID } from "node:crypto";

import type {
  DeviceRefreshCredentialRecord,
  PairedDeviceAuthRecord,
} from "../infrastructure/security/device-auth.js";
import {
  createPairedDeviceRecord,
  issueDeviceAccessToken,
  issueDeviceRefreshCredential,
  normalizeDevicePublicKey,
  planRefreshCredentialReuseRevocation,
  rotateDeviceRefreshCredential,
  hashOpaqueCredential,
  DeviceAuthError,
} from "../infrastructure/security/device-auth.js";
import {
  applyRefreshCredentialFamilyRevocation,
  createDeviceAccessTokenRow,
  createDeviceRefreshCredentialRow,
  createPairedDeviceRow,
  loadDeviceRefreshCredentialRow,
  loadDeviceRefreshCredentialFamily,
  rotateDeviceRefreshCredentialRows,
} from "../infrastructure/db/device-pairing-repository.js";
import {
  claimServerOwnerIdentity,
  findDeviceIdByPublicKey,
  revokeDeviceCredentials,
} from "../infrastructure/db/owner-login-repository.js";
import type { LoginIdentity } from "../infrastructure/auth/login-provider.js";
import { loadPairedDeviceAuthRecord } from "../infrastructure/db/device-access-token-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";

const ACCESS_TOKEN_TTL_MILLISECONDS = 15 * 60 * 1000;
const REFRESH_TOKEN_TTL_MILLISECONDS = 30 * 24 * 60 * 60 * 1000;

/** OAuth 登录成功后 App 获得的 Lyra 凭据，不含任何第三方 token。 */
export interface DevicePairingHttpResponse {
  deviceId: string;
  displayName: string;
  accessToken: string;
  accessTokenExpiresAt: string;
  refreshToken: string;
  refreshTokenExpiresAt: string;
}

export interface DeviceTokenRefreshHttpResponse {
  accessToken: string;
  accessTokenExpiresAt: string;
  refreshToken: string;
  refreshTokenExpiresAt: string;
}

/**
 * OAuth owner 登录后的设备会话管理。保留已有 access/refresh 凭据格式；
 * 删除匿名配对码入口。所有签发、刷新与撤销在同一单写队列中执行。
 */
export class DevicePairingService {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #now: () => Date;

  constructor(input: {
    database: LyraDatabase;
    writeCoordinator: WriteCoordinator;
    now?: () => Date;
  }) {
    this.#database = input.database;
    this.#writeCoordinator = input.writeCoordinator;
    this.#now = input.now ?? (() => new Date());
  }

  /** 仅由已验证 OAuth + App PKCE 的应用用例调用；所有写入原子提交。 */
  async authorizeOwnerDevice(input: {
    identity: LoginIdentity;
    displayName: string;
    publicKeyPem: string;
  }): Promise<DevicePairingHttpResponse> {
    const publicKey = normalizeDevicePublicKey(input.publicKeyPem);
    return this.#writeCoordinator.enqueue(async () => {
      const now = this.#now();
      const connection = this.#database.connection;
      await connection.run("BEGIN TRANSACTION");
      try {
        await claimServerOwnerIdentity(this.#database, input.identity, now);
        const existingId = await findDeviceIdByPublicKey(this.#database, publicKey.fingerprint);
        const deviceId = existingId ?? randomUUID();
        const record = createPairedDeviceRecord(deviceId, input.displayName, publicKey, now);
        if (existingId === null) {
          await createPairedDeviceRow(this.#database, record);
        } else {
          await revokeDeviceCredentials(this.#database, deviceId, now);
          await connection.run(`
            UPDATE paired_devices SET display_name = $display_name,
              status = 'active', revoked_at = NULL,
              updated_at = greatest(updated_at, CAST($now AS TIMESTAMPTZ))
            WHERE id = CAST($id AS UUID)
          `, { id: deviceId, display_name: record.displayName, now: now.toISOString() });
        }
        const authRecord: PairedDeviceAuthRecord = { id: deviceId, status: "active", revokedAt: null };
        const refresh = issueDeviceRefreshCredential(deviceId, now, REFRESH_TOKEN_TTL_MILLISECONDS);
        const access = issueDeviceAccessToken(authRecord, now, ACCESS_TOKEN_TTL_MILLISECONDS);
        await createDeviceRefreshCredentialRow(this.#database, refresh.record);
        await createDeviceAccessTokenRow(this.#database, access.record);
        await connection.run("COMMIT");
        return {
          deviceId,
          displayName: record.displayName,
          accessToken: access.token,
          accessTokenExpiresAt: access.record.expiresAt,
          refreshToken: refresh.token,
          refreshTokenExpiresAt: refresh.record.expiresAt,
        };
      } catch (error: unknown) {
        await rollback(connection, error);
        throw error;
      }
    });
  }

  /** 读取、校验、轮换与 access 签发共用队列，避免刷新/退出登录交错。 */
  async refreshTokens(input: { refreshToken: string }): Promise<DeviceTokenRefreshHttpResponse> {
    return this.#writeCoordinator.enqueue(async () => {
      const now = this.#now();
      const record = await loadDeviceRefreshCredentialRow(
        this.#database, hashOpaqueCredential(input.refreshToken),
      );
      if (record === null) {
        throw this.#credentialInvalid();
      }
      const device = await loadPairedDeviceAuthRecord(this.#database, record.deviceId);
      if (device === null) {
        throw this.#credentialInvalid();
      }
      if (record.status !== "active") {
        await this.#revokeCredentialFamily(record);
        throw new ApplicationError({
          code: "DEVICE_CREDENTIAL_REUSED", message: "设备刷新凭据已使用或撤销", statusCode: 401, retryable: false,
        });
      }
      let rotation;
      try {
        rotation = rotateDeviceRefreshCredential(
          device, record, input.refreshToken, now, REFRESH_TOKEN_TTL_MILLISECONDS,
        );
      } catch (error: unknown) {
        throw this.#deviceError(error);
      }
      const access = issueDeviceAccessToken(device, now, ACCESS_TOKEN_TTL_MILLISECONDS);
      await rotateDeviceRefreshCredentialRows(this.#database, rotation, access.record);
      return {
        accessToken: access.token,
        accessTokenExpiresAt: access.record.expiresAt,
        refreshToken: rotation.token,
        refreshTokenExpiresAt: rotation.next.expiresAt,
      };
    });
  }

  /** 当前安装退出登录，撤销设备与每个 access/refresh；重新登录可复用设备 ID。 */
  async revokeDevice(deviceId: string): Promise<void> {
    await this.#writeCoordinator.enqueue(async () => {
      const connection = this.#database.connection;
      const now = this.#now();
      await connection.run("BEGIN TRANSACTION");
      try {
        await revokeDeviceCredentials(this.#database, deviceId, now);
        await connection.run(`
          UPDATE paired_devices SET status = 'revoked',
            revoked_at = coalesce(revoked_at, CAST($now AS TIMESTAMPTZ)),
            updated_at = greatest(updated_at, CAST($now AS TIMESTAMPTZ))
          WHERE id = CAST($id AS UUID)
        `, { id: deviceId, now: now.toISOString() });
        await connection.run("COMMIT");
      } catch (error: unknown) {
        await rollback(connection, error);
        throw error;
      }
    });
  }

  async #revokeCredentialFamily(record: DeviceRefreshCredentialRecord): Promise<void> {
    const family = await loadDeviceRefreshCredentialFamily(this.#database, record.deviceId, record.familyId);
    const plan = planRefreshCredentialReuseRevocation(record, family, this.#now());
    await applyRefreshCredentialFamilyRevocation(this.#database, plan);
  }

  #deviceError(error: unknown): ApplicationError {
    if (error instanceof ApplicationError) {
      return error;
    }
    if (error instanceof DeviceAuthError) {
      return new ApplicationError({
        code: error.code, message: error.message, statusCode: 401, retryable: false,
      });
    }
    return this.#credentialInvalid();
  }

  #credentialInvalid(): ApplicationError {
    return new ApplicationError({
      code: "DEVICE_CREDENTIAL_INVALID", message: "设备凭据无效", statusCode: 401, retryable: false,
    });
  }
}

async function rollback(connection: LyraDatabase["connection"], error: unknown): Promise<void> {
  try {
    await connection.run("ROLLBACK");
  } catch (rollbackError: unknown) {
    throw new AggregateError([error, rollbackError], "设备凭据事务失败且回滚失败");
  }
}
