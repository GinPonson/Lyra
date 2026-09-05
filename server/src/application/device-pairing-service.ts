import { randomUUID } from "node:crypto";

import type {
  DeviceRefreshCredentialRecord,
  PairedDeviceAuthRecord,
} from "../infrastructure/security/device-auth.js";
import {
  consumePairingChallenge,
  createPairedDeviceRecord,
  createPairingChallenge,
  issueDeviceAccessToken,
  issueDeviceRefreshCredential,
  normalizeDevicePublicKey,
  planRefreshCredentialReuseRevocation,
  rotateDeviceRefreshCredential,
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
import { hashOpaqueCredential } from "../infrastructure/security/index.js";
import { loadPairedDeviceAuthRecord } from "../infrastructure/db/device-access-token-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { ApplicationError } from "./app-error.js";
import type { WriteCoordinator } from "./write-coordinator.js";

/** 配对码有效期；一次性且短时，不写入任何静态配置。 */
const PAIRING_CODE_TTL_MILLISECONDS = 10 * 60 * 1000;

/** 访问令牌与刷新凭据的有效期。 */
const ACCESS_TOKEN_TTL_MILLISECONDS = 15 * 60 * 1000;
const REFRESH_TOKEN_TTL_MILLISECONDS = 30 * 24 * 60 * 60 * 1000;

/** 一次配对完成后 App 获得的凭据集。 */
export interface DevicePairingHttpResponse {
  deviceId: string;
  displayName: string;
  accessToken: string;
  accessTokenExpiresAt: string;
  refreshToken: string;
  refreshTokenExpiresAt: string;
}

/** 一次刷新轮换后 App 获得的新凭据集。 */
export interface DeviceTokenRefreshHttpResponse {
  accessToken: string;
  accessTokenExpiresAt: string;
  refreshToken: string;
  refreshTokenExpiresAt: string;
}

/**
 * 设备配对、访问令牌签发与刷新凭据轮换的应用层编排。
 *
 * 配对码记录只保存在进程内内存（短时、一次性；重启即失效，需要时重新生
 * 成）。刷新凭据与访问令牌只持久化 SHA-256 哈希；明文仅在签发响应中出现
 * 一次。旧刷新凭据重放会触发同族全代次撤销。
 */
export class DevicePairingService {
  readonly #database: LyraDatabase;
  readonly #writeCoordinator: WriteCoordinator;
  readonly #now: () => Date;
  readonly #challenges = new Map<string, PairingChallengeRecordLike>();

  constructor(input: {
    database: LyraDatabase;
    writeCoordinator: WriteCoordinator;
    now?: () => Date;
  }) {
    this.#database = input.database;
    this.#writeCoordinator = input.writeCoordinator;
    this.#now = input.now ?? (() => new Date());
  }

  /** 生成一次性配对码；仅返回明文展示码与过期时间。 */
  createPairingCode(): { code: string; expiresAt: string } {
    const challenge = createPairingChallenge(this.#now(), PAIRING_CODE_TTL_MILLISECONDS);
    this.#challenges.set(challenge.record.codeHash, challenge.record);
    return { code: challenge.code, expiresAt: challenge.record.expiresAt };
  }

  /**
   * 用配对码兑换设备凭据：消费一次性挑战、登记设备并签发首代刷新凭据与
   * 短期访问令牌。
   */
  async pairDevice(input: {
    code: string;
    displayName: string;
    publicKeyPem: string;
  }): Promise<DevicePairingHttpResponse> {
    const now = this.#now();
    const consumed = this.#consumeChallenge(input.code);
    const publicKey = normalizeDevicePublicKey(input.publicKeyPem);
    const record = createPairedDeviceRecord(
      randomUUID(),
      input.displayName,
      publicKey,
      now,
    );
    const authRecord: PairedDeviceAuthRecord = {
      id: record.id,
      status: record.status,
      revokedAt: record.revokedAt,
    };
    const refresh = issueDeviceRefreshCredential(
      record.id,
      now,
      REFRESH_TOKEN_TTL_MILLISECONDS,
    );
    const access = issueDeviceAccessToken(authRecord, now, ACCESS_TOKEN_TTL_MILLISECONDS);
    await this.#writeCoordinator.enqueue(async () => {
      await createPairedDeviceRow(this.#database, record);
      await createDeviceRefreshCredentialRow(this.#database, refresh.record);
      await createDeviceAccessTokenRow(this.#database, {
        id: access.record.id,
        deviceId: record.id,
        tokenHash: access.record.tokenHash,
        expiresAt: access.record.expiresAt,
        issuedAt: access.record.issuedAt,
      });
    });
    this.#challenges.delete(consumed.codeHash);
    return {
      deviceId: record.id,
      displayName: record.displayName,
      accessToken: access.token,
      accessTokenExpiresAt: access.record.expiresAt,
      refreshToken: refresh.token,
      refreshTokenExpiresAt: refresh.record.expiresAt,
    };
  }

  /**
   * 校验刷新凭据并轮换：旧代标记 rotated、新代入库，同时签发新访问令牌。
   *
   * 已轮换/已撤销凭据再次出现会撤销整个凭据族并返回稳定重用错误。
   */
  async refreshTokens(input: { refreshToken: string }): Promise<DeviceTokenRefreshHttpResponse> {
    const now = this.#now();
    const record = await loadDeviceRefreshCredentialRow(
      this.#database,
      hashOpaqueCredential(input.refreshToken),
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
        code: "DEVICE_CREDENTIAL_REUSED",
        message: "设备刷新凭据已使用或撤销",
        statusCode: 401,
        retryable: false,
      });
    }
    let rotation;
    try {
      rotation = rotateDeviceRefreshCredential(
        device,
        record,
        input.refreshToken,
        now,
        REFRESH_TOKEN_TTL_MILLISECONDS,
      );
    } catch (error: unknown) {
      if (error instanceof DeviceAuthError
        && error.code === "DEVICE_CREDENTIAL_REUSED") {
        await this.#revokeCredentialFamily(record);
      }
      throw this.#deviceError(error);
    }
    await this.#writeCoordinator.enqueue(() => rotateDeviceRefreshCredentialRows(
      this.#database,
      rotation,
    ));
    const access = issueDeviceAccessToken(device, now, ACCESS_TOKEN_TTL_MILLISECONDS);
    await this.#writeCoordinator.enqueue(() => createDeviceAccessTokenRow(this.#database, {
      id: access.record.id,
      deviceId: device.id,
      tokenHash: access.record.tokenHash,
      expiresAt: access.record.expiresAt,
      issuedAt: access.record.issuedAt,
    }));
    return {
      accessToken: access.token,
      accessTokenExpiresAt: access.record.expiresAt,
      refreshToken: rotation.token,
      refreshTokenExpiresAt: rotation.next.expiresAt,
    };
  }

  /** 逐条尝试消费内存中的挑战记录；USED/EXPIRED 直接上报，INVALID 尝试下一条。 */
  #consumeChallenge(candidate: string) {
    let invalidSeen = false;
    for (const record of this.#challenges.values()) {
      try {
        return consumePairingChallenge(record, candidate, this.#now());
      } catch (error: unknown) {
        if (error instanceof DeviceAuthError
          && (error.code === "PAIRING_CODE_USED" || error.code === "PAIRING_CODE_EXPIRED")) {
          throw this.#deviceError(error);
        }
        invalidSeen = true;
      }
    }
    void invalidSeen;
    throw this.#deviceError(new DeviceAuthError("PAIRING_CODE_INVALID", "配对码无效"));
  }

  async #revokeCredentialFamily(record: DeviceRefreshCredentialRecord): Promise<void> {
    const family = await loadDeviceRefreshCredentialFamily(
      this.#database,
      record.deviceId,
      record.familyId,
    );
    const plan = planRefreshCredentialReuseRevocation(record, family, this.#now());
    await this.#writeCoordinator.enqueue(() => applyRefreshCredentialFamilyRevocation(
      this.#database,
      plan,
    ));
  }

  #deviceError(error: unknown): ApplicationError {
    if (error instanceof ApplicationError) {
      return error;
    }
    if (error instanceof DeviceAuthError) {
      const statusCode = error.code === "PAIRING_CODE_EXPIRED" ? 410
        : error.code === "PAIRING_CODE_USED" ? 409
          : error.code === "PAIRING_CODE_INVALID" ? 400
            : 401;
      return new ApplicationError({
        code: error.code,
        message: error.message,
        statusCode,
        retryable: false,
      });
    }
    return this.#credentialInvalid();
  }

  #credentialInvalid(): ApplicationError {
    return new ApplicationError({
      code: "DEVICE_CREDENTIAL_INVALID",
      message: "设备凭据无效",
      statusCode: 401,
      retryable: false,
    });
  }
}

interface PairingChallengeRecordLike {
  codeHash: string;
  expiresAt: string;
  consumedAt: string | null;
}
