import type { PairedDeviceRecord } from "../infrastructure/security/index.js";

/** 账户中心可展示、可按 ID 撤销的设备最小公开信息。 */
export interface PublicPairedDevice {
  id: string;
  displayName: string;
  status: "active" | "revoked";
  pairedAt: string;
  lastSeenAt: string | null;
  revokedAt: string | null;
  updatedAt: string;
}

/**
 * 创建设备管理页面的稳定公开列表。
 *
 * 公钥 PEM、指纹和算法只供服务端认证，不进入 App 或本地缓存。活跃设备优先，
 * 同状态内按最近配对时间倒序，再用不可变 ID 保证稳定顺序。
 */
export function createPublicDeviceList(
  devices: readonly PairedDeviceRecord[],
): readonly PublicPairedDevice[] {
  return devices
    .toSorted((left, right) => (
      deviceStatusOrder(left.status) - deviceStatusOrder(right.status)
      || right.pairedAt.localeCompare(left.pairedAt)
      || left.id.localeCompare(right.id)
    ))
    .map((device) => ({
      id: device.id,
      displayName: device.displayName,
      status: device.status,
      pairedAt: device.pairedAt,
      lastSeenAt: device.lastSeenAt,
      revokedAt: device.revokedAt,
      updatedAt: device.updatedAt,
    }));
}

/** 活跃设备在安全管理页面优先于只读历史记录。 */
function deviceStatusOrder(status: PairedDeviceRecord["status"]): number {
  return status === "active" ? 0 : 1;
}
