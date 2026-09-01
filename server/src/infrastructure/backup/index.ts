/** 加密备份基础设施公共导出。 */
export {
  BackupIntegrityError,
  decryptBackupFile,
  encryptBackupFile,
  verifyEncryptedBackup,
} from "./encrypted-backup.js";
export type {
  DecryptBackupInput,
  EncryptBackupInput,
  EncryptedBackupManifest,
} from "./encrypted-backup.js";
