import type { PairedDeviceAuthRecord } from "../infrastructure/security/device-auth.js";
import {
  DeviceAuthError,
  authenticateDeviceAccessToken,
} from "../infrastructure/security/device-auth.js";
import { hashOpaqueCredential } from "../infrastructure/security/index.js";
import {
  loadDeviceAccessTokenRecord,
  loadPairedDeviceAuthRecord,
} from "../infrastructure/db/device-access-token-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { ApplicationError } from "./app-error.js";

/**
 * 设备访问令牌认证网关。
 *
 * Bearer 明文只在请求内存中散列；数据库只保存哈希。认证失败统一转换为
 * 401 的稳定设备错误码，底层原因不进入客户端响应。
 */
export function createDeviceAuthGate(
  database: LyraDatabase,
  now: () => Date = () => new Date(),
): (token: string) => Promise<PairedDeviceAuthRecord> {
  return async (token) => {
    try {
      const normalizedToken = token.trim();
      if (normalizedToken.length === 0) {
        throw new DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效");
      }
      const record = await loadDeviceAccessTokenRecord(
        database,
        hashOpaqueCredential(normalizedToken),
      );
      if (record === null) {
        throw new DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效");
      }
      const device = await loadPairedDeviceAuthRecord(database, record.deviceId);
      if (device === null) {
        throw new DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效");
      }
      authenticateDeviceAccessToken(device, record, normalizedToken, now());
      return device;
    } catch (error: unknown) {
      if (error instanceof DeviceAuthError) {
        throw new ApplicationError({
          code: error.code,
          message: error.message,
          statusCode: 401,
          retryable: false,
        });
      }
      throw error;
    }
  };
}
