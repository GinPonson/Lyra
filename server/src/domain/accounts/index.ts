/** 账户生命周期领域能力的公共导出面。 */
export {
  archiveBrokerageAccount,
  createBrokerageAccount,
  renameBrokerageAccount,
} from "./account.js";
export type {
  AccountBroker,
  AccountStatus,
  BrokerageAccount,
  CreateBrokerageAccountInput,
} from "./account.js";
