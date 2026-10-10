import { ApplicationError } from "../application/index.js";

/** 当前移动端与服务端共享的协议版本。 */
export const LYRA_PROTOCOL_VERSION = 2;

/** 当前服务端接受的最旧协议版本。 */
export const MIN_SUPPORTED_PROTOCOL_VERSION = 2;

/** 当前服务端接受的最新协议版本。 */
export const MAX_SUPPORTED_PROTOCOL_VERSION = LYRA_PROTOCOL_VERSION;

/** `/v1/system/info` 可直接返回的无敏感协议元数据。 */
export interface ServerProtocolMetadata {
  server_version: string;
  protocol_version: number;
  min_supported_protocol_version: number;
  max_supported_protocol_version: number;
  schema_version: number;
}

/**
 * 校验 App 通过请求头提交的协议版本。
 *
 * @param rawVersion - `X-Lyra-Protocol-Version` 原始值。
 * @returns 已验证的整数协议版本。
 * @throws ApplicationError 当版本缺失、非法、过旧或过新时抛出 426 错误。
 */
export function assertClientProtocolVersion(rawVersion: string | undefined): number {
  const version = rawVersion === undefined ? Number.NaN : Number(rawVersion);
  if (
    !Number.isSafeInteger(version)
    || version < MIN_SUPPORTED_PROTOCOL_VERSION
    || version > MAX_SUPPORTED_PROTOCOL_VERSION
  ) {
    throw new ApplicationError({
      code: "CLIENT_VERSION_INCOMPATIBLE",
      message: "App 与服务端协议版本不兼容，请升级后重试",
      statusCode: 426,
      retryable: false,
      details: {
        min_supported_protocol_version: MIN_SUPPORTED_PROTOCOL_VERSION,
        max_supported_protocol_version: MAX_SUPPORTED_PROTOCOL_VERSION,
      },
    });
  }
  return version;
}

/**
 * 建立公开的服务端协议元数据。
 *
 * @param serverVersion - 服务端发布版本，不含构建路径或提交环境信息。
 * @param schemaVersion - 当前已经完成的 DuckDB migration 版本。
 */
export function createServerProtocolMetadata(
  serverVersion: string,
  schemaVersion: number,
): ServerProtocolMetadata {
  if (serverVersion.trim().length === 0) {
    throw new RangeError("serverVersion 不能为空");
  }
  if (!Number.isSafeInteger(schemaVersion) || schemaVersion < 0) {
    throw new RangeError("schemaVersion 必须是非负安全整数");
  }
  return {
    server_version: serverVersion,
    protocol_version: LYRA_PROTOCOL_VERSION,
    min_supported_protocol_version: MIN_SUPPORTED_PROTOCOL_VERSION,
    max_supported_protocol_version: MAX_SUPPORTED_PROTOCOL_VERSION,
    schema_version: schemaVersion,
  };
}
