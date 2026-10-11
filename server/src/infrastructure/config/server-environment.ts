import { z } from "zod";

/** 正式启动与部署前检查共用的基础环境校验；此模块不初始化数据库或服务。 */
export const environmentSchema = z.object({
  LYRA_HOST: z.string().trim().min(1).default("127.0.0.1"),
  LYRA_PORT: z.coerce.number().int().min(1).max(65_535).default(3_141),
  LYRA_DATABASE_PATH: z.string().trim().min(1),
  LYRA_IMPORT_STAGING_DIR: z.string().trim().min(1),
  /** 32 字节 hex 主密钥；Gmail refresh token 等敏感数据经 AES-256-GCM 加密。 */
  LYRA_SECRET_MASTER_KEY: z.string().regex(/^[0-9a-fA-F]{64}$/),
  LYRA_GOOGLE_CLIENT_ID: z.string().trim().min(1).default(""),
  LYRA_GOOGLE_CLIENT_SECRET: z.string().trim().min(1).default(""),
  LYRA_GOOGLE_REDIRECT_URI: z.string().trim().min(1).default(""),
});
