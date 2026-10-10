import { ApplicationError, type ApiErrorCode, type ApiErrorDetails } from "../application/index.js";
import {
  AgentSessionStateError,
  AgentSwitchError,
  AgentToolPolicyError,
} from "../domain/agent/index.js";
import { FifoError } from "../domain/fifo/index.js";
import { GmailSyncError } from "../domain/gmail/index.js";
import { IngestError } from "../domain/ledger/index.js";
import { GmailOAuthError } from "../infrastructure/gmail/index.js";
import {
  DeviceAuthError,
  type DeviceAuthErrorCode,
} from "../infrastructure/security/index.js";

/** Android App 可稳定解析的错误响应正文。 */
export interface ApiErrorBody {
  request_id: string;
  code: ApiErrorCode;
  message: string;
  details: ApiErrorDetails | null;
  retryable: boolean;
}

/** HTTP 适配层写回响应所需的信息。 */
export interface SerializedApiError {
  statusCode: number;
  body: ApiErrorBody;
}

/** 设备配对错误与 HTTP 语义的显式映射。 */
const DEVICE_STATUS_CODES: Readonly<Record<DeviceAuthErrorCode, number>> = {
  PAIRING_CODE_INVALID: 400,
  PAIRING_CODE_EXPIRED: 410,
  PAIRING_CODE_USED: 409,
  DEVICE_PUBLIC_KEY_INVALID: 400,
  DEVICE_CREDENTIAL_INVALID: 401,
  DEVICE_CREDENTIAL_EXPIRED: 401,
  DEVICE_CREDENTIAL_REUSED: 401,
  DEVICE_REVOKED: 403,
};

/** Gmail 状态错误与 HTTP 语义的显式映射。 */
const GMAIL_STATUS_CODES: Readonly<Record<GmailSyncError["code"], number>> = {
  GMAIL_HISTORY_EXPIRED: 409,
  GMAIL_INCREMENTAL_LIMIT_EXCEEDED: 409,
  GMAIL_RESYNC_CONFIRMATION_REQUIRED: 409,
  GMAIL_AUTH_REVOKED: 401,
  GMAIL_CONNECTION_ERROR: 409,
};

/** Google OAuth 事务错误与 HTTP 语义的显式映射。 */
const GOOGLE_AUTH_STATUS_CODES: Readonly<Record<GmailOAuthError["code"], number>> = {
  GOOGLE_AUTH_TRANSACTION_INVALID: 400,
  GOOGLE_AUTH_TRANSACTION_EXPIRED: 410,
  GOOGLE_AUTH_TRANSACTION_USED: 409,
  GOOGLE_TOKEN_EXCHANGE_FAILED: 502,
  GOOGLE_IDENTITY_UNVERIFIED: 403,
  GMAIL_SCOPE_MISSING: 403,
};

/**
 * 把应用和领域错误转换成稳定、安全的 API 响应。
 *
 * 调用方仍须使用原始 error 写服务端结构化日志；本函数只负责客户端响应，
 * 未识别错误统一脱敏，绝不返回堆栈、文件路径、令牌或底层异常文本。
 *
 * @param error - 路由执行期间捕获的原始异常。
 * @param requestId - 由请求入口生成并写入日志的关联标识。
 */
export function serializeApiError(error: unknown, requestId: string): SerializedApiError {
  if (error instanceof ApplicationError) {
    return createResponse(
      requestId,
      error.statusCode,
      error.code,
      error.message,
      error.details,
      error.retryable,
    );
  }
  if (error instanceof IngestError) {
    return createResponse(
      requestId,
      422,
      "IMPORT_VALIDATION_FAILED",
      error.message,
      null,
      false,
    );
  }
  if (error instanceof FifoError) {
    return createResponse(
      requestId,
      409,
      "LEDGER_REBUILD_FAILED",
      error.message,
      null,
      false,
    );
  }
  if (error instanceof DeviceAuthError) {
    return createResponse(
      requestId,
      DEVICE_STATUS_CODES[error.code],
      error.code,
      error.message,
      null,
      false,
    );
  }
  if (error instanceof AgentSwitchError) {
    return createResponse(
      requestId,
      error.code === "AGENT_UNAVAILABLE" ? 503 : 409,
      error.code,
      error.message,
      null,
      false,
    );
  }
  if (error instanceof AgentToolPolicyError) {
    return createResponse(
      requestId,
      403,
      "AGENT_TOOL_FORBIDDEN",
      "Agent 请求的工具不允许执行",
      null,
      false,
    );
  }
  if (error instanceof AgentSessionStateError) {
    return createResponse(
      requestId,
      409,
      error.code,
      error.message,
      null,
      false,
    );
  }
  if (error instanceof GmailSyncError) {
    return createResponse(
      requestId,
      error.code === "GMAIL_CONNECTION_ERROR" && error.retryable
        ? 503
        : GMAIL_STATUS_CODES[error.code],
      error.code,
      error.message,
      null,
      error.retryable,
    );
  }
  if (error instanceof GmailOAuthError) {
    return createResponse(
      requestId,
      GOOGLE_AUTH_STATUS_CODES[error.code],
      error.code,
      error.message,
      null,
      error.retryable,
    );
  }
  if (error instanceof RangeError) {
    return createResponse(
      requestId,
      400,
      "INVALID_ARGUMENT",
      "请求参数无效",
      null,
      false,
    );
  }
  return createResponse(
    requestId,
    500,
    "INTERNAL_ERROR",
    "服务端发生内部错误",
    null,
    false,
  );
}

/** 统一构造响应，避免各错误分支产生字段差异。 */
function createResponse(
  requestId: string,
  statusCode: number,
  code: ApiErrorCode,
  message: string,
  details: ApiErrorDetails | null,
  retryable: boolean,
): SerializedApiError {
  return {
    statusCode,
    body: {
      request_id: requestId,
      code,
      message,
      details,
      retryable,
    },
  };
}
