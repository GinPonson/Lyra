import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
  randomUUID,
  type CipherGCM,
  type Hash,
} from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { link, open, rm, stat, writeFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";
import { Readable, Transform, Writable } from "node:stream";
import { pipeline } from "node:stream/promises";

/** Lyra 加密备份的固定文件标识。 */
const BACKUP_MAGIC = Buffer.from("LYRABK01", "ascii");

/** AES-GCM 推荐的 96 位随机 IV。 */
const BACKUP_IV_BYTES = 12;

/** AES-GCM 128 位认证标签长度。 */
const BACKUP_AUTH_TAG_BYTES = 16;

/** 防止损坏文件声明超大 JSON 头部。 */
const MAX_BACKUP_HEADER_BYTES = 16 * 1024;

/** 加密备份认证、结构或明文完整性校验失败。 */
export class BackupIntegrityError extends Error {
  /** @param message - 不包含文件内容、密钥或宿主路径的安全错误说明。 */
  constructor(message: string, cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "BackupIntegrityError";
  }
}

/** 作为 AAD 写入备份头部的版本化完整性清单。 */
export interface EncryptedBackupManifest {
  format: "lyra-encrypted-backup";
  formatVersion: 1;
  algorithm: "aes-256-gcm";
  backupId: string;
  createdAt: string;
  schemaVersion: number;
  iv: string;
  sourceBytes: number;
  sourceSha256: string;
}

/** 创建加密备份所需的输入。 */
export interface EncryptBackupInput {
  sourcePath: string;
  destinationPath: string;
  key: Buffer;
  schemaVersion: number;
  now: Date;
}

/** 解密已验证备份所需的输入。 */
export interface DecryptBackupInput {
  backupPath: string;
  destinationPath: string;
  key: Buffer;
}

/**
 * 流式加密一个已经由写协调器完成检查点的 DuckDB 文件。
 *
 * 目标文件使用硬链接原子发布且禁止覆盖；源文件会在加密前后分别计算哈希，
 * 若检查点文件在备份期间发生变化，结果不会发布。
 *
 * @param input - 源/目标路径、独立备份密钥、schema 版本和可信时钟。
 * @returns 已写入并认证的备份清单。
 */
export async function encryptBackupFile(
  input: EncryptBackupInput,
): Promise<EncryptedBackupManifest> {
  assertBackupKey(input.key);
  assertSchemaVersion(input.schemaVersion);
  if (resolve(input.sourcePath) === resolve(input.destinationPath)) {
    throw new RangeError("备份源文件和目标文件不能相同");
  }
  const sourceStat = await stat(input.sourcePath);
  if (!sourceStat.isFile()) {
    throw new RangeError("备份源必须是普通文件");
  }

  const initial = await hashFile(input.sourcePath);
  const iv = randomBytes(BACKUP_IV_BYTES);
  const manifest: EncryptedBackupManifest = {
    format: "lyra-encrypted-backup",
    formatVersion: 1,
    algorithm: "aes-256-gcm",
    backupId: randomUUID(),
    createdAt: input.now.toISOString(),
    schemaVersion: input.schemaVersion,
    iv: iv.toString("base64url"),
    sourceBytes: initial.bytes,
    sourceSha256: initial.sha256,
  };
  const header = Buffer.from(JSON.stringify(manifest), "utf8");
  if (header.byteLength > MAX_BACKUP_HEADER_BYTES) {
    throw new RangeError("加密备份头部超过允许上限");
  }
  const prefix = Buffer.alloc(BACKUP_MAGIC.byteLength + 4);
  BACKUP_MAGIC.copy(prefix, 0);
  prefix.writeUInt32BE(header.byteLength, BACKUP_MAGIC.byteLength);

  const temporaryPath = createTemporaryPath(input.destinationPath);
  try {
    await writeFile(temporaryPath, Buffer.concat([prefix, header]), {
      flag: "wx",
      mode: 0o600,
    });
    const sourceHash = new HashingTransform();
    const cipher = createCipheriv("aes-256-gcm", input.key, iv, {
      authTagLength: BACKUP_AUTH_TAG_BYTES,
    });
    cipher.setAAD(header);
    const appendTag = new AppendAuthTagTransform(cipher);
    await pipeline(
      createReadStream(input.sourcePath),
      sourceHash,
      cipher,
      appendTag,
      createWriteStream(temporaryPath, { flags: "a", mode: 0o600 }),
    );
    const encryptedSource = sourceHash.finishHash();
    if (
      encryptedSource.bytes !== initial.bytes
      || encryptedSource.sha256 !== initial.sha256
    ) {
      throw new BackupIntegrityError("备份期间源文件发生变化，备份已拒绝");
    }
    await publishExclusive(temporaryPath, input.destinationPath);
    return manifest;
  } catch (error: unknown) {
    await cleanupTemporaryPath(temporaryPath, error);
    throw error;
  }
}

/**
 * 完整验证备份头部、AES-GCM 标签、明文字节数和 SHA-256。
 *
 * @param backupPath - 待验证的 Lyra 加密备份。
 * @param key - 精确 32 字节的备份密钥。
 */
export async function verifyEncryptedBackup(
  backupPath: string,
  key: Buffer,
): Promise<EncryptedBackupManifest> {
  assertBackupKey(key);
  const envelope = await readBackupEnvelope(backupPath);
  const plaintextHash = new HashingTransform();
  try {
    await pipeline(
      createEncryptedPayloadStream(backupPath, envelope),
      createBackupDecipher(key, envelope),
      plaintextHash,
      createDiscardSink(),
    );
  } catch (error: unknown) {
    throw new BackupIntegrityError("加密备份认证失败或密文已损坏", error);
  }
  assertPlaintextMatchesManifest(plaintextHash.finishHash(), envelope.manifest);
  return envelope.manifest;
}

/**
 * 验证并流式解密备份到一个不存在的新文件。
 *
 * 该函数只负责恢复候选文件，不替换运行中的 DuckDB；调用方仍须在隔离位置
 * 执行 schema、账本哈希与 FIFO 审计后，才能进入人工确认的替换步骤。
 */
export async function decryptBackupFile(
  input: DecryptBackupInput,
): Promise<EncryptedBackupManifest> {
  assertBackupKey(input.key);
  if (resolve(input.backupPath) === resolve(input.destinationPath)) {
    throw new RangeError("加密备份和解密目标不能相同");
  }
  const envelope = await readBackupEnvelope(input.backupPath);
  const temporaryPath = createTemporaryPath(input.destinationPath);
  try {
    const plaintextHash = new HashingTransform();
    await pipeline(
      createEncryptedPayloadStream(input.backupPath, envelope),
      createBackupDecipher(input.key, envelope),
      plaintextHash,
      createWriteStream(temporaryPath, { flags: "wx", mode: 0o600 }),
    );
    assertPlaintextMatchesManifest(plaintextHash.finishHash(), envelope.manifest);
    await publishExclusive(temporaryPath, input.destinationPath);
    return envelope.manifest;
  } catch (error: unknown) {
    await cleanupTemporaryPath(temporaryPath, error);
    if (error instanceof BackupIntegrityError) {
      throw error;
    }
    throw new BackupIntegrityError("加密备份认证失败或密文已损坏", error);
  }
}

/** 已解析并通过结构校验的备份信封。 */
interface BackupEnvelope {
  manifest: EncryptedBackupManifest;
  header: Buffer;
  iv: Buffer;
  authTag: Buffer;
  payloadStart: number;
  payloadBytes: number;
}

/** 流式计算文件的 SHA-256 和字节数。 */
async function hashFile(path: string): Promise<PlaintextHash> {
  const hash = createHash("sha256");
  let bytes = 0;
  for await (const chunk of createReadStream(path)) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    hash.update(buffer);
    bytes += buffer.byteLength;
  }
  return { bytes, sha256: hash.digest("hex") };
}

/** 明文完整性计算结果。 */
interface PlaintextHash {
  bytes: number;
  sha256: string;
}

/** 在不缓存完整文件的情况下记录经过的明文字节。 */
class HashingTransform extends Transform {
  private readonly hash: Hash = createHash("sha256");
  private bytes = 0;
  private finished = false;

  override _transform(
    chunk: Buffer,
    _encoding: BufferEncoding,
    callback: (error?: Error | null, data?: Buffer) => void,
  ): void {
    this.hash.update(chunk);
    this.bytes += chunk.byteLength;
    callback(null, chunk);
  }

  /** pipeline 完成后取得一次性摘要。 */
  finishHash(): PlaintextHash {
    if (this.finished) {
      throw new Error("备份明文摘要已经读取");
    }
    this.finished = true;
    return { bytes: this.bytes, sha256: this.hash.digest("hex") };
  }
}

/** 在密文流尾部追加 GCM 认证标签。 */
class AppendAuthTagTransform extends Transform {
  constructor(private readonly cipher: CipherGCM) {
    super();
  }

  override _transform(
    chunk: Buffer,
    _encoding: BufferEncoding,
    callback: (error?: Error | null, data?: Buffer) => void,
  ): void {
    callback(null, chunk);
  }

  override _flush(callback: (error?: Error | null) => void): void {
    try {
      this.push(this.cipher.getAuthTag());
      callback();
    } catch (error: unknown) {
      callback(error instanceof Error ? error : new Error("无法读取备份认证标签"));
    }
  }
}

/** 读取并严格验证固定头部、JSON 清单和末尾认证标签。 */
async function readBackupEnvelope(path: string): Promise<BackupEnvelope> {
  const handle = await open(path, "r");
  try {
    const fileStat = await handle.stat();
    const prefixBytes = BACKUP_MAGIC.byteLength + 4;
    const prefix = Buffer.alloc(prefixBytes);
    await readExactly(handle, prefix, 0);
    if (!prefix.subarray(0, BACKUP_MAGIC.byteLength).equals(BACKUP_MAGIC)) {
      throw new BackupIntegrityError("不是受支持的 Lyra 加密备份格式");
    }
    const headerBytes = prefix.readUInt32BE(BACKUP_MAGIC.byteLength);
    if (headerBytes <= 0 || headerBytes > MAX_BACKUP_HEADER_BYTES) {
      throw new BackupIntegrityError("加密备份头部长度无效");
    }
    const payloadStart = prefixBytes + headerBytes;
    const payloadBytes = fileStat.size - payloadStart - BACKUP_AUTH_TAG_BYTES;
    if (payloadBytes < 0) {
      throw new BackupIntegrityError("加密备份缺少密文或认证标签");
    }

    const header = Buffer.alloc(headerBytes);
    await readExactly(handle, header, prefixBytes);
    const authTag = Buffer.alloc(BACKUP_AUTH_TAG_BYTES);
    await readExactly(handle, authTag, fileStat.size - BACKUP_AUTH_TAG_BYTES);
    const manifest = parseBackupManifest(header);
    const iv = Buffer.from(manifest.iv, "base64url");
    if (iv.byteLength !== BACKUP_IV_BYTES) {
      throw new BackupIntegrityError("加密备份 IV 长度无效");
    }
    return { manifest, header, iv, authTag, payloadStart, payloadBytes };
  } finally {
    await handle.close();
  }
}

/** 从文件句柄的指定位置读取完整缓冲区，禁止接受截断数据。 */
async function readExactly(
  handle: Awaited<ReturnType<typeof open>>,
  target: Buffer,
  position: number,
): Promise<void> {
  const result = await handle.read(target, 0, target.byteLength, position);
  if (result.bytesRead !== target.byteLength) {
    throw new BackupIntegrityError("加密备份文件被截断");
  }
}

/** 解析并验证版本 1 备份清单的必要字段。 */
function parseBackupManifest(header: Buffer): EncryptedBackupManifest {
  let value: unknown;
  try {
    value = JSON.parse(header.toString("utf8"));
  } catch (error: unknown) {
    throw new BackupIntegrityError("加密备份头部不是有效 JSON", error);
  }
  if (!isRecord(value)) {
    throw new BackupIntegrityError("加密备份清单结构无效");
  }
  if (
    value.format !== "lyra-encrypted-backup"
    || value.formatVersion !== 1
    || value.algorithm !== "aes-256-gcm"
    || typeof value.backupId !== "string"
    || typeof value.createdAt !== "string"
    || typeof value.schemaVersion !== "number"
    || !Number.isSafeInteger(value.schemaVersion)
    || value.schemaVersion < 0
    || typeof value.iv !== "string"
    || typeof value.sourceBytes !== "number"
    || !Number.isSafeInteger(value.sourceBytes)
    || value.sourceBytes < 0
    || typeof value.sourceSha256 !== "string"
    || !/^[a-f0-9]{64}$/.test(value.sourceSha256)
  ) {
    throw new BackupIntegrityError("加密备份清单字段无效");
  }
  return value as unknown as EncryptedBackupManifest;
}

/** 创建只覆盖密文正文、不包含尾部认证标签的读取流。 */
function createEncryptedPayloadStream(
  path: string,
  envelope: BackupEnvelope,
): Readable {
  if (envelope.payloadBytes === 0) {
    return Readable.from([]);
  }
  return createReadStream(path, {
    start: envelope.payloadStart,
    end: envelope.payloadStart + envelope.payloadBytes - 1,
  });
}

/** 使用头部 AAD 和尾部标签建立解密器。 */
function createBackupDecipher(
  key: Buffer,
  envelope: BackupEnvelope,
): ReturnType<typeof createDecipheriv> {
  const decipher = createDecipheriv("aes-256-gcm", key, envelope.iv, {
    authTagLength: BACKUP_AUTH_TAG_BYTES,
  });
  decipher.setAAD(envelope.header);
  decipher.setAuthTag(envelope.authTag);
  return decipher;
}

/** 比较解密后的完整明文与受认证清单。 */
function assertPlaintextMatchesManifest(
  actual: PlaintextHash,
  manifest: EncryptedBackupManifest,
): void {
  if (actual.bytes !== manifest.sourceBytes || actual.sha256 !== manifest.sourceSha256) {
    throw new BackupIntegrityError("加密备份明文哈希与清单不一致");
  }
}

/** 产生与目标同目录的唯一临时文件名，保证后续硬链接可原子发布。 */
function createTemporaryPath(destinationPath: string): string {
  return join(
    dirname(destinationPath),
    `.${basename(destinationPath)}.${randomUUID()}.tmp`,
  );
}

/** 使用硬链接排他发布，目标已存在时由文件系统明确拒绝。 */
async function publishExclusive(temporaryPath: string, destinationPath: string): Promise<void> {
  await link(temporaryPath, destinationPath);
  await rm(temporaryPath);
}

/** 清理失败流程产生的临时文件，并保留原始错误与清理错误。 */
async function cleanupTemporaryPath(temporaryPath: string, originalError: unknown): Promise<void> {
  try {
    await rm(temporaryPath, { force: true });
  } catch (cleanupError: unknown) {
    throw new AggregateError(
      [originalError, cleanupError],
      "备份失败且临时文件清理失败",
    );
  }
}

/** 创建不会缓存明文的验证输出端。 */
function createDiscardSink(): Writable {
  return new Writable({
    write(_chunk, _encoding, callback) {
      callback();
    },
  });
}

/** 备份密钥必须是独立提供的 AES-256 密钥。 */
function assertBackupKey(key: Buffer): void {
  if (key.byteLength !== 32) {
    throw new RangeError("加密备份密钥必须恰好为 32 字节");
  }
}

/** schema 版本写入受认证清单前必须有效。 */
function assertSchemaVersion(schemaVersion: number): void {
  if (!Number.isSafeInteger(schemaVersion) || schemaVersion < 0) {
    throw new RangeError("备份 schemaVersion 必须是非负安全整数");
  }
}

/** 将未知 JSON 值收窄为普通对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
