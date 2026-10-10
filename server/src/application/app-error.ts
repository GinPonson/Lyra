/** 服务端与 Android App 共享语义的稳定错误码。 */
export const API_ERROR_CODES = [
  "INVALID_ARGUMENT",
  "AUTH_NOT_CONFIGURED",
  "LOGIN_STATE_INVALID",
  "LOGIN_EXPIRED",
  "LOGIN_ALREADY_REDEEMED",
  "LOGIN_FAILED",
  "LOGIN_DENIED",
  "LOGIN_RATE_LIMITED",
  "IMPORT_UNSUPPORTED_FORMAT",
  "IMPORT_UNKNOWN_HEADERS",
  "IMPORT_AMBIGUOUS_SHEET",
  "IMPORT_VALIDATION_FAILED",
  "IMPORT_PREVIEW_STALE",
  "IMPORT_STATE_CONFLICT",
  "ACCOUNT_MAPPING_REQUIRED",
  "ACCOUNT_ALREADY_EXISTS",
  "ACCOUNT_SCOPE_REQUIRED",
  "GMAIL_SCOPE_MISSING",
  "GMAIL_AUTH_REVOKED",
  "GMAIL_HISTORY_EXPIRED",
  "GMAIL_INCREMENTAL_LIMIT_EXCEEDED",
  "GMAIL_RESYNC_CONFIRMATION_REQUIRED",
  "GMAIL_CONNECTION_ERROR",
  "GMAIL_MESSAGE_CONFLICT",
  "GMAIL_MESSAGE_REJECTED",
  "GOOGLE_AUTH_TRANSACTION_INVALID",
  "GOOGLE_AUTH_TRANSACTION_EXPIRED",
  "GOOGLE_AUTH_TRANSACTION_USED",
  "GOOGLE_TOKEN_EXCHANGE_FAILED",
  "GOOGLE_IDENTITY_UNVERIFIED",
  "LEDGER_VERSION_CONFLICT",
  "LEDGER_REBUILD_FAILED",
  "AGENT_UNAVAILABLE",
  "AGENT_SWITCH_REQUIRES_TURN_BOUNDARY",
  "AGENT_SESSION_STATE_CONFLICT",
  "AGENT_TURN_FAILED",
  "AGENT_TURN_CANCELLED",
  "AGENT_TOOL_FORBIDDEN",
  "AGENT_TOOL_EXECUTION_FAILED",
  "AGENT_TOOL_CANCELLED",
  "CLIENT_VERSION_INCOMPATIBLE",
  "PAIRING_CODE_INVALID",
  "PAIRING_CODE_EXPIRED",
  "PAIRING_CODE_USED",
  "DEVICE_PUBLIC_KEY_INVALID",
  "DEVICE_CREDENTIAL_INVALID",
  "DEVICE_CREDENTIAL_EXPIRED",
  "DEVICE_CREDENTIAL_REUSED",
  "DEVICE_REVOKED",
  "IDEMPOTENCY_KEY_REUSED",
  "IDEMPOTENCY_REQUEST_IN_PROGRESS",
  "INTERNAL_ERROR",
] as const;

/** 编译期与运行时共享的 API 错误码类型。 */
export type ApiErrorCode = typeof API_ERROR_CODES[number];

/** 可安全序列化给客户端的结构化错误上下文。 */
export type ApiErrorDetails = Readonly<Record<string, unknown>>;

/** 构造一个预期应用错误所需的完整信息。 */
export interface ApplicationErrorInput {
  code: ApiErrorCode;
  message: string;
  statusCode: number;
  retryable: boolean;
  details?: ApiErrorDetails | null;
  cause?: unknown;
}

/**
 * 路由和应用用例可显式抛出的预期错误。
 *
 * 只有不含密钥、路径、堆栈或底层命令的信息才能放入 message 与 details；
 * cause 仅供服务端日志使用，错误序列化器不会把它返回给客户端。
 */
export class ApplicationError extends Error {
  readonly code: ApiErrorCode;
  readonly statusCode: number;
  readonly retryable: boolean;
  readonly details: ApiErrorDetails | null;

  /** @param input - 稳定错误码、HTTP 状态和安全客户端信息。 */
  constructor(input: ApplicationErrorInput) {
    super(input.message, input.cause === undefined ? undefined : { cause: input.cause });
    this.name = "ApplicationError";
    this.code = input.code;
    this.statusCode = input.statusCode;
    this.retryable = input.retryable;
    this.details = input.details ?? null;
  }
}
