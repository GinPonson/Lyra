/**
 * 在单个 Node 进程内串行执行所有 DuckDB 写操作。
 *
 * DuckDB 适配器仍负责开启、提交和回滚具体事务；本类只保证写任务不会并发。
 * 一个任务失败不会污染队列，后续已经排队的任务仍会按顺序执行。
 */
export interface WriteCoordinatorStatus {
  accepting: boolean;
  /** 包含当前正在执行及等待执行的全部写任务。 */
  pendingOperations: number;
}

export class WriteCoordinator {
  private tail: Promise<void> = Promise.resolve();
  private accepting = true;
  private pendingOperations = 0;

  /**
   * 把一个写任务追加到单写队列。
   *
   * @param operation - 在轮到该任务时执行的异步写操作。
   * @returns operation 的原始结果或错误。
   * @throws Error 当协调器已开始关闭时拒绝新任务。
   */
  enqueue<T>(operation: () => Promise<T>): Promise<T> {
    if (!this.accepting) {
      return Promise.reject(new Error("写协调器正在关闭，不能接受新任务"));
    }

    this.pendingOperations += 1;
    const result = this.tail.then(operation);
    const tracked = result.then(
      (value) => {
        this.pendingOperations -= 1;
        return value;
      },
      (error: unknown) => {
        this.pendingOperations -= 1;
        throw error;
      },
    );
    // 队列尾部只承担排队职责；任务错误由 result 返回给对应调用方，不能让
    // rejected Promise 阻断后续写任务。
    this.tail = tracked.then(
      () => undefined,
      () => undefined,
    );
    return tracked;
  }

  /** 返回健康检查和指标可安全读取的瞬时队列状态。 */
  status(): WriteCoordinatorStatus {
    return {
      accepting: this.accepting,
      pendingOperations: this.pendingOperations,
    };
  }

  /**
   * 停止接收新任务，并等待当前及已排队任务全部结束。
   *
   * 多次调用 close 是幂等的，适合服务关闭钩子并发等待同一个安全检查点。
   */
  async close(): Promise<void> {
    this.accepting = false;
    await this.tail;
  }
}
