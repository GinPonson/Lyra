import { createHash, type Hash } from "node:crypto";
import { Transform, type TransformCallback } from "node:stream";
import { ApplicationError } from "./app-error.js";

/** 第一阶段明确支持的导入文件类型。 */
export type ImportFileType = "csv" | "xlsx";

/** Multipart 流开始后执行格式预检所需的最小元数据。 */
export interface InspectImportUploadInput {
  fileName: string;
  sizeBytes: number;
  leadingBytes: Uint8Array;
  maxSizeBytes: number;
}

/** 通过预检、可以交给正式 CSV/Excel 解析器的上传描述。 */
export interface InspectedImportUpload {
  fileName: string;
  fileType: ImportFileType;
  sizeBytes: number;
}

/** 流式接收完成后交给预览流程的不可变文件摘要。 */
export interface ImportUploadSummary extends InspectedImportUpload {
  sha256: string;
  leadingBytes: Uint8Array;
}

/** 上传流监控器的边界配置。 */
export interface ImportUploadMonitorOptions {
  fileName: string;
  maxSizeBytes: number;
  sampleSizeBytes?: number;
}

/** XLSX 是 ZIP 容器，正常工作簿必须以 local file header 开始。 */
const XLSX_MAGIC = Buffer.from([0x50, 0x4b, 0x03, 0x04]);

/** 旧版二进制 XLS 使用的 OLE Compound File 标识。 */
const LEGACY_XLS_MAGIC = Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]);

/** 足以识别文件签名且不会显著占用服务端内存的默认样本大小。 */
const DEFAULT_SAMPLE_SIZE_BYTES = 4096;

/**
 * 在 multipart 上传管道中透传字节，同时执行硬大小限制、SHA-256 和头部采样。
 *
 * 调用方应把该 Transform 接入临时文件写入管道；管道成功后调用 `summary()`。
 * 当大小超限时，当前超限块不会继续写入，调用方仍须删除此前的临时文件。
 */
export class ImportUploadMonitor extends Transform {
  readonly #fileName: string;
  readonly #maxSizeBytes: number;
  readonly #leadingBytes: Buffer;
  readonly #hash: Hash = createHash("sha256");
  #sizeBytes = 0;
  #sampledBytes = 0;
  #summary: ImportUploadSummary | null = null;

  /** @param options - 原始文件名、上传硬上限及可选头部采样上限。 */
  constructor(options: ImportUploadMonitorOptions) {
    super();
    if (!Number.isSafeInteger(options.maxSizeBytes) || options.maxSizeBytes <= 0) {
      throw new RangeError("导入文件大小上限必须是正安全整数");
    }
    const sampleSizeBytes = options.sampleSizeBytes ?? DEFAULT_SAMPLE_SIZE_BYTES;
    if (!Number.isSafeInteger(sampleSizeBytes) || sampleSizeBytes <= 0) {
      throw new RangeError("导入文件样本大小必须是正安全整数");
    }
    this.#fileName = options.fileName;
    this.#maxSizeBytes = options.maxSizeBytes;
    this.#leadingBytes = Buffer.alloc(sampleSizeBytes);
  }

  /** 统计并原样转发一个二进制块；超限块不会传给下游。 */
  override _transform(
    chunk: Buffer,
    _encoding: BufferEncoding,
    callback: TransformCallback,
  ): void {
    if (!Buffer.isBuffer(chunk)) {
      callback(new TypeError("导入上传流必须提供二进制数据"));
      return;
    }
    const nextSizeBytes = this.#sizeBytes + chunk.byteLength;
    if (!Number.isSafeInteger(nextSizeBytes) || nextSizeBytes > this.#maxSizeBytes) {
      callback(validationError("导入文件超过允许的大小上限"));
      return;
    }

    const remainingSampleBytes = this.#leadingBytes.byteLength - this.#sampledBytes;
    if (remainingSampleBytes > 0) {
      const copiedBytes = chunk.copy(
        this.#leadingBytes,
        this.#sampledBytes,
        0,
        Math.min(chunk.byteLength, remainingSampleBytes),
      );
      this.#sampledBytes += copiedBytes;
    }
    this.#hash.update(chunk);
    this.#sizeBytes = nextSizeBytes;
    callback(null, chunk);
  }

  /**
   * 在上传管道成功结束后返回文件类型、大小、哈希和有界头部样本。
   * 重复调用返回同一份摘要，不会二次结束哈希状态。
   */
  summary(): ImportUploadSummary {
    if (!this.writableFinished) {
      throw new Error("导入上传流尚未成功结束，不能生成摘要");
    }
    if (this.#summary !== null) {
      return this.#summary;
    }
    const leadingBytes = Buffer.from(
      this.#leadingBytes.subarray(0, this.#sampledBytes),
    );
    const inspected = inspectImportUpload({
      fileName: this.#fileName,
      sizeBytes: this.#sizeBytes,
      leadingBytes,
      maxSizeBytes: this.#maxSizeBytes,
    });
    this.#summary = Object.freeze({
      ...inspected,
      sha256: this.#hash.digest("hex"),
      leadingBytes,
    });
    return this.#summary;
  }
}

/**
 * 在创建临时文件和进入解析器前检查导入上传边界。
 *
 * 该函数只识别扩展名、大小和文件签名，不尝试解析 CSV 或 ZIP；正式内容仍
 * 必须交给标准库处理，并继续复用统一八列标准化内核。
 *
 * @param input - 客户端文件名、流式计数大小、开头样本和配置上限。
 */
export function inspectImportUpload(
  input: InspectImportUploadInput,
): InspectedImportUpload {
  if (!Number.isSafeInteger(input.maxSizeBytes) || input.maxSizeBytes <= 0) {
    throw new RangeError("导入文件大小上限必须是正安全整数");
  }
  if (!Number.isSafeInteger(input.sizeBytes) || input.sizeBytes <= 0) {
    throw validationError("导入文件不能为空");
  }
  if (input.sizeBytes > input.maxSizeBytes) {
    throw validationError("导入文件超过允许的大小上限");
  }
  if (input.leadingBytes.byteLength === 0 || input.leadingBytes.byteLength > input.sizeBytes) {
    throw validationError("导入文件开头样本与文件大小不一致");
  }

  const fileName = input.fileName.trim();
  if (fileName.length === 0 || /[/\\]/.test(fileName) || fileName === "." || fileName === "..") {
    throw validationError("导入文件名无效");
  }
  const extensionMatch = /\.([^.]+)$/.exec(fileName);
  const extension = extensionMatch?.[1]?.toLowerCase() ?? "";
  const leading = Buffer.from(input.leadingBytes);

  if (extension === "xlsx") {
    if (!startsWithBytes(leading, XLSX_MAGIC)) {
      throw validationError("XLSX 文件签名无效");
    }
    return { fileName, fileType: "xlsx", sizeBytes: input.sizeBytes };
  }
  if (extension === "csv") {
    if (startsWithBytes(leading, XLSX_MAGIC) || startsWithBytes(leading, LEGACY_XLS_MAGIC)) {
      throw validationError("CSV 扩展名与实际文件格式不一致");
    }
    if (leading.includes(0x00)) {
      throw validationError("CSV 开头包含 NUL 字节，无法作为文本导入");
    }
    return { fileName, fileType: "csv", sizeBytes: input.sizeBytes };
  }
  throw new ApplicationError({
    code: "IMPORT_UNSUPPORTED_FORMAT",
    message: "仅支持 .csv 和 .xlsx 导入文件",
    statusCode: 415,
    retryable: false,
    details: { supported_formats: ["csv", "xlsx"] },
  });
}

/** 比较文件样本是否以给定二进制签名开始。 */
function startsWithBytes(value: Buffer, prefix: Buffer): boolean {
  return value.byteLength >= prefix.byteLength
    && value.subarray(0, prefix.byteLength).equals(prefix);
}

/** 建立统一的上传内容校验错误。 */
function validationError(message: string): ApplicationError {
  return new ApplicationError({
    code: "IMPORT_VALIDATION_FAILED",
    message,
    statusCode: 422,
    retryable: false,
  });
}
