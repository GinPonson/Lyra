import { randomUUID } from "node:crypto";
import { z } from "zod";

import { createBrokerageAccount } from "../domain/accounts/index.js";
import { createAuditEvent } from "../domain/audit/index.js";
import {
  loadBrokerageAccount,
  persistCreatedBrokerageAccount,
} from "../infrastructure/db/account-repository.js";
import type { LyraDatabase } from "../infrastructure/db/database.js";
import { loadMutationIdempotencyRecord } from "../infrastructure/db/mutation-idempotency-repository.js";
import { createPublicAccountList, type PublicBrokerageAccount } from "./account-view.js";
import { ApplicationError } from "./app-error.js";
import {
  createIdempotentRequestHash,
  hashIdempotencyKey,
  planIdempotentMutation,
} from "./idempotent-mutation.js";
import type { WriteCoordinator } from "./write-coordinator.js";

/** 用户只提供导出文件中的账户标识，不需要完整券商账号或客户端派生后缀。 */
const createAccountSchema = z.object({
  displayName: z.string().trim().min(1).max(64).regex(/^[^\u0000-\u001f\u007f-\u009f]+$/u),
  accountIdentifier: z.string().trim().toUpperCase().min(4).max(32)
    .regex(/^[A-Z0-9-]+[0-9]{3}$/u),
  baseCurrency: z.string().trim().toUpperCase().regex(/^[A-Z]{3}$/u).default("USD"),
}).strict();

export type CreateAccountRequest = z.input<typeof createAccountSchema>;
export interface CreateAccountHttpResponse {
  account: PublicBrokerageAccount;
}

/** 证券账户创建仅供已登录 App 使用，始终与只读金融 Tool Registry 分离。 */
export class AccountService {
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

  /**
   * 写队列内重新校验原始访问凭证；退出登录先落库时，等待中的创建或重放均拒绝。
   * 同设备、同规范载荷、同幂等键重放相同账户，不重复写入账户或审计事件。
   */
  async createAccount(input: {
    request: unknown;
    idempotencyKey: string | null;
    requestId: string;
    authorize: () => Promise<{ id: string }>;
  }): Promise<CreateAccountHttpResponse> {
    const parsed = createAccountSchema.safeParse(input.request);
    if (!parsed.success) {
      throw new ApplicationError({
        code: "INVALID_ARGUMENT",
        message: "账户名称须为 1 至 64 个字符；文件账户标识须为 4 至 32 位字母、数字或连字符并以至少 3 位数字结尾；币种须为 3 个字母",
        statusCode: 400,
        retryable: false,
      });
    }
    const request = parsed.data;
    const operation = "account.create";
    const keyHash = input.idempotencyKey === null ? null : hashIdempotencyKey(input.idempotencyKey);
    const requestHash = createIdempotentRequestHash(operation, request);
    return this.#writeCoordinator.enqueue(async () => {
      const device = await input.authorize();
      const now = this.#now();
      const idempotency = input.idempotencyKey === null || keyHash === null
        ? null
        : planIdempotentMutation({
          recordId: randomUUID(),
          deviceId: device.id,
          operation,
          key: input.idempotencyKey,
          requestHash,
          existingRecord: await loadMutationIdempotencyRecord(this.#database, {
            deviceId: device.id,
            operation,
            keyHash,
          }),
          now,
        });
      if (idempotency?.kind === "replay") {
        const account = await loadBrokerageAccount(this.#database, idempotency.resultResourceId);
        if (account === null) {
          throw new Error("已完成账户创建的结果不存在");
        }
        return { account: createPublicAccountList([account])[0]! };
      }
      // Identifier validation guarantees at least three trailing digits. Expose
      // at most the last four digits, never the import matching key itself.
      const maskedSuffix = request.accountIdentifier.match(/[0-9]{3,4}$/u)![0];
      const account = createBrokerageAccount({
        displayName: request.displayName,
        legacyAccountKey: request.accountIdentifier,
        maskedSuffix,
        baseCurrency: request.baseCurrency,
      }, randomUUID(), now);
      const auditEvent = createAuditEvent({
        eventType: "account.created",
        outcome: "success",
        actorType: "device",
        actorId: device.id,
        requestId: input.requestId,
        targetType: "account",
        targetId: account.id,
        accountId: account.id,
        metadata: { broker: account.broker, base_currency: account.baseCurrency },
        now,
      });
      await persistCreatedBrokerageAccount(this.#database, { account, auditEvent, idempotency });
      return { account: createPublicAccountList([account])[0]! };
    });
  }
}
