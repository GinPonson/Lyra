import { ApplicationError } from "./app-error.js";

/** 与 DuckDB import_files.status 一致的导入作业状态。 */
export type ImportJobStatus =
  | "uploaded"
  | "parsing"
  | "preview_ready"
  | "committing"
  | "committed"
  | "rejected"
  | "failed"
  | "cancelled";

/** 判断状态转换所需的最小导入作业快照。 */
export interface ImportJobState {
  importId: string;
  status: ImportJobStatus;
  updatedAt: string;
}

/** 合法转换图；终态不存在出边。 */
const IMPORT_JOB_TRANSITIONS: Readonly<Record<ImportJobStatus, readonly ImportJobStatus[]>> = {
  uploaded: ["parsing", "cancelled"],
  parsing: ["preview_ready", "rejected", "failed"],
  preview_ready: ["committing", "cancelled"],
  committing: ["committed", "failed"],
  committed: [],
  rejected: [],
  failed: [],
  cancelled: [],
};

/**
 * 验证并生成一次导入作业状态更新。
 *
 * 相同状态请求视为幂等重放并返回原对象；其他转换必须遵守完整预览后再提交的
 * 顺序。持久化层仍须使用 `WHERE status = current.status` 做比较并交换更新。
 *
 * @param current - 数据库读取的当前作业状态。
 * @param nextStatus - 请求进入的目标状态。
 * @param now - 服务端可信时钟。
 */
export function transitionImportJob(
  current: ImportJobState,
  nextStatus: ImportJobStatus,
  now: Date,
): ImportJobState {
  if (current.status === nextStatus) {
    return current;
  }
  if (!IMPORT_JOB_TRANSITIONS[current.status].includes(nextStatus)) {
    throw new ApplicationError({
      code: "IMPORT_STATE_CONFLICT",
      message: "导入作业状态已变化，请刷新后重试",
      statusCode: 409,
      retryable: false,
      details: {
        current_status: current.status,
        requested_status: nextStatus,
      },
    });
  }
  if (Number.isNaN(now.getTime())) {
    throw new RangeError("导入作业更新时间无效");
  }
  return {
    ...current,
    status: nextStatus,
    updatedAt: now.toISOString(),
  };
}
