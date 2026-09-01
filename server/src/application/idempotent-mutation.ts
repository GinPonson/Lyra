import { createHash } from "node:crypto";

import { ApplicationError } from "./app-error.js";

/** 修改请求参与指纹计算的规范 JSON 值。 */
export type IdempotencyJson =
  | null
  | boolean
  | number
  | string
  | readonly IdempotencyJson[]
  | { readonly [key: string]: IdempotencyJson };

/** 默认保留完成记录 24 小时，覆盖移动网络重试和 App 重启。 */
export const DEFAULT_IDEMPOTENCY_TTL_HOURS = 24;

/** 与 mutation_idempotency_records 表对齐，不包含明文幂等键或响应正文。 */
export interface MutationIdempotencyRecord {
  id: string;
  deviceId: string;
  operation: string;
  keyHash: string;
  requestHash: string;
  status: "in_progress" | "completed";
  resultResourceId: string | null;
  responseStatusCode: number | null;
  createdAt: string;
  expiresAt: string;
  completedAt: string | null;
}

/** 敏感修改接口在执行业务逻辑前取得的幂等决策。 */
export type IdempotentMutationPlan =
  | {
      kind: "start";
      record: MutationIdempotencyRecord;
      replacesExpiredRecordId: string | null;
    }
  | {
      kind: "replay";
      recordId: string;
      resultResourceId: string;
      responseStatusCode: number;
      completedAt: string;
    };

/**
 * 使用操作名和规范 JSON 载荷生成稳定请求指纹。
 *
 * 对象键排序后再散列，因此 JSON 字段顺序不会制造不同请求；函数只返回
 * SHA-256，不返回或保存可能包含账户名称等业务字段的规范化正文。
 */
export function createIdempotentRequestHash(
  operation: string,
  payload: IdempotencyJson,
): string {
  const normalizedOperation = requireOperation(operation);
  return sha256(`${normalizedOperation}\n${serializeCanonicalJson(payload)}`);
}

/**
 * 根据数据库中同设备、同操作、同 key_hash 的记录决定开始、重放或拒绝。
 *
 * @param input.key - App 每次逻辑修改生成的明文幂等键；只在当前请求内存中存在。
 * @param input.requestHash - 由 `createIdempotentRequestHash` 生成的请求指纹。
 * @param input.existingRecord - 仓储按设备、操作和 key_hash 查询到的唯一记录。
 */
export function planIdempotentMutation(input: {
  recordId: string;
  deviceId: string;
  operation: string;
  key: string;
  requestHash: string;
  existingRecord: MutationIdempotencyRecord | null;
  now: Date;
  ttlHours?: number;
}): IdempotentMutationPlan {
  const recordId = requireText(input.recordId, "幂等记录 ID");
  const deviceId = requireText(input.deviceId, "幂等设备 ID");
  const operation = requireOperation(input.operation);
  const keyHash = hashIdempotencyKey(input.key);
  const requestHash = requireSha256(input.requestHash, "幂等请求指纹");
  const now = requireDate(input.now, "幂等请求时间");
  const ttlHours = input.ttlHours ?? DEFAULT_IDEMPOTENCY_TTL_HOURS;
  if (!Number.isSafeInteger(ttlHours) || ttlHours <= 0) {
    throw new RangeError("幂等记录保留小时数必须是正安全整数");
  }

  const existing = input.existingRecord;
  if (existing !== null) {
    assertMatchingRecord(existing, deviceId, operation, keyHash);
    const expiresAt = requireTimestamp(existing.expiresAt, "幂等记录过期时间");
    if (expiresAt > now.getTime()) {
      if (existing.requestHash !== requestHash) {
        throw new ApplicationError({
          code: "IDEMPOTENCY_KEY_REUSED",
          message: "幂等键已用于其他请求，请生成新的幂等键",
          statusCode: 409,
          retryable: false,
        });
      }
      if (existing.status === "in_progress") {
        throw new ApplicationError({
          code: "IDEMPOTENCY_REQUEST_IN_PROGRESS",
          message: "相同修改请求仍在处理中，请稍后重试",
          statusCode: 409,
          retryable: true,
        });
      }
      if (
        existing.resultResourceId === null
        || existing.responseStatusCode === null
        || existing.completedAt === null
      ) {
        throw new Error("已完成幂等记录缺少结果引用");
      }
      return {
        kind: "replay",
        recordId: existing.id,
        resultResourceId: existing.resultResourceId,
        responseStatusCode: existing.responseStatusCode,
        completedAt: existing.completedAt,
      };
    }
  }

  const createdAt = now.toISOString();
  return {
    kind: "start",
    replacesExpiredRecordId: existing?.id ?? null,
    record: {
      id: recordId,
      deviceId,
      operation,
      keyHash,
      requestHash,
      status: "in_progress",
      resultResourceId: null,
      responseStatusCode: null,
      createdAt,
      expiresAt: new Date(
        now.getTime() + ttlHours * 60 * 60 * 1000,
      ).toISOString(),
      completedAt: null,
    },
  };
}

/**
 * 在业务写入成功的同一事务内完成幂等记录。
 *
 * 只保存可重新读取安全响应所需的资源 ID 与 HTTP 状态，不保存响应正文、
 * access token、refresh token 或 OAuth code。
 */
export function completeIdempotentMutation(
  record: MutationIdempotencyRecord,
  input: { resultResourceId: string; responseStatusCode: number; now: Date },
): MutationIdempotencyRecord {
  if (record.status !== "in_progress") {
    throw new ApplicationError({
      code: "IDEMPOTENCY_KEY_REUSED",
      message: "幂等修改已经完成",
      statusCode: 409,
      retryable: false,
    });
  }
  const resultResourceId = requireText(input.resultResourceId, "幂等结果资源 ID");
  if (
    !Number.isSafeInteger(input.responseStatusCode)
    || input.responseStatusCode < 200
    || input.responseStatusCode > 299
  ) {
    throw new RangeError("幂等结果 HTTP 状态必须是 2xx 整数");
  }
  return {
    ...record,
    status: "completed",
    resultResourceId,
    responseStatusCode: input.responseStatusCode,
    completedAt: requireDate(input.now, "幂等完成时间").toISOString(),
  };
}

/** 明文幂等键只在当前请求内散列，禁止进入持久化记录。 */
function hashIdempotencyKey(key: string): string {
  if (
    key !== key.trim()
    || !/^[A-Za-z0-9._:-]{16,200}$/.test(key)
  ) {
    throw new RangeError("幂等键必须是 16 至 200 位安全字符");
  }
  return sha256(key);
}

/** 确保仓储没有把其他设备或操作的记录用于本次重放判断。 */
function assertMatchingRecord(
  record: MutationIdempotencyRecord,
  deviceId: string,
  operation: string,
  keyHash: string,
): void {
  if (
    record.deviceId !== deviceId
    || record.operation !== operation
    || record.keyHash !== keyHash
  ) {
    throw new Error("幂等记录与请求查询范围不匹配");
  }
}

/** 递归生成无空白且对象键稳定排序的 JSON 文本。 */
function serializeCanonicalJson(value: IdempotencyJson): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new RangeError("幂等请求 JSON 数字必须有限");
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(serializeCanonicalJson).join(",")}]`;
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new RangeError("幂等请求载荷必须是普通 JSON 对象");
  }
  return `{${Object.entries(value)
    .toSorted(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${JSON.stringify(key)}:${serializeCanonicalJson(item)}`)
    .join(",")}}`;
}

/** 验证路由定义使用的稳定操作名。 */
function requireOperation(value: string): string {
  if (value !== value.trim() || !/^[a-z][a-z0-9_.:-]{1,99}$/.test(value)) {
    throw new RangeError("幂等操作名无效");
  }
  return value;
}

/** 验证必填文本但不改变其持久化语义。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 验证调用方提供的 SHA-256 十六进制摘要。 */
function requireSha256(value: string, label: string): string {
  const normalized = value.toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(normalized)) {
    throw new RangeError(`${label}必须是 SHA-256 十六进制摘要`);
  }
  return normalized;
}

/** 验证服务端可信时钟。 */
function requireDate(value: Date, label: string): Date {
  if (Number.isNaN(value.getTime())) {
    throw new RangeError(`${label}无效`);
  }
  return value;
}

/** 验证数据库 UTC 时间戳并返回 epoch 毫秒。 */
function requireTimestamp(value: string, label: string): number {
  if (!value.endsWith("Z")) {
    throw new RangeError(`${label}必须是 UTC ISO 时间戳`);
  }
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    throw new RangeError(`${label}无效`);
  }
  return timestamp;
}

/** 统一生成小写 SHA-256 十六进制摘要。 */
function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}
