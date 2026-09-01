import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { WriteCoordinator } from "../src/application/index.js";

/** 创建由测试显式释放的 Promise，用于观察任务是否发生重叠。 */
function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolvePromise: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    resolvePromise = resolve;
  });
  if (resolvePromise === undefined) {
    throw new Error("无法创建测试同步点");
  }
  return { promise, resolve: resolvePromise };
}

describe("WriteCoordinator", () => {
  it("严格按入队顺序执行写任务且不发生重叠", async () => {
    const coordinator = new WriteCoordinator();
    const gate = deferred();
    const events: string[] = [];

    const first = coordinator.enqueue(async () => {
      events.push("first:start");
      await gate.promise;
      events.push("first:end");
      return 1;
    });
    const second = coordinator.enqueue(async () => {
      events.push("second:start");
      events.push("second:end");
      return 2;
    });

    await Promise.resolve();
    assert.deepEqual(coordinator.status(), { accepting: true, pendingOperations: 2 });
    assert.deepEqual(events, ["first:start"]);
    gate.resolve();
    assert.deepEqual(await Promise.all([first, second]), [1, 2]);
    assert.deepEqual(events, ["first:start", "first:end", "second:start", "second:end"]);
    assert.deepEqual(coordinator.status(), { accepting: true, pendingOperations: 0 });
  });

  it("把任务错误返回给调用方但继续执行后续任务", async () => {
    const coordinator = new WriteCoordinator();
    const failed = coordinator.enqueue(async () => {
      throw new Error("transaction rolled back");
    });
    const next = coordinator.enqueue(async () => "committed");

    await assert.rejects(failed, /transaction rolled back/);
    assert.equal(await next, "committed");
  });

  it("关闭时等待已排队任务，并拒绝关闭后的新任务", async () => {
    const coordinator = new WriteCoordinator();
    const gate = deferred();
    const task = coordinator.enqueue(async () => {
      await gate.promise;
      return "done";
    });
    const closing = coordinator.close();

    await assert.rejects(
      coordinator.enqueue(async () => "late"),
      /正在关闭/,
    );
    gate.resolve();
    assert.equal(await task, "done");
    await closing;
    await coordinator.close();
    assert.deepEqual(coordinator.status(), { accepting: false, pendingOperations: 0 });
  });
});
