import { randomUUID } from "node:crypto";
import { createWriteStream } from "node:fs";
import { rm, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import {
  ImportUploadMonitor,
  type ImportUploadSummary,
} from "../../application/index.js";

/** 安全暂存一次 multipart 导入上传所需的参数。 */
export interface StageImportUploadInput {
  stream: Readable;
  stagingDirectory: string;
  fileName: string;
  maxSizeBytes: number;
}

/** 已完整写入、通过格式预检且可交给标准解析器的临时文件。 */
export interface StagedImportUpload {
  filePath: string;
  summary: ImportUploadSummary;
  /** 解析或请求结束后删除临时文件；重复调用保持幂等，清理失败会显式抛出。 */
  dispose(): Promise<void>;
}

/**
 * 将上传流写入服务端生成的私有临时文件，并同步完成大小、哈希和格式检查。
 *
 * 客户端文件名永远不会参与路径拼接。任一步骤失败时会删除临时文件；成功后
 * 返回的 dispose 明确承接成功路径生命周期，调用方必须在解析或请求结束时调用。
 *
 * @param input - 上传流、受信任的暂存目录、客户端文件名和字节上限。
 */
export async function stageImportUpload(
  input: StageImportUploadInput,
): Promise<StagedImportUpload> {
  const stagingDirectory = resolve(input.stagingDirectory);
  const directoryStat = await stat(stagingDirectory);
  if (!directoryStat.isDirectory()) {
    throw new RangeError("导入暂存路径必须是目录");
  }

  const filePath = join(stagingDirectory, `.lyra-import-${randomUUID()}.upload`);
  const monitor = new ImportUploadMonitor({
    fileName: input.fileName,
    maxSizeBytes: input.maxSizeBytes,
  });
  try {
    await pipeline(
      input.stream,
      monitor,
      createWriteStream(filePath, { flags: "wx", mode: 0o600 }),
    );
    return {
      filePath,
      summary: monitor.summary(),
      dispose: async () => {
        await rm(filePath, { force: true });
      },
    };
  } catch (error: unknown) {
    try {
      await rm(filePath, { force: true });
    } catch (cleanupError: unknown) {
      throw new AggregateError(
        [error, cleanupError],
        "导入暂存失败且临时文件清理失败",
      );
    }
    throw error;
  }
}
