/** 第一阶段唯一支持的券商标识。 */
export type AccountBroker = "schwab";

/** 账户保留历史数据时使用的生命周期状态。 */
export type AccountStatus = "active" | "archived";

/** 服务端账户领域记录，与 DuckDB accounts 表字段一一对应。 */
export interface BrokerageAccount {
  id: string;
  displayName: string;
  legacyAccountKey: string;
  broker: AccountBroker;
  maskedSuffix: string;
  baseCurrency: string;
  status: AccountStatus;
  createdAt: string;
  updatedAt: string;
}

/** 创建 Schwab 账户时允许客户端提供的字段。 */
export interface CreateBrokerageAccountInput {
  displayName: string;
  legacyAccountKey: string;
  maskedSuffix: string;
  baseCurrency?: string;
}

/**
 * 创建经过规范化、可直接持久化的 Schwab 账户记录。
 *
 * UUID 由应用入口生成并传入，领域函数不隐式依赖随机源。基础币种缺省为 USD；
 * 后缀只用于显示和匹配线索，不在此处声明唯一。
 *
 * @param input - 用户确认的显示名、旧账户键、脱敏后缀和可选币种。
 * @param accountId - 服务端生成的不可变账户 UUID。
 * @param now - 服务端可信 UTC 时钟。
 */
export function createBrokerageAccount(
  input: CreateBrokerageAccountInput,
  accountId: string,
  now: Date,
): BrokerageAccount {
  const id = requireText(accountId, "账户 ID");
  const displayName = requireText(input.displayName, "账户显示名");
  const legacyAccountKey = requireText(input.legacyAccountKey, "旧账户键");
  const maskedSuffix = requireText(input.maskedSuffix, "账户脱敏后缀").toUpperCase();
  const baseCurrency = (input.baseCurrency ?? "USD").trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(baseCurrency)) {
    throw new RangeError("账户基础币种必须是三个大写字母");
  }
  const timestamp = requireTimestamp(now);

  return {
    id,
    displayName,
    legacyAccountKey,
    broker: "schwab",
    maskedSuffix,
    baseCurrency,
    status: "active",
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

/** 更新用户可编辑的显示名，不改变账户身份、币种或历史时间。 */
export function renameBrokerageAccount(
  account: BrokerageAccount,
  displayName: string,
  now: Date,
): BrokerageAccount {
  const normalizedName = requireText(displayName, "账户显示名");
  if (normalizedName === account.displayName) {
    return account;
  }
  return {
    ...account,
    displayName: normalizedName,
    updatedAt: requireTimestamp(now),
  };
}

/**
 * 归档账户而非删除；重复归档保持幂等且不伪造新的更新时间。
 * 关联交易、FIFO、Gmail 映射和审计记录均由持久化层继续保留。
 */
export function archiveBrokerageAccount(
  account: BrokerageAccount,
  now: Date,
): BrokerageAccount {
  if (account.status === "archived") {
    return account;
  }
  return {
    ...account,
    status: "archived",
    updatedAt: requireTimestamp(now),
  };
}

/** 去除边界空白并拒绝空业务标识。 */
function requireText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 把可信时钟转换为 UTC ISO 时间戳，并拒绝无效 Date。 */
function requireTimestamp(now: Date): string {
  if (Number.isNaN(now.getTime())) {
    throw new RangeError("账户变更时间无效");
  }
  return now.toISOString();
}
