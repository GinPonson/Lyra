import {
  createHash,
  createPublicKey,
  randomBytes,
  randomInt,
  randomUUID,
  timingSafeEqual,
} from "node:crypto";

/** 去除容易混淆的 I、L、O、0、1 后的配对码字符集。 */
const PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

/** 10 个 Base32 字符约提供 50 位随机性，并保持手工输入可接受。 */
const PAIRING_CODE_CHARACTERS = 10;

/** 避免每个认证请求都额外更新 paired_devices 的默认活动写入间隔。 */
export const DEFAULT_DEVICE_ACTIVITY_UPDATE_INTERVAL_MS = 5 * 60 * 1000;

/** 设备认证流程可安全映射到 API 的稳定错误码。 */
export type DeviceAuthErrorCode =
  | "PAIRING_CODE_INVALID"
  | "PAIRING_CODE_EXPIRED"
  | "PAIRING_CODE_USED"
  | "DEVICE_PUBLIC_KEY_INVALID"
  | "DEVICE_CREDENTIAL_INVALID"
  | "DEVICE_CREDENTIAL_EXPIRED"
  | "DEVICE_CREDENTIAL_REUSED"
  | "DEVICE_REVOKED";

/** 配对或设备公钥验证失败。 */
export class DeviceAuthError extends Error {
  /**
   * @param code - App 可以稳定分支处理的错误码。
   * @param message - 不包含配对码、令牌或私钥的安全错误说明。
   * @param cause - 可选底层公钥解析错误。
   */
  constructor(
    readonly code: DeviceAuthErrorCode,
    message: string,
    cause?: unknown,
  ) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "DeviceAuthError";
  }
}

/** 服务端内存或短期存储中保存的配对挑战，不含明文配对码。 */
export interface PairingChallengeRecord {
  codeHash: string;
  expiresAt: string;
  consumedAt: string | null;
}

/** 新建配对挑战时唯一一次返回给服务端操作者的明文码。 */
export interface CreatedPairingChallenge {
  code: string;
  record: PairingChallengeRecord;
}

/** 已规范化的设备公钥及其稳定指纹。 */
export interface NormalizedDevicePublicKey {
  pem: string;
  fingerprint: string;
  algorithm: string;
}

/** 一次生成的高熵明文凭据及服务端持久化哈希。 */
export interface CreatedOpaqueCredential {
  token: string;
  tokenHash: string;
}

/** 刷新凭据校验所需的最小设备安全状态。 */
export interface PairedDeviceAuthRecord {
  id: string;
  status: "active" | "revoked";
  revokedAt: string | null;
}

/** 与 paired_devices 表对齐且不包含设备私钥的完整持久化记录。 */
export interface PairedDeviceRecord extends PairedDeviceAuthRecord {
  displayName: string;
  publicKeyPem: string;
  publicKeyFingerprint: string;
  keyAlgorithm: string;
  pairedAt: string;
  lastSeenAt: string | null;
  updatedAt: string;
}

/** 与 DuckDB v3 schema 对齐的设备刷新凭据状态。 */
export interface DeviceRefreshCredentialRecord {
  id: string;
  deviceId: string;
  familyId: string;
  generation: number;
  tokenHash: string;
  status: "active" | "rotated" | "revoked";
  expiresAt: string;
  lastUsedAt: string | null;
  rotatedAt: string | null;
  revokedAt: string | null;
  createdAt: string;
}

/** 首次签发的明文刷新凭据及其可持久化记录。 */
export interface IssuedDeviceRefreshCredential {
  token: string;
  record: DeviceRefreshCredentialRecord;
}

/** 刷新成功后必须在同一数据库事务中应用的轮换结果。 */
export interface RotatedDeviceRefreshCredential {
  token: string;
  previous: DeviceRefreshCredentialRecord;
  next: DeviceRefreshCredentialRecord;
}

/** 设备撤销时需要原子写入的设备和凭据状态。 */
export interface DeviceRevocationPlan {
  device: PairedDeviceAuthRecord;
  credentials: readonly DeviceRefreshCredentialRecord[];
}

/** 刷新凭据重用后需要在同一事务中撤销的完整凭据族。 */
export interface RefreshCredentialFamilyRevocationPlan {
  deviceId: string;
  familyId: string;
  credentials: readonly DeviceRefreshCredentialRecord[];
}

/** 与 DuckDB v6 schema 对齐的短期访问令牌状态。 */
export interface DeviceAccessTokenRecord {
  id: string;
  deviceId: string;
  tokenHash: string;
  status: "active" | "revoked";
  expiresAt: string;
  lastSeenAt: string | null;
  revokedAt: string | null;
  issuedAt: string;
}

/** 签发时唯一一次返回的访问令牌及其可持久化哈希记录。 */
export interface IssuedDeviceAccessToken {
  token: string;
  record: DeviceAccessTokenRecord;
}

/**
 * 生成短时一次性设备配对码。
 *
 * @param now - 服务端当前时间，由应用层时钟提供。
 * @param ttlMilliseconds - 配对码有效期毫秒数。
 * @returns 显示用分组码和仅含 SHA-256 哈希的记录。
 */
export function createPairingChallenge(
  now: Date,
  ttlMilliseconds: number,
): CreatedPairingChallenge {
  if (!Number.isSafeInteger(ttlMilliseconds) || ttlMilliseconds <= 0) {
    throw new RangeError("配对码有效期必须是正安全整数毫秒数");
  }
  const rawCode = Array.from({ length: PAIRING_CODE_CHARACTERS }, () => (
    PAIRING_ALPHABET[randomInt(PAIRING_ALPHABET.length)]
  )).join("");
  const code = `${rawCode.slice(0, 5)}-${rawCode.slice(5)}`;
  return {
    code,
    record: {
      codeHash: hashPairingCode(rawCode),
      expiresAt: new Date(now.getTime() + ttlMilliseconds).toISOString(),
      consumedAt: null,
    },
  };
}

/**
 * 原子更新事务中校验并消费一次性配对码。
 *
 * @param record - 当前未修改的挑战记录。
 * @param candidate - App 提交的配对码，允许包含显示分隔符和空格。
 * @param now - 校验时间。
 * @returns consumedAt 已设置的新记录；原记录保持不变。
 * @throws DeviceAuthError 当配对码错误、过期或已使用时抛出。
 */
export function consumePairingChallenge(
  record: PairingChallengeRecord,
  candidate: string,
  now: Date,
): PairingChallengeRecord {
  if (record.consumedAt !== null) {
    throw new DeviceAuthError("PAIRING_CODE_USED", "配对码已经使用");
  }
  if (now.getTime() >= Date.parse(record.expiresAt)) {
    throw new DeviceAuthError("PAIRING_CODE_EXPIRED", "配对码已经过期");
  }
  const candidateHash = Buffer.from(hashPairingCode(candidate), "hex");
  const expectedHash = Buffer.from(record.codeHash, "hex");
  if (
    candidateHash.byteLength !== expectedHash.byteLength
    || !timingSafeEqual(candidateHash, expectedHash)
  ) {
    throw new DeviceAuthError("PAIRING_CODE_INVALID", "配对码无效");
  }
  return { ...record, consumedAt: now.toISOString() };
}

/**
 * 生成适合访问令牌或刷新凭据的 256 位随机不透明值。
 *
 * 服务端只持久化 tokenHash；明文 token 仅在签发响应中返回一次。
 */
export function createOpaqueCredential(): CreatedOpaqueCredential {
  const token = randomBytes(32).toString("base64url");
  return { token, tokenHash: hashOpaqueCredential(token) };
}

/** 对高熵随机凭据生成可持久化 SHA-256 哈希。 */
export function hashOpaqueCredential(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

/** 使用常量时间比较验证不透明凭据。 */
export function verifyOpaqueCredential(token: string, expectedHash: string): boolean {
  if (!/^[a-f0-9]{64}$/.test(expectedHash)) {
    return false;
  }
  return timingSafeEqual(
    Buffer.from(hashOpaqueCredential(token), "hex"),
    Buffer.from(expectedHash, "hex"),
  );
}

/**
 * 为已完成配对的设备签发第一代刷新凭据。
 *
 * @param deviceId - 已持久化设备 ID。
 * @param now - 服务端可信时钟。
 * @param ttlMilliseconds - 刷新凭据有效期毫秒数。
 * @returns 明文令牌和仅含哈希的持久化记录；明文只应返回一次。
 */
export function issueDeviceRefreshCredential(
  deviceId: string,
  now: Date,
  ttlMilliseconds: number,
): IssuedDeviceRefreshCredential {
  assertCredentialTtl(ttlMilliseconds);
  const credential = createOpaqueCredential();
  const timestamp = now.toISOString();
  return {
    token: credential.token,
    record: {
      id: randomUUID(),
      deviceId,
      familyId: randomUUID(),
      generation: 1,
      tokenHash: credential.tokenHash,
      status: "active",
      expiresAt: new Date(now.getTime() + ttlMilliseconds).toISOString(),
      lastUsedAt: null,
      rotatedAt: null,
      revokedAt: null,
      createdAt: timestamp,
    },
  };
}

/**
 * 校验并轮换一枚设备刷新凭据。
 *
 * 返回值是事务计划：调用方必须原子更新 previous 并插入 next。若旧令牌再次
 * 出现，状态检查会明确返回重用错误，调用方随后应撤销同一凭据族。
 *
 * @param device - 当前设备状态。
 * @param record - 按提交令牌哈希查得的刷新凭据记录。
 * @param token - App 提交的明文刷新凭据。
 * @param now - 服务端可信时钟。
 * @param ttlMilliseconds - 下一代凭据有效期毫秒数。
 */
export function rotateDeviceRefreshCredential(
  device: PairedDeviceAuthRecord,
  record: DeviceRefreshCredentialRecord,
  token: string,
  now: Date,
  ttlMilliseconds: number,
): RotatedDeviceRefreshCredential {
  assertCredentialTtl(ttlMilliseconds);
  if (record.deviceId !== device.id || !verifyOpaqueCredential(token, record.tokenHash)) {
    throw new DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备刷新凭据无效");
  }
  if (device.status === "revoked") {
    throw new DeviceAuthError("DEVICE_REVOKED", "设备已经撤销");
  }
  if (record.status !== "active") {
    throw new DeviceAuthError("DEVICE_CREDENTIAL_REUSED", "设备刷新凭据已使用或撤销");
  }
  if (now.getTime() >= Date.parse(record.expiresAt)) {
    throw new DeviceAuthError("DEVICE_CREDENTIAL_EXPIRED", "设备刷新凭据已经过期");
  }

  const nextCredential = createOpaqueCredential();
  const timestamp = now.toISOString();
  return {
    token: nextCredential.token,
    previous: {
      ...record,
      status: "rotated",
      lastUsedAt: timestamp,
      rotatedAt: timestamp,
    },
    next: {
      id: randomUUID(),
      deviceId: device.id,
      familyId: record.familyId,
      generation: record.generation + 1,
      tokenHash: nextCredential.tokenHash,
      status: "active",
      expiresAt: new Date(now.getTime() + ttlMilliseconds).toISOString(),
      lastUsedAt: null,
      rotatedAt: null,
      revokedAt: null,
      createdAt: timestamp,
    },
  };
}

/**
 * 在检测到旧刷新凭据重用后撤销同一凭据族的全部代次。
 *
 * 调用方应按提交令牌定位 `reusedCredential`，并在同一事务中锁定、读取该设备
 * 和 familyId 的完整凭据族后应用本计划。活动的最新代也会被撤销，阻止已泄漏
 * 的旧凭据与合法客户端继续竞速；已经撤销的记录保持幂等不变。
 *
 * @param reusedCredential - 本次被再次提交的 rotated 或 revoked 凭据。
 * @param familyCredentials - 数据库读取的同设备、同 familyId 全部代次。
 * @param now - 服务端确认重用的时间。
 * @returns 可在单事务中批量持久化的同族撤销计划。
 */
export function planRefreshCredentialReuseRevocation(
  reusedCredential: DeviceRefreshCredentialRecord,
  familyCredentials: readonly DeviceRefreshCredentialRecord[],
  now: Date,
): RefreshCredentialFamilyRevocationPlan {
  if (reusedCredential.status === "active") {
    throw new RangeError("活动刷新凭据不能作为重用撤销依据");
  }
  if (!familyCredentials.some((credential) => credential.id === reusedCredential.id)) {
    throw new RangeError("刷新凭据族不包含被重用的凭据");
  }
  if (familyCredentials.some((credential) => (
    credential.deviceId !== reusedCredential.deviceId
    || credential.familyId !== reusedCredential.familyId
  ))) {
    throw new RangeError("刷新凭据重用撤销计划包含其他设备或凭据族");
  }
  const timestamp = now.toISOString();
  return {
    deviceId: reusedCredential.deviceId,
    familyId: reusedCredential.familyId,
    credentials: familyCredentials.map((credential) => (
      credential.status === "revoked"
        ? credential
        : { ...credential, status: "revoked", revokedAt: timestamp }
    )),
  };
}

/**
 * 为活动设备签发短期不透明访问令牌。
 *
 * @param device - 已通过配对且尚未撤销的设备。
 * @param now - 服务端可信时钟。
 * @param ttlMilliseconds - 访问令牌有效期毫秒数。
 */
export function issueDeviceAccessToken(
  device: PairedDeviceAuthRecord,
  now: Date,
  ttlMilliseconds: number,
): IssuedDeviceAccessToken {
  assertCredentialTtl(ttlMilliseconds);
  if (device.status === "revoked") {
    throw new DeviceAuthError("DEVICE_REVOKED", "设备已经撤销");
  }
  const credential = createOpaqueCredential();
  return {
    token: credential.token,
    record: {
      id: randomUUID(),
      deviceId: device.id,
      tokenHash: credential.tokenHash,
      status: "active",
      expiresAt: new Date(now.getTime() + ttlMilliseconds).toISOString(),
      lastSeenAt: null,
      revokedAt: null,
      issuedAt: now.toISOString(),
    },
  };
}

/**
 * 校验短期访问令牌并返回更新 lastSeenAt 后的新记录。
 *
 * @param device - 令牌关联设备的当前状态。
 * @param record - 按提交令牌哈希查得的访问令牌记录。
 * @param token - App 提交的 Bearer 令牌明文。
 * @param now - 认证发生时间。
 */
export function authenticateDeviceAccessToken(
  device: PairedDeviceAuthRecord,
  record: DeviceAccessTokenRecord,
  token: string,
  now: Date,
): DeviceAccessTokenRecord {
  if (record.deviceId !== device.id || !verifyOpaqueCredential(token, record.tokenHash)) {
    throw new DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效");
  }
  if (device.status === "revoked") {
    throw new DeviceAuthError("DEVICE_REVOKED", "设备已经撤销");
  }
  if (record.status === "revoked") {
    throw new DeviceAuthError("DEVICE_CREDENTIAL_INVALID", "设备访问令牌无效");
  }
  if (now.getTime() >= Date.parse(record.expiresAt)) {
    throw new DeviceAuthError("DEVICE_CREDENTIAL_EXPIRED", "设备访问令牌已经过期");
  }
  return { ...record, lastSeenAt: now.toISOString() };
}

/**
 * 在访问令牌认证成功后生成低频的设备活动时间更新。
 *
 * 调用方只应在 `authenticateDeviceAccessToken` 成功后调用，并通过单写队列按
 * 设备 ID 与原 `updatedAt` 做比较并交换。间隔内或早于设备现有状态的观察不
 * 产生无收益写入，直接返回原记录。
 *
 * @param device - 当前完整活动设备记录。
 * @param observedAt - 本次成功认证的服务端可信时间。
 * @param minimumIntervalMs - 两次设备活动写入的最小间隔，默认五分钟。
 * @returns 需要持久化的新设备记录，或无需写入时的原对象。
 */
export function planPairedDeviceActivityUpdate(
  device: PairedDeviceRecord,
  observedAt: Date,
  minimumIntervalMs = DEFAULT_DEVICE_ACTIVITY_UPDATE_INTERVAL_MS,
): PairedDeviceRecord {
  if (device.status === "revoked") {
    throw new DeviceAuthError("DEVICE_REVOKED", "设备已经撤销");
  }
  if (!Number.isSafeInteger(minimumIntervalMs) || minimumIntervalMs < 0) {
    throw new RangeError("设备活动更新间隔必须是非负安全整数毫秒数");
  }
  const observedTime = observedAt.getTime();
  if (Number.isNaN(observedTime)) {
    throw new RangeError("设备活动时间无效");
  }
  const pairedTime = Date.parse(device.pairedAt);
  if (observedTime < pairedTime) {
    throw new RangeError("设备活动时间不能早于配对时间");
  }
  const latestKnownTime = Math.max(
    Date.parse(device.updatedAt),
    device.lastSeenAt === null ? pairedTime : Date.parse(device.lastSeenAt),
  );
  if (
    observedTime <= latestKnownTime
    || (
      device.lastSeenAt !== null
      && observedTime - Date.parse(device.lastSeenAt) < minimumIntervalMs
    )
  ) {
    return device;
  }
  const timestamp = observedAt.toISOString();
  return {
    ...device,
    lastSeenAt: timestamp,
    updatedAt: timestamp,
  };
}

/**
 * 设备撤销事务中将其所有活动访问令牌标记为 revoked。
 *
 * @param deviceId - 被撤销设备 ID。
 * @param records - 数据库按 deviceId 查询出的全部访问令牌。
 * @param now - 撤销时间。
 */
export function planDeviceAccessTokenRevocation(
  deviceId: string,
  records: readonly DeviceAccessTokenRecord[],
  now: Date,
): readonly DeviceAccessTokenRecord[] {
  if (records.some((record) => record.deviceId !== deviceId)) {
    throw new RangeError("访问令牌撤销计划包含其他设备记录");
  }
  const timestamp = now.toISOString();
  return records.map((record) => (
    record.status === "active"
      ? { ...record, status: "revoked", revokedAt: timestamp }
      : record
  ));
}

/**
 * 生成设备撤销事务计划，同时撤销该设备仍处于 active 的刷新凭据。
 *
 * @param device - 要撤销的设备。
 * @param credentials - 由数据库按 device.id 查询得到的全部凭据。
 * @param now - 撤销时间。
 */
export function planDeviceRevocation(
  device: PairedDeviceAuthRecord,
  credentials: readonly DeviceRefreshCredentialRecord[],
  now: Date,
): DeviceRevocationPlan {
  if (credentials.some((credential) => credential.deviceId !== device.id)) {
    throw new RangeError("撤销计划包含其他设备的刷新凭据");
  }
  const timestamp = device.revokedAt ?? now.toISOString();
  return {
    device: { ...device, status: "revoked", revokedAt: timestamp },
    credentials: credentials.map((credential) => (
      credential.status === "active"
        ? { ...credential, status: "revoked", revokedAt: timestamp }
        : credential
    )),
  };
}

/**
 * 解析并规范化 Android Keystore 导出的 PEM 公钥。
 *
 * @param pem - `PUBLIC KEY` PEM；明确拒绝客户端上传私钥。
 * @returns 规范 SPKI PEM、SHA-256 指纹和算法名。
 * @throws DeviceAuthError 当输入不是可识别公钥时抛出。
 */
export function normalizeDevicePublicKey(pem: string): NormalizedDevicePublicKey {
  if (/PRIVATE KEY/.test(pem)) {
    throw new DeviceAuthError("DEVICE_PUBLIC_KEY_INVALID", "设备只能提交公钥，不能提交私钥");
  }
  try {
    const key = createPublicKey(pem);
    const der = key.export({ type: "spki", format: "der" });
    const normalizedPem = key.export({ type: "spki", format: "pem" }).toString();
    return {
      pem: normalizedPem,
      fingerprint: createHash("sha256").update(der).digest("hex"),
      algorithm: key.asymmetricKeyType ?? "unknown",
    };
  } catch (error: unknown) {
    throw new DeviceAuthError("DEVICE_PUBLIC_KEY_INVALID", "设备公钥格式无效", error);
  }
}

/**
 * 在配对码已成功消费后创建可持久化的活动设备记录。
 *
 * 本函数只接受 `normalizeDevicePublicKey` 的结果，因此记录保存规范 SPKI 公钥、
 * 稳定指纹和算法名，不接触设备私钥。设备 ID 与可信时钟由应用层显式提供。
 *
 * @param deviceId - 服务端生成的不可变设备 UUID。
 * @param displayName - 用户可识别的设备名称。
 * @param publicKey - 已规范化的 Android Keystore 公钥。
 * @param now - 配对事务提交时间。
 */
export function createPairedDeviceRecord(
  deviceId: string,
  displayName: string,
  publicKey: NormalizedDevicePublicKey,
  now: Date,
): PairedDeviceRecord {
  const id = requireDeviceText(deviceId, "设备 ID");
  const name = requireDeviceText(displayName, "设备名称");
  if (Number.isNaN(now.getTime())) {
    throw new RangeError("设备配对时间无效");
  }
  const timestamp = now.toISOString();
  return {
    id,
    displayName: name,
    publicKeyPem: publicKey.pem,
    publicKeyFingerprint: publicKey.fingerprint,
    keyAlgorithm: publicKey.algorithm,
    status: "active",
    pairedAt: timestamp,
    lastSeenAt: null,
    revokedAt: null,
    updatedAt: timestamp,
  };
}

/** 统一配对码的大小写、显示横线和空格后再哈希。 */
function hashPairingCode(code: string): string {
  const normalized = code.toUpperCase().replace(/[-\s]/g, "");
  return createHash("sha256").update(normalized, "utf8").digest("hex");
}

/** 规范设备持久化所需的短文本字段。 */
function requireDeviceText(value: string, label: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new RangeError(`${label}不能为空`);
  }
  return normalized;
}

/** 两个刷新流程共用同一有效期约束。 */
function assertCredentialTtl(ttlMilliseconds: number): void {
  if (!Number.isSafeInteger(ttlMilliseconds) || ttlMilliseconds <= 0) {
    throw new RangeError("设备凭据有效期必须是正安全整数毫秒数");
  }
}
