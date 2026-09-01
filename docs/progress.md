# Lyra 实现进度

> 本表只记录已经落盘的实现和实际验证结果。未通过验证的功能不得标记为完成。  
> 总体方案见 [Lyra 移动资产账本与 AI Agent 应用完整方案](plans/lyra-product-technical-design.md)。

## 进度状态

| 状态 | 含义 |
| --- | --- |
| 待开始 | 尚未实现 |
| 进行中 | 已有实现，但验证或验收尚未完成 |
| 已完成 | 功能实现完成，并已执行对应验证 |
| 阻塞 | 存在已确认的外部条件阻塞；必须记录原因 |

## 功能进度表

| 阶段 | 功能点 | 状态 | 验证证据 | 日期 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Phase 0 | Schwab Python 现有测试基线 | 已完成 | `.venv/bin/pytest -q`：71 项通过 | 2026-08-30 | 已有项目虚拟环境可复用；`uv` 从零建环境仍受代理不可用影响 |
| Phase 1 | TypeScript 工作区与严格编译基线 | 已完成 | `npm run check` 通过 | 2026-08-30 | 单一 `server` workspace；TypeScript strict 编译通过 |
| Phase 1 | Schwab 基础字段解析 | 已完成 | 9 项测试通过 | 2026-08-30 | 已覆盖金额、日期、期权符号、账户文件名及拒绝路径 |
| Phase 1 | 交易行标准化与金额勾稽 | 已完成 | 7 项新增测试通过；累计 16 项通过 | 2026-08-30 | 覆盖股票/期权/现金分类、100 倍期权乘数、拒绝路径和 Python golden 哈希 |
| Phase 1 | 整批标准化与预览核心 | 已完成 | 3 项新增测试通过；累计 19 项通过 | 2026-08-30 | 全批校验、重复出现次数、动作统计和行号错误定位 |
| Phase 1 | Schwab CSV 严格解析与导入预览 | 已完成 | 2 项新增适配器测试通过；TypeScript strict 编译及完整回归累计 243 项通过 | 2026-08-31 | 安装并精确锁定 csv-parse 7.0.2；支持 UTF-8 BOM、CRLF、逗号与标准双引号转义，忽略空物理行；严格拒绝列数不一致、未闭合引号和非法 UTF-8；解析结果只转换为统一八列批次，并复用既有账户解析、金额勾稽、动作/期权规则、哈希与预览内核 |
| Phase 1 | Schwab Excel 导入适配 | 已完成 | 2 项新增 XLSX 适配器测试通过；TypeScript strict 编译及完整回归累计 245 项通过 | 2026-08-31 | 安装并精确锁定 ExcelJS 4.4.0；选择唯一可见标准八列工作表；支持日期、文本、数值、富文本与空单元格；拒绝隐藏候选表、隐藏交易行、公式、合并单元格、多个候选表及无法读取/加密工作簿；转换后复用 CSV 相同的账户、金融校验、哈希与预览内核 |
| Phase 1 | RFC MIME 文件导入适配 | 已完成 | 2 项新增适配器测试通过；TypeScript strict 编译及完整回归累计 251 项通过 | 2026-08-31 | 精确锁定 mailparser 3.9.17 与类型包 3.4.6；解码原始 RFC 5322 multipart、Base64、plain/HTML 后复用既有正文规范化和 eConfirm 金融内核；只接受已确认 Schwab 发件地址和主题，附件正文不进入候选；消息 ID 按 Gmail 扩展头、Message-ID、原文 SHA-256 依次确定，保证 `.eml` 重复导入幂等；原始 MIME 仍只在受控临时处理期存在 |
| Phase 1 | CSV/XLSX 上传前置安全检查 | 已完成 | 3 项新增测试通过；累计 106 项通过 | 2026-08-30 | 限制扩展名、大小和路径式文件名；XLSX 校验 ZIP 签名；CSV 拒绝格式伪装与 NUL 字节；实际表格解析仍使用标准库 |
| Phase 1 | 导入上传流式大小、哈希与头部采样 | 已完成 | 2 项新增测试通过；累计 108 项通过 | 2026-08-30 | 上传字节原样透传；不缓冲整文件；超限块不写入下游；单次生成 SHA-256 与 4 KiB 有界样本供预览和格式检查复用 |
| Phase 1 | 导入上传私有临时文件暂存 | 已完成 | 2 项新增测试通过；累计 110 项通过 | 2026-08-30 | 服务端随机路径、不使用客户端文件名拼接、0600 权限；上传失败删除部分文件，清理失败不静默吞错 |
| Phase 1 | 成功暂存导入文件显式生命周期清理 | 已完成 | 扩展既有暂存上传测试；累计 217 项通过 | 2026-08-31 | 成功结果随附幂等 `dispose()`，解析或请求终态可明确删除 0600 临时文件；重复清理安全；清理失败继续显式抛出，不静默遗留原始上传；失败路径原有的部分文件自动清理保持不变 |
| Phase 1 | Schwab 标准八列表头与行桥接 | 已完成 | 2 项新增测试通过；累计 112 项通过 | 2026-08-30 | CSV/Excel 解析结果共用固定列序；支持首列表头 UTF-8 BOM；拒绝缺列、重排、额外列和非文本单元格，并使用稳定错误码 |
| Phase 1 | 显式多账户范围与币种分组 | 已完成 | 2 项新增测试通过；累计 114 项通过 | 2026-08-30 | App 账户 UUID 精确映射到账本旧键；缺省范围被拒绝；all 只含活跃账户；归档账户可显式查询；异币种不生成伪合计 |
| Phase 1 | Schwab 账户创建、重命名与归档领域规则 | 已完成 | 2 项新增测试通过；累计 116 项通过 | 2026-08-30 | 创建默认 USD 并规范化字段；重命名不改变身份；只归档不删除；重复归档幂等且不伪造更新时间 |
| Phase 2 | App 与 Agent 账户列表安全公开投影 | 已完成 | 1 项新增测试通过；累计 209 项通过 | 2026-08-31 | 共用公开模型仅返回内部 ID、显示名、Schwab、脱敏后缀、币种、状态和时间；迁移期 `legacyAccountKey` 不进入 App 或 Agent；活跃账户优先，再按创建时间和 ID 稳定排序；不修改仓储输入快照 |
| Phase 2 | App 只读账本响应权威数据语境 | 已完成 | 2 项新增测试通过；累计 211 项通过 | 2026-08-31 | 总览、持仓、分析和流水可统一携带服务端解析的账户范围、账户 ID、币种分组、可合计币种、账本版本、截止时间和获取时间；多币种明确返回 `aggregateCurrency=null`；带时区时间统一为 UTC；非法版本、无时区时间和无效服务端时钟明确拒绝；不复制或重算金融结果 |
| Phase 2 | App 账本明细安全公开投影 | 已完成 | 2 项新增测试通过；累计 238 项通过 | 2026-08-31 | 持仓、到期、交易、已实现和现金流明细统一把迁移期 Schwab 旧账户键映射为 Lyra 账户 UUID，所有 Decimal 显式输出十进制字符串；交易只公开可解释的 csv/excel/gmail 来源类型，不公开来源引用、文件摘要、业务/交易哈希、物理顺序或 occurrence；已实现明细不公开开平仓交易哈希；范围外账户行在投影边界明确拒绝 |
| Phase 1 | 文件导入账户线索显式映射 | 已完成 | 2 项新增测试通过；累计 118 项通过 | 2026-08-30 | 仅完整旧账户键可自动匹配；脱敏后缀只生成候选；显式选择仍校验线索；归档账户不能作为新导入目标 |
| Phase 1 | 导入作业状态机与冲突错误 | 已完成 | 2 项新增测试通过；累计 120 项通过 | 2026-08-30 | 强制 uploaded→parsing→preview_ready→committing→committed；终态不可回退，提交期间不可取消；相同状态重放幂等；新增 IMPORT_STATE_CONFLICT |
| Phase 1 | 短时导入预览确认令牌与 DuckDB v7 | 已完成 | 2 项新增测试及 DuckDB 1.5.5 migration smoke 通过；累计 122 项通过 | 2026-08-31 | 明文令牌只返回一次，数据库仅存 SHA-256；绑定导入 ID、预览指纹、有效期和 preview_ready 状态；提交须 CAS 进入 committing；v7 可重复执行 |
| Phase 1 | FIFO 纯领域重放内核 | 已完成 | 10 项新增测试通过；累计 29 项通过 | 2026-08-30 | 覆盖股票/期权多空、Expired、Assigned、费用尾差、顺序歧义和幂等 |
| Phase 1 | 首批只读分析 | 已完成 | 4 项新增测试通过；累计 33 项通过 | 2026-08-30 | 已实现显式账户范围下的 positions、expiring、summary |
| Phase 1 | 月度、配置与归因分析 | 已完成 | 3 项新增测试通过；累计 36 项通过 | 2026-08-30 | 复现 monthly、allocation、attribution 的 Python 核心样本 |
| Phase 1 | 回撤与期权风险分析 | 已完成 | 2 项新增测试通过；累计 38 项通过 | 2026-08-30 | 复现 drawdown、risk 的 Python 核心样本和风险口径 |
| Phase 1 | 持有期、极值、连续盈亏与压力测试 | 已完成 | 2 项新增测试通过；累计 40 项通过 | 2026-08-30 | 共用平仓交易聚合，复现 holding-period、extremes、streaks、stress |
| Phase 1 | 账本完整性只读审计 | 已完成 | 2 项新增测试通过；累计 50 项通过 | 2026-08-30 | 覆盖有效账本及“新增交易未进入旧 FIFO 结果”的陈旧重放检测，同时检查孤立记录和非法数量 |
| Phase 1 | 交易、已实现与现金流基础查询 | 已完成 | 4 项新增测试通过；累计 57 项通过 | 2026-08-30 | 所有查询显式携带账户范围，覆盖标的、动作、日期、上限、排序和分类合计 |
| Phase 1 | 交易流水稳定游标分页 | 已完成 | 1 项新增测试并扩展 Agent schema 测试；累计 208 项通过 | 2026-08-31 | 交易按日期、文件顺序、账户、来源与哈希形成确定性顺序；不透明游标绑定账户范围和筛选条件；跨页无重复；非法、跨范围或已不在当前筛选快照中的游标明确拒绝；页长限制 1–200，末页 `nextCursor=null`；App 与 Agent `trades` 共用 `cursor` 契约 |
| Phase 1 | 已实现与现金流水稳定游标分页 | 已完成 | 1 项新增测试并扩展 Agent schema 测试；累计 226 项通过 | 2026-08-31 | realized 使用账户及开/平仓交易哈希生成不可逆定位键，cashflow 使用交易哈希；两者均确定性排序，游标绑定查询类型、账户范围和筛选条件，禁止跨工具复用；页长统一 1–200；`count` 表示当前页，`totalCount` 与完整范围 `totalPnl`/`total`/`byAction` 不随翻页变化；Agent 两工具同步支持 limit/cursor |
| Phase 1 | 标准交易来源溯源、流水过滤与 DuckDB v9 | 已完成 | 扩展既有标准化、Gmail、查询和 Agent schema 测试；DuckDB 1.5.5 migration smoke 通过；累计 205 项通过 | 2026-08-31 | ParsedTransaction 显式携带 csv/excel/gmail 与可审计来源引用；调用方必须提供来源，标准化器不从文件名猜测；Gmail 固定使用 message ID；交易、现金流水和 Agent 工具统一支持闭域来源过滤；v9 视图公开来源列；来源不进入业务哈希，保持跨来源重复交易幂等 |
| Phase 2 | 设备配对与不透明凭据安全基础 | 已完成 | 4 项新增测试通过；累计 82 项通过 | 2026-08-30 | 配对码仅保存 SHA-256 摘要并一次性消费；设备公钥规范化并生成指纹；设备凭据使用 256 位随机令牌与常量时间校验 |
| Phase 2 | 已配对设备持久化创建计划 | 已完成 | 1 项新增测试通过；累计 218 项通过 | 2026-08-31 | 一次性挑战消费后，以服务端设备 ID、用户设备名、规范 SPKI 公钥、SHA-256 指纹、算法和可信 UTC 时钟创建 active `paired_devices` 记录；不接触或保存私钥；空名称与无效时钟明确拒绝 |
| Phase 2 | App 已配对设备安全公开列表 | 已完成 | 1 项新增测试通过；累计 219 项通过 | 2026-08-31 | 账户中心仅公开设备 ID、名称、active/revoked 状态及配对、最后活动、撤销和更新时间；活跃设备优先，同状态按最近配对时间和 ID 稳定排序；公钥 PEM、指纹和算法不进入 App 或本地缓存；不修改仓储输入顺序 |
| Phase 2 | 成功认证后的低频设备活动更新 | 已完成 | 1 项新增测试通过；累计 227 项通过 | 2026-08-31 | 访问令牌认证成功后可生成 paired_devices 活动更新计划，默认五分钟最多写一次；首次活动与跨间隔活动更新 lastSeenAt/updatedAt，间隔内或旧时间观察返回原记录，避免每个只读请求额外写设备表；撤销设备、无效时间及非法间隔明确拒绝；仓储使用原 updatedAt 做 CAS |
| Phase 2 | 配对设备与刷新凭据 DuckDB schema | 已完成 | DuckDB 1.5.5 迁移 smoke 通过；既有 85 项测试通过 | 2026-08-30 | v3 迁移可重复执行；设备公钥指纹唯一；刷新凭据只存哈希，支持凭据族代际轮换及设备撤销状态，不持久化配对码或明文令牌 |
| Phase 2 | 设备刷新凭据轮换与撤销状态转换 | 已完成 | 3 项新增测试通过；累计 88 项通过 | 2026-08-30 | 首次签发只持久化哈希；轮换保留凭据族并递增代次；明确拒绝过期、重用和已撤销设备；撤销设备同步撤销 active 凭据 |
| Phase 2 | 刷新凭据重用后的同族全代次撤销 | 已完成 | 2 项新增测试通过；累计 213 项通过 | 2026-08-31 | 被重用的 rotated/revoked 凭据作为明确依据；同一设备与 familyId 的旧代和当前 active 代在单事务计划中全部撤销，阻止泄漏旧令牌与合法客户端继续竞速；重复执行幂等；活动凭据伪触发、计划缺少被重用记录以及跨设备/跨凭据族输入明确拒绝 |
| Phase 2 | 短期设备访问令牌与 DuckDB schema | 已完成 | 2 项新增测试及 DuckDB 1.5.5 migration smoke 通过；累计 96 项通过 | 2026-08-30 | 访问令牌只返回一次且仅持久化哈希；校验设备、过期和撤销状态；v6 迁移可重复执行并支持批量撤销 |
| Phase 2 | Flutter Android App 工程与首个可运行页面 | 已完成 | `flutter analyze` 无问题；`flutter test` 1 项通过；`flutter build apk --debug` 成功；Android CLI 在 Medium_Phone 安装、启动、截图与布局树检查通过 | 2026-08-31 | Flutter 3.47.2 / Dart 3.13.2；实现总览、持仓、分析、流水、Agent 五主导航，手机底部导航与宽屏 NavigationRail 自适应；统一 Material Icons Outlined，无表情符号；断开态不伪造金融数据；设备截图见 `plans/assets/lyra-android-overview.png` |
| Phase 3 | Schwab eConfirm 确定性解析内核 | 已完成 | 7 项新增测试通过；累计 47 项通过 | 2026-08-30 | 覆盖动作映射、费用、勾稽、多成交、账户和正文候选冲突 |
| Phase 3 | eConfirm 到标准账本桥接 | 已完成 | 1 项新增端到端领域测试；累计 48 项通过 | 2026-08-30 | 邮件成交转换为 Schwab 八列并复用统一标准化、勾稽和哈希 |
| Phase 3 | 多账户邮件归属与只读对账 | 已完成 | 3 项新增测试通过；累计 53 项通过 | 2026-08-30 | 尾号仅在唯一候选时自动解析；重复成交逐笔消费，并区分 matched、missing、conflict |
| Phase 3 | Gmail `format=full` 消息适配 | 已完成 | 3 项新增测试通过；累计 60 项通过 | 2026-08-30 | 递归解析内联 plain/HTML、Base64URL 与 charset，校验 Schwab 发件人和主题；raw MIME 仍待依赖 |
| Phase 3 | Gmail refresh token 静态加密 | 已完成 | 2 项新增测试通过；累计 75 项通过 | 2026-08-30 | AES-256-GCM 随机 IV；密文绑定连接上下文；错误上下文、篡改和非法主密钥明确失败且不回退 |
| Phase 3 | Gmail 连接、映射与消息元数据 schema | 已完成 | DuckDB 1.5.5 迁移 smoke 通过 | 2026-08-30 | 迁移可重复执行；账户尾号映射唯一；仅保存消息摘要和交易关联，不保存正文或明文令牌 |
| Phase 3 | Gmail history 增量与重扫状态机 | 已完成 | 3 项初始测试并扩展游标规范化断言；累计 205 项通过 | 2026-08-31 | active 只走增量；首次扫描和游标失效必须确认起始日期；成功后才推进游标；状态机与 REST 层统一只接受不转 Number 的正十进制 history ID，同步完成时间规范为 UTC ISO |
| Phase 3 | Gmail 每小时自动同步触发计划 | 已完成 | 2 项新增测试通过；累计 207 项通过 | 2026-08-31 | 默认 60 分钟；到期仅复用 active 增量请求，未到期返回下一时间；已有同步任务时不重叠；pending/needs_resync/disconnected/error 不由定时器自动运行，用户确认重扫门禁保持不变；手动同步不受到期判断限制 |
| Phase 3 | Google OAuth state、PKCE 与 scope 安全事务 | 已完成 | 3 项新增测试通过；累计 101 项通过 | 2026-08-30 | 32 字节随机 state/PKCE，state 仅存哈希并绑定设备、过期和一次性消费；实际授权必须包含 gmail.readonly，不保存授权码或 token |
| Phase 3 | Schwab eConfirm 有界 Gmail 查询策略 | 已完成 | 2 项新增测试通过；累计 103 项通过 | 2026-08-30 | 仅查询样本确认的 donotreply@mail.schwab.com 和固定主题；UTC epoch 起始日；任务级消息硬上限；解析层拒绝其他 schwab.com 发件邮箱 |
| Phase 3 | Google 已验证身份到 Gmail 连接凭据计划 | 已完成 | 2 项新增测试通过；累计 124 项通过 | 2026-08-31 | 只接受已验证邮箱身份和实际 gmail.readonly scope；access token 不进入接口或记录；refresh token 以连接 ID 为 AAD 加密；断开时清除密文且幂等 |
| Phase 3 | App Gmail 连接安全公开投影 | 已完成 | 1 项新增测试通过；累计 217 项通过 | 2026-08-31 | 账户中心仅公开连接 ID、已验证邮箱、状态、稳定错误码、最近同步和创建/更新时间；Google subject、授权 scope、history 游标与 refresh token 密文只留在服务端，不进入 App 响应或本地缓存 |
| Phase 3 | App Gmail 多账户映射安全公开列表 | 已完成 | 1 项新增测试通过；累计 222 项通过 | 2026-08-31 | 指定连接的映射页仅公开映射 ID、邮件内脱敏账户尾号、Lyra 账户 UUID 和创建/更新时间，并按尾号与 ID 稳定排序；路由已确定的 connectionId 不在每条响应重复；混入其他连接的仓储记录明确拒绝，首次确认及禁止普通同步改绑的既有规则保持不变 |
| Phase 3 | Gmail 映射用户显式改绑计划 | 已完成 | 2 项新增测试通过；累计 225 项通过 | 2026-08-31 | 仅映射管理写接口使用独立更新计划；验证映射属于当前连接、目标账户活跃且尾号匹配，返回旧 accountId 供仓储 CAS；相同目标幂等，跨连接、归档或不匹配目标明确拒绝；普通同步仍不能改绑，历史 Gmail 消息与既有账本交易不回写 |
| Phase 3 | Gmail 连接同步错误码闭集 | 已完成 | 扩展既有连接与状态机测试；累计 217 项通过 | 2026-08-31 | `lastErrorCode` 仅允许 GMAIL_HISTORY_EXPIRED、GMAIL_INCREMENTAL_LIMIT_EXCEEDED、GMAIL_RESYNC_CONFIRMATION_REQUIRED、GMAIL_AUTH_REVOKED、GMAIL_CONNECTION_ERROR；history 失效转换只接受对应稳定码；任意底层 Google 错误文本或历史脏值在 App 公开边界明确拒绝，不使用兜底文案掩盖 |
| Phase 3 | Google OAuth 轻量 REST 交换与身份验证 | 已完成 | 2 项新增测试通过；累计 160 项通过 | 2026-08-31 | Node 原生 fetch 完成 PKCE 授权码交换和 userinfo；申请 openid/email/gmail.readonly，强制离线 refresh token 与已验证邮箱；授权码、client secret、access token 和 Google 原始错误正文不进入连接记录或客户端错误 |
| Phase 3 | Gmail 有界 messages REST 取数 | 已完成 | 2 项新增测试通过；累计 162 项通过 | 2026-08-31 | Node 原生 fetch 分页调用 messages.list 并逐封获取 format=full；每页额度服从任务剩余额度，达到用户确认硬上限即停止并标记 truncated；单请求 15 秒超时；401/403 要求重新授权，429/5xx 映射可重试 503；token 与 Google 错误正文不外泄 |
| Phase 3 | Google access token 后台刷新 | 已完成 | 2 项新增测试通过；累计 164 项通过 | 2026-08-31 | 复用 Google token REST 边界，以加密存储解密出的 refresh token 获取短期 access token；Google 省略 scope 时沿用并重新校验连接范围；不返回或轮换 refresh token；400/401/403 明确进入 GMAIL_AUTH_REVOKED，原始错误正文不读取 |
| Phase 3 | Gmail history REST 增量取数 | 已完成 | 2 项新增测试通过；累计 166 项通过 | 2026-08-31 | history.list 只读取 messageAdded，候选 message ID 去重并逐封获取 format=full；返回 observed historyId；达到任务硬上限标记 truncated 且禁止上层推进游标；404 映射 GMAIL_HISTORY_EXPIRED；起始游标参数与 Google 响应错误分离 |
| Phase 3 | Gmail 增量截断到重扫状态门禁 | 已完成 | 2 项新增测试通过；累计 168 项通过 | 2026-08-31 | 完整增量只生成候选 completion historyId，仍等待消息持久化后推进；截断不暴露可推进游标，保留旧 historyId 并进入 needs_resync/GMAIL_INCREMENTAL_LIMIT_EXCEEDED；后续同步强制要求用户确认有界重扫日期 |
| Phase 3 | Gmail 首次扫描 profile/history 基线 | 已完成 | 2 项新增测试通过；累计 170 项通过 | 2026-08-31 | 首扫先调用 users.getProfile 固定邮箱身份与 history 基线，再执行有界 messages 查询，扫描期间新邮件由后续增量覆盖；profile 邮箱必须匹配授权身份；仅完整扫描返回 completion historyId，截断结果无可推进游标 |
| Phase 3 | Gmail history 元数据隐私预筛选 | 已完成 | 既有 history REST 测试扩展；累计 170 项通过 | 2026-08-31 | history 无查询过滤能力，先对新增 ID 请求仅含 From/Subject 的 metadata；只有确认的 donotreply@mail.schwab.com 与 Schwab eConfirms 主题才下载 format=full；无关邮件仅计数，不进入正文解析、持久化或 Agent 上下文 |
| Phase 3 | Gmail 文本 attachment 正文补取 | 已完成 | 2 项新增测试通过；累计 172 项通过 | 2026-08-31 | format=full 的 text/plain 或 text/html part 只有 attachmentId 时调用 attachments.get 并注入 Base64URL data，直接复用既有 full 解析器；其他 MIME 附件不下载；非法 attachment id/data 明确失败且不返回部分记录 |
| Phase 3 | 加密 Gmail 连接到 access token 刷新桥接 | 已完成 | 2 项新增测试通过；累计 174 项通过 | 2026-08-31 | 仅以 gmail-connection:<id> AAD 解密连接 refresh token 并换取短期 access token；已断开连接不发请求；主密钥不匹配或密文损坏明确报连接错误；返回、消息和错误均不含 refresh token 或密文 |
| Phase 3 | Google Gmail 远端授权撤销 | 已完成 | 2 项新增测试通过；累计 176 项通过 | 2026-08-31 | refresh token 通过 POST form 提交 Google revoke endpoint；仅收到成功响应后才清除本地密文并标记 disconnected；网络/远端失败保留原连接用于重试，429/5xx 标记 retryable，Google 错误正文不读取 |
| Phase 3 | Android Google 系统授权 URL 契约 | 已完成 | 2 项新增测试通过；累计 178 项通过 | 2026-08-31 | 固定 Google 官方授权端点、S256 PKCE、openid/email/gmail.readonly、offline access、consent 与增量 scope；返回 App 的对象仅含 authorization URL 和过期时间，不包含 codeVerifier、state hash、client secret 或服务端事务记录 |
| Phase 3 | Gmail 解析消息最小元数据与内容摘要 | 已完成 | 2 项新增测试通过；累计 126 项通过 | 2026-08-31 | 规范换行后计算 SHA-256；时间统一为 UTC；仅持久化 ID、摘要、状态、计数和游标，不保存正文；非法时间明确失败 |
| Phase 3 | Gmail 消息到账本原子写入计划 | 已完成 | 2 项新增测试通过；累计 128 项通过 | 2026-08-31 | 邮件成交复用 Schwab 八列、标准化、去重和 FIFO 内核；生成消息/交易关联且不复制正文；相同已导入 message ID 幂等跳过，内容或账户变化使用 GMAIL_MESSAGE_CONFLICT 明确拒绝 |
| Phase 3 | Gmail 多账户显式映射规则 | 已完成 | 2 项新增测试通过；累计 130 项通过 | 2026-08-31 | 首次尾号即使只有一个候选也必须由用户确认；已有映射仅在目标账户活跃时复用；普通同步不得改绑，归档账户不得接收新邮件交易；错误仅返回候选 UUID |
| Phase 3 | Gmail 同步批次 history 推进门禁 | 已完成 | 2 项新增测试通过；累计 132 项通过 | 2026-08-31 | 候选 message ID 去重；仅当每条消息均已有持久终态时推进游标；临时失败、结果缺失或批次外结果均保留旧游标；摘要不含正文和底层错误 |
| Phase 3 | Gmail 失败消息无正文归档 | 已完成 | 2 项新增测试通过；累计 134 项通过 | 2026-08-31 | 成功与失败共用内容摘要规则；解析拒绝记录 GMAIL_MESSAGE_REJECTED，账户待映射和同 ID 变化记录 conflict；仅保存稳定错误码，不保存正文或底层错误详情 |
| Phase 3 | Schwab 官方文件定期对账提醒与 DuckDB v8 | 已完成 | 2 项新增测试及 DuckDB 1.5.5 migration smoke 通过；累计 205 项通过 | 2026-08-31 | 按账户根据最近一次成功官方文件导入生成确定性计划；默认一个自然月并正确钳制月末日期，可关闭或调整为 1–24 个月；v8 设置表强制周期约束，视图只从 committed 官方文件推导最近日期，Gmail 同步不会重置提醒 |
| Phase 4 | Agent 只读工具注册表与切换策略 | 已完成 | 4 项新增测试通过；累计 71 项通过 | 2026-08-30 | 统一 17 个金融工具白名单；服务端覆盖范围字段；仅在回合边界切换，不可用时不自动回退 |
| Phase 4 | Agent 统一流式事件序号 | 已完成 | 2 项新增测试通过；累计 73 项通过 | 2026-08-30 | 统一 11 种事件信封；会话内序号可从持久化位置续接，并可检测缺号或跨会话拼接 |
| Phase 4 | Agent CLI 隔离可用性探测 | 已完成 | 2 项新增测试通过；累计 92 项通过；本机三 CLI 实测通过 | 2026-08-30 | 绝对路径、空临时目录、无 shell、仅 PATH 环境、超时及输出上限；实测 Claude 2.1.233、Pi 0.84.2、OpenCode 1.18.21，未启动模型会话 |
| Phase 4 | Agent CLI 精确版本准入 | 已完成 | 1 项新增测试并扩展既有探测测试；累计 203 项通过 | 2026-08-31 | 探测成功后仍须命中管理员配置的适配器契约版本白名单；不自行猜测 semver 兼容范围，未验证版本稳定返回 VERSION_NOT_ALLOWED 并标记 unavailable；白名单为空或格式非法时启动配置明确失败 |
| Phase 4 | 三 Agent 公开可用状态快照 | 已完成 | 2 项新增测试通过；累计 215 项通过 | 2026-08-31 | 同一隔离探测批次固定输出 Claude、Pi、OpenCode，避免异步完成顺序影响 App；可用项公开已验证版本，不可用项公开稳定失败原因；不含可执行路径、stderr 或认证信息；三者任一缺失、重复或批次完成时间无效时明确拒绝，不自动替换供应商 |
| Phase 4 | App Agent 会话安全公开快照 | 已完成 | 1 项新增测试通过；累计 220 项通过 | 2026-08-31 | App 重连和会话页仅接收会话 ID、供应商、状态、账户范围、账本版本、截止时间、revision 与规范可见消息；逐字段复制账户范围和消息，避免客户端修改污染仓储快照；内部 handoff summary、隐藏推理及供应商私有协议状态不进入响应 |
| Phase 4 | App Agent 工具调用安全公开列表 | 已完成 | 1 项新增测试通过；累计 221 项通过 | 2026-08-31 | 会话流按开始时间、消息内顺序和调用 ID 稳定展示工具名、状态、扁平有界参数/结果摘要、账本版本、耗时、稳定错误码与时间；动态摘要独立复制；内部 authorizationDecision 和由路由确定的 sessionId 不进入 App 响应 |
| Phase 4 | 官方 MCP Streamable HTTP transport | 已完成 | 2 项官方协议端点集成测试通过；累计 253 项测试通过 | 2026-08-31 | 精确锁定 `@modelcontextprotocol/sdk` 1.30.0；在现有 Fastify 进程中提供可选 `/mcp` 无状态 POST 路由，不创建第二套会话；每个请求先按令牌哈希加载服务端快照，再校验 running revision、时效、账户范围和账本基线；只从既有 Tool Registry 公开已配置工具，复用 SDK AJV 校验同一份 JSON Schema；GET/SSE 与 DELETE 明确返回 405；响应后关闭本次 transport |
| Phase 4 | Agent 会话读取与短期 MCP 凭据运行时存储 | 已完成 | 2 项真实 DuckDB/运行时存储测试通过；累计 255 项测试通过 | 2026-08-31 | 从 v5 既有表以单次一致性查询恢复会话、账户 UUID、账本基线、摘要和按 ordinal 排序的规范消息；短期 MCP 凭据仅以哈希保存在服务进程内，支持使用时间更新和按会话撤销，服务重启即失效；不新增迁移、不保存明文令牌，也不把短期凭据伪装成持久登录态 |
| Phase 4 | MCP 核心真实金融查询 handlers | 已完成 | 2 项正式导入后真实 DuckDB 测试通过；累计 257 项测试通过 | 2026-08-31 | `accounts`、`summary`、`positions`、`expiring` 已连接同一条 SQL 返回的账本版本、账户、交易、lots 与 realized 快照；复用既有确定性分析内核和 App 安全投影；`all` summary 按基础币种分组，旧账户键与交易哈希不公开；Agent 基线落后时明确返回账本版本冲突，不读取新版本数据 |
| Phase 4 | MCP 账本明细真实 handlers | 已完成 | 扩展真实 DuckDB 与工具定义聚焦测试；累计 257 项测试通过 | 2026-08-31 | `trades`、`realized`、`cashflow` 已复用既有稳定 cursor、筛选和 App 安全投影；交易/已实现哈希及来源引用不公开；`trades.action` schema 直接复用标准账本的 Schwab 动作闭集，不再接受任意字符串 |
| Phase 4 | MCP 剩余 10 个确定性分析 handlers | 已完成 | 扩展正式导入后真实 DuckDB 聚焦测试；累计 257 项测试通过 | 2026-08-31 | `monthly`、`allocation`、`attribution`、`drawdown`、`risk`、`holding-period`、`extremes`、`streaks`、`stress`、`audit` 已全部接入既有 TypeScript 分析内核；九个金额类结果按基础币种分组，`audit` 执行无金额的全范围一致性检查；`extremes` 显式改写账户 UUID 与 Decimal 字符串并移除开平仓哈希 |
| Phase 4 | Agent 会话创建与用户回合原子启动仓储 | 已完成 | 1 项真实 DuckDB 写侧测试通过；累计 258 项测试通过 | 2026-08-31 | 新会话在事务内校验 active 设备与当前账本版本；用户回合以 CAS 同时固化 `idle→running`、revision、用户规范消息和最后事件序号，提交后才允许发送供应商输入；事件只持久化序号，正文不复制到事件表；重复计划明确冲突并整体回滚 |
| Phase 4 | Agent 会话不可变身份 schema 与 DuckDB v11 | 已完成 | DuckDB 1.5.5 文件库 v1–v11 迁移、重复打开与未来版本拒绝测试通过；累计 258 项通过 | 2026-09-01 | 消息外键改指不可变 `agent_session_identities`，`agent_sessions` 保留可变回合状态；tool_calls 只保留对 messages 的复合外键；v11 以无约束 staging 表原位重建完成表交换，绕开 DuckDB 拒绝重命名被外键依赖父表的限制；新会话由仓储在同一事务登记身份行 |
| Phase 4 | Agent 成功与失败回合收口写仓储 | 已完成 | 1 项真实 DuckDB 写侧测试通过（创建、启动、成功收口、二次启动、失败收口全流程）；累计 258 项通过 | 2026-09-01 | `persistAgentTurnCompletion`/`persistAgentTurnFailure` 以单次 CAS 把 running/cancelling 收口为 idle、原子追加助手终态消息并递增事件序号；空部分回答不伪造助手消息；重复计划 CAS 失败整体回滚 |
| Phase 4 | Agent 工具调用写仓储与审计持久化 | 已完成 | 1 项真实 DuckDB 写侧测试通过；TypeScript strict 编译及完整回归累计 259 项通过 | 2026-09-01 | started/成功/失败/拒绝四路径各自原子落库；每次写入以 CAS 校验会话处于 running 且事件序号连续，重复计划或漂移序号整体回滚；拒绝记录与 `agent.tool_rejected` 审计事件同事务写入，请求参数不进入任何记录 |
| Phase 4 | Agent 回合编排器（orchestrator 内核） | 已完成 | 2 项真实 DuckDB 编排测试通过（成功、失败、取消三回合与越权/合法工具调用）；累计 261 项通过 | 2026-09-01 | 统一流信号执行器边界隔离供应商协议；事件序号全局单调，持久化序号允许单调跳过未落库的流式 delta，结算与工具写入 CAS 相应放宽为单调推进；工具调用绑定当前回合已持久化用户消息；取消先固化 cancelling 再中止执行器并经统一失败路径收口；MCP 短期凭据随回合签发、终态撤销；事件有界内存缓冲支持按游标续传，缺口强制重新取快照 |
| Phase 4 | Agent MCP 生产装配与服务入口启用 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过（凭据签发→工具发现→真实账本调用→撤销拒绝）；真实进程 smoke 健康返回 schema 11、`GET /mcp` 405、缺失与未知凭据均 401、SIGTERM 安全关闭；累计 262 项通过 | 2026-09-01 | `main` 经 `assembleAgentMcpRuntime` 装配共享金融工具注册表、进程内短期凭据存储与真实会话恢复；不构造任何持久化或长期凭据，凭据仅由编排器在回合运行期签发，服务重启即全部失效 |
| Phase 4 | Claude/Pi/OpenCode 回合执行器与编排器接入 | 已完成 | 5 项受控假进程契约测试通过：Claude 成功翻译与越权权限拒绝、取消按 cancelled 收口、协议异常按 failed 收口、Pi 持久进程结算后回收、OpenCode ACP 握手并校验 Lyra MCP Bearer 绑定；累计 267 项通过 | 2026-09-01 | `AgentTurnExecutor` 契约下沉到领域层；执行器复用最小权限启动计划、受控 JSON Lines 进程运行时与三个协议适配器，只做进程编排与协议翻译；Claude 权限请求一律回 deny、OpenCode 客户端能力请求回 cancelled/disabled，工具调用不经执行器（由 CLI 经 MCP HTTP 调用并在 orchestrator 持久化）；协议结算后回收仍存活的持久进程，进程在结算前退出按取消/失败收口；契约测试使用假 CLI，不启动真实模型会话 |
| Phase 4 | MCP 生产入口与 Agent 回合编排 | 进行中 | 官方 transport、短期凭据、完整 17 工具注册表、会话/回合/工具调用写仓储、回合编排内核、生产入口 `/mcp` 启用与三供应商回合执行器均已通过测试 | 2026-09-01 | 下一步把 orchestrator 与执行器装配进 `main` 并实现会话 REST 路由与 WebSocket 事件表面；不得绕过生命周期直接在启动时构造虚假 running 会话或长期凭据 |
| Phase 4 | Pi Lyra 工具扩展桥接 | 阻塞 | 项目缺少 `@earendil-works/pi-ai` 与 `@earendil-works/pi-coding-agent` | 2026-08-31 | Pi RPC 适配器与受限启动计划已完成；实际 `pi.registerTool` 扩展需官方 TypeBox/ExtensionAPI 运行时，不复制主机全局安装目录进项目 |
| Phase 4 | Claude/Pi/OpenCode 最小权限启动计划 | 已完成 | 2 项新增测试通过；累计 136 项通过；本机 CLI help 协议核对 | 2026-08-31 | Claude 使用 bare、双向 stream-json 和 strict MCP；Pi 使用 RPC、关闭内建工具与自动发现、仅加载 Lyra 扩展；OpenCode 使用 pure ACP 和隔离目录；prompt 不进入 argv，运行环境只允许显式凭据 |
| Phase 4 | Agent 会话回合生命周期门禁 | 已完成 | 2 项新增测试通过；累计 138 项通过 | 2026-08-31 | idle→running→cancelling/idle→closed 显式转换；相同状态重放幂等；只有 idle 可关闭，closed 不可恢复；每次真实转换递增 revision；新增 AGENT_SESSION_STATE_CONFLICT |
| Phase 4 | Agent 跨供应商 handoff 长度控制 | 已完成 | 2 项新增测试通过；累计 140 项通过 | 2026-08-31 | 默认字符预算；仅保留连续且完整的最近规范消息；省略旧消息必须有已固化摘要；不迁移隐藏推理、不截断金融内容；预算不足使用 AGENT_HANDOFF_CONTEXT_TOO_LARGE 明确拒绝 |
| Phase 4 | Agent handoff 显式切换边界消息 | 已完成 | 扩展既有切换策略测试；累计 215 项通过 | 2026-08-31 | 已持久化到新会话的同一条 system 切换标记同时进入目标 Agent 的有界 handoff，上下文明确说明来源与目标供应商；目标 Agent 不再只靠结构字段推断切换；原会话快照保持不变，切换消息仍受既有长度预算约束 |
| Phase 4 | 三 Agent 共用金融系统上下文 | 已完成 | 2 项新增测试通过；累计 142 项通过 | 2026-08-31 | 服务端固化账户范围、账本版本、截止时间和实际工具；金融数字必须披露口径与工具；明确历史成本/市值和已实现回撤/净值回撤边界；拒绝非白名单工具，不含正文或令牌 |
| Phase 4 | Agent 用户回合原子启动计划 | 已完成 | 2 项新增测试通过；累计 144 项通过 | 2026-08-31 | 仅 idle 会话可开始；先固化 running 快照、规范用户消息和递增事件，再发送供应商协议；拒绝空内容和重复消息 ID；事件不复制用户正文并保留授权账本基线 |
| Phase 4 | Agent 成功回合原子完成计划 | 已完成 | 2 项新增测试通过；累计 146 项通过 | 2026-08-31 | 供应商明确完成后原子追加完整助手消息、递增 message.completed 事件并恢复 idle；增量不直接作为规范消息；事件不复制回答正文；空回答和非 running 状态不得伪装成功 |
| Phase 4 | Agent 失败与取消回合原子收口 | 已完成 | 2 项新增测试通过；累计 148 项通过 | 2026-08-31 | running 失败与 cancelling 取消分别使用稳定错误码；可见部分回答明确标注终态，空部分不伪造助手消息；统一 message.failed 事件不携带正文或底层错误；会话恢复 idle |
| Phase 4 | Agent 工具调用安全生命周期 | 已完成 | 2 项新增测试通过；累计 150 项通过 | 2026-08-31 | started→completed/failed/cancelled 显式终态；工具调用绑定服务端账本版本与已持久化消息；仅保存扁平有界参数/结果摘要；事件不复制原始数据，失败不保存部分结果或底层异常 |
| Phase 4 | Agent 工具策略拒绝持久化与审计 | 已完成 | 2 项新增测试通过；累计 152 项通过 | 2026-08-31 | 未知、未配置和禁止能力不执行且直接形成 denied/rejected 终态；不保存请求参数；统一事件只含稳定错误码；同步生成 agent.tool_rejected 最小审计事件；非法工具名不得污染记录 |
| Phase 4 | Agent CLI 受控 JSON Lines 进程运行时 | 已完成 | 2 项新增测试通过；累计 154 项通过 | 2026-08-31 | 无 shell、无宿主环境继承；prompt 仅经 stdin JSON 帧传输；仅允许显式凭据变量；stdout 逐帧解析，stderr 只计数；输出上限、非法帧、超时、非零退出和取消均返回稳定脱敏终态 |
| Phase 4 | Pi RPC 严格协议适配器 | 已完成 | 2 项新增测试通过；累计 156 项通过；按本机 Pi 0.84.2 类型声明核对 | 2026-08-31 | prompt/abort JSON 帧带关联 ID；可见文本、reasoning、工具请求、usage 和 settled 显式分流；最终快照只提取 text block；扩展 UI、未知帧及供应商错误使用 AGENT_PROTOCOL_ERROR 明确拒绝且不向客户端暴露原始错误 |
| Phase 4 | Agent 流式消息聚合与终态桥接 | 已完成 | 2 项新增测试通过；累计 158 项通过 | 2026-08-31 | 文本与 usage 生成连续实时事件；reasoning 仅生成不含原文的状态事件且不存储；供应商权威快照覆盖临时增量；仅 settled 后调用成功/失败原子计划；取消保留可见部分；终态后迟到信号明确拒绝 |
| Phase 4 | Agent 隐藏推理客户端隔离 | 已完成 | 扩展既有流式聚合测试；累计 225 项通过 | 2026-08-31 | 三适配器仍可识别 reasoning 信号用于运行时状态，但统一 `reasoning.delta` 事件只携带 active 状态、供应商和账本版本，用于 App 思考动效；隐藏推理原文不进入客户端事件、断线缓冲、会话快照或持久化记录；可见 message delta 与权威终态流程保持不变 |
| Phase 4 | Claude 双向 stream-json 严格协议适配器 | 已完成 | 2 项新增测试通过；累计 180 项通过；按本机 Claude Code 2.1.233 源码 schema 核对 | 2026-08-31 | 用户消息、interrupt、权限 response 使用官方 SDK 帧；partial text/reasoning 与 result 权威快照分流；累计 token/成本只取 result；仅 `mcp__lyra__*` 自动预授权并二次收窄到 17 个金融工具；内建工具关闭；未知帧、嵌套 Agent 和 elicitation 明确拒绝；未启动模型会话 |
| Phase 4 | OpenCode ACP 1.0 严格协议适配器 | 已完成 | 2 项新增测试通过；累计 182 项通过；按本机 OpenCode 1.18.21 内嵌 ACP schema 核对 | 2026-08-31 | JSON-RPC 2.0 初始化明确关闭 fs、terminal 和 terminal auth；新会话只接入显式 Lyra HTTP MCP；支持 prompt/cancel、message/thought chunk、tool status、usage/cost 与 stopReason；ACP 无最终全文时以内存累计文本形成权威快照；权限请求统一取消，其他客户端能力请求返回禁用错误；未知、跨会话和迟到帧明确拒绝；未启动模型会话 |
| Phase 4 | Agent MCP 单会话短期凭据 | 已完成 | 2 项新增测试通过；累计 184 项通过 | 2026-08-31 | 256-bit 不透明令牌只在签发时返回，服务端仅保存哈希；绑定会话 ID、会话修订、供应商、账户范围、账本版本和截止时间；仅 running 会话可签发，认证后恢复服务端权威工具上下文；过期、撤销、会话切换或范围变化均失效；公开 API 统一返回脱敏 401 |
| Phase 4 | Agent MCP 认证工具执行边界 | 已完成 | 2 项新增测试通过；累计 186 项通过 | 2026-08-31 | 应用层先认证单会话凭据，再调用三 Agent 共用金融工具注册表；账户范围、账本版本和截止时间只取服务端权威上下文，模型同名参数会被移除；认证失败时 handler 零执行；成功后返回需要回存的 lastUsedAt 记录 |
| Phase 4 | 17 个金融工具单一 MCP 定义清单 | 已完成 | 2 项新增测试通过；累计 188 项通过 | 2026-08-31 | 工具名、中文用途、闭域对象 JSON Schema 和 read-only/non-destructive/idempotent/no-open-world 标记集中在现有 Tool Registry；账户范围、账本版本和 asOf 不进入模型可控 schema；MCP 只公开实际配置 handler 的定义，后续不得为三供应商复制 schema |
| Phase 4 | Agent 金融工具数据边界与来源口径 | 已完成 | 既有认证执行测试扩展；累计 188 项通过 | 2026-08-31 | MCP 工具结果统一包装为 lyra_financial_data；服务端附加工具名、账户范围、账本版本、截止时间和 data_only 信任边界；handler 原始结果只进入 data 字段，不得成为系统指令或覆盖来源口径 |
| Phase 4 | Claude/OpenCode 短期 MCP HTTP 连接配置 | 已完成 | 2 项新增测试通过；累计 190 项通过；Claude 结构按本机 2.1.233 schema 核对 | 2026-08-31 | Claude `mcpServers.lyra` 与 OpenCode ACP `mcpServers` 共用 URL 约束；远端只允许 HTTPS，本机允许回环 HTTP；短期凭据只进入 Authorization Bearer header，不进入 URL、argv 或持久化记录；拒绝 URL 内嵌凭据、片段和 header 注入 |
| Phase 4 | Claude 私有临时 MCP 配置文件 | 已完成 | 2 项新增测试通过；累计 192 项通过 | 2026-08-31 | 只允许在禁止 group/other 访问的既有会话目录中排他创建；随机文件名、0600 权限，正文仅含唯一 Lyra HTTP MCP 与短期 Bearer header；子进程终态通过幂等 dispose 删除；配置内容不得进入日志或数据库 |
| Phase 4 | 三 Agent 私有会话工作目录 | 已完成 | 2 项新增测试通过；累计 194 项通过 | 2026-08-31 | 在受信任绝对根目录下使用随机名创建 0700 空目录，不以会话 ID、用户文本或 prompt 拼接路径；作为三个供应商唯一 cwd，并可承载 Claude 临时配置与 Pi 会话文件；关闭后幂等递归删除，拒绝相对路径和非目录根路径 |
| Phase 4 | Agent 会话账户范围显式变更 | 已完成 | 2 项新增测试通过；累计 196 项通过 | 2026-08-31 | 仅 idle 回合边界可变更；追加可见 system 上下文消息，明确旧/新范围、新账本版本与截止时间；保留历史消息但清除旧范围摘要；revision 递增使旧 MCP 单会话凭据失效；拒绝回合中和无实际变化的请求 |
| Phase 4 | Agent 会话创建领域入口 | 已完成 | 2 项新增测试通过；累计 198 项通过 | 2026-08-31 | 创建 idle、revision=1、空消息的规范快照；首个账户范围、账本版本和截止时间显式固定；仅接受用户选择且探测可用的供应商；不可用时保留内部原因并明确失败，不自动切换到其他 Agent，也不在创建阶段启动模型 |
| Phase 4 | Agent WebSocket 断线事件恢复计划 | 已完成 | 2 项新增测试通过；累计 200 项通过 | 2026-08-31 | 仅当有界缓冲完整覆盖客户端游标到服务端已持久化序号且严格连续时续传增量；游标相等返回空增量；事件缺口、客户端超前或混入其他会话统一要求重新获取规范会话快照，不补造 delta 或猜测最终文本 |
| Phase 4 | Agent 同账户账本基线刷新 | 已完成 | 2 项新增测试通过；累计 202 项通过 | 2026-08-31 | 导入、Gmail 同步或重建产生更高账本版本后，仅允许在 idle 回合边界显式刷新；保留旧消息、清除旧数字摘要、追加新基线 system 消息并递增 revision，使旧 MCP 会话凭据失效；拒绝回合中刷新和版本回退 |
| Phase 0 | Agent 会话与供应商协议错误 API 映射 | 已完成 | 既有 API 错误测试扩展；累计 158 项通过 | 2026-08-31 | 会话状态冲突映射 409/AGENT_SESSION_STATE_CONFLICT；供应商协议异常映射可重试 502/AGENT_PROTOCOL_ERROR；内部协议原因、token 和路径不进入客户端错误正文 |
| Phase 4 | Agent 会话、消息与工具调用 DuckDB schema | 已完成 | DuckDB 1.5.5 migration smoke 通过；既有 92 项测试通过 | 2026-08-30 | v5 迁移幂等；保存规范会话快照、可见消息、授权后的工具摘要与事件序号；跨会话消息引用被拒绝，不保存隐藏推理、原始协议帧或供应商缓存 |
| Phase 1 | DuckDB 核心 schema 与查询视图 | 已完成 | DuckDB 1.5.5 内存库 smoke 通过 | 2026-08-30 | schema 可重复执行；验证账户、版本、导入、交易、FIFO 外键、3 个视图和重复交易约束 |
| Phase 1 | 单进程账本写入协调器与队列状态 | 已完成 | 3 项测试并扩展可观测状态断言；累计 205 项通过 | 2026-08-31 | 严格串行写任务；失败不污染后续队列；关闭时拒绝新任务并等待安全检查点；只读状态快照准确提供 accepting 与执行中/排队任务总数，供健康检查和指标复用 |
| Phase 1 | 导入预览快照与提交前置校验 | 已完成 | 2 项新增测试通过；累计 65 项通过 | 2026-08-30 | 统计已有/预计新增、日期范围；绑定文件哈希、解析器、账户和账本版本，陈旧预览必须重做 |
| Phase 2 | App 导入预览安全公开投影 | 已完成 | 1 项新增测试通过；累计 216 项通过 | 2026-08-31 | App 仅收到用户已选文件名、Lyra 账户 UUID、基础账本版本、总计/新增/已有行数、动作统计和日期范围；完整标准交易、内部账户字段、交易哈希、来源引用、文件哈希、解析器版本及预览指纹保留在服务端；提交继续使用独立 importId 与短时确认令牌 |
| Phase 2 | App 导入作业安全公开状态 | 已完成 | 1 项新增测试通过；累计 223 项通过 | 2026-08-31 | `GET /v1/imports/:importId` 可返回文件名/类型/大小、状态、目标账户、预览及提交账本版本、行计数、稳定错误码和时间；文件哈希、解析器版本、预览令牌摘要及底层 error_summary 不进入响应；非导入/账本稳定错误码明确拒绝，不把数据库自由文本传给 App |
| Phase 1 | 导入提交确定性变更计划 | 已完成 | 2 项新增测试通过；累计 67 项通过 | 2026-08-30 | 写事务前筛出新增交易并重放受影响账户 FIFO；重复提交返回无变更且不做无收益重放 |
| Phase 1 | DuckDB Node 启动、迁移与健康读取 | 已完成 | 2 项真实文件数据库集成测试通过；全新临时库服务 smoke 返回 schema 10 / ledger 0；累计 247 项测试通过 | 2026-08-31 | 安装并精确锁定官方 `@duckdb/node-api` 1.5.5-r.4；单事务执行并核验 v1–v10，重复打开幂等，未来 migration 明确拒绝；构建产物显式携带 SQL；迁移提交后立即 CHECKPOINT，降低 DuckDB 1.5.5 schema WAL 强制中断回放风险；服务关闭钩子等待写队列后关闭连接 |
| Phase 1 | 导入提交与 FIFO 原子持久化 | 已完成 | 2 项真实 DuckDB 集成测试通过；累计 249 项测试通过 | 2026-08-31 | 仓储在单事务内复核活跃账户映射、账本基线及预览指纹，并以 compare-and-swap 消费 `preview_ready` 作业；有变更时创建账本版本、写入标准交易、原子替换目标账户 lots/realized 并记录版本账户映射，无变更时不制造新版本；任一事实或 FIFO 写入失败会回滚作业、版本及全部派生产物。因 DuckDB 不允许更新已被子表引用的父行，作业终态在同事务内先于交易外键写入，提交前对外不可见，失败仍完整回滚 |
| Phase 1 | Fastify 首条纵向 API | 已完成 | 3 项新增 Fastify 注入测试通过；真实进程握手返回 200，接入实际 DuckDB 后健康端点返回数据库 schema 10 / ledger 0；累计 247 项测试通过 | 2026-08-31 | 安装并精确锁定 Fastify 5.12.1、Zod 4.5.4；单一服务入口默认只监听 `127.0.0.1:3141`，数据库路径必须显式配置；公开握手与健康端点附加 UUID request ID；其余 `/v1` 路由统一执行协议版本门禁并复用安全错误响应 |
| Phase 1 | 流式加密备份、认证与隔离解密 | 已完成 | 2 项新增测试通过；累计 98 项通过 | 2026-08-30 | AES-256-GCM、随机 IV、清单 AAD、明文 SHA-256；源变化或密文篡改明确失败；排他发布不覆盖文件；数据库 checkpoint 和隔离恢复审计待 Node 驱动接入 |
| Phase 1 | 每日加密备份调度与保留轮换计划 | 已完成 | 2 项新增测试通过；累计 231 项通过 | 2026-08-31 | 默认每 24 小时根据最近成功备份生成 start/NOT_DUE 决策，已有 checkpoint/加密任务时明确 BACKUP_IN_PROGRESS；保留数量由配置显式提供，按 verified manifest 创建时间与 backupId 稳定选择；轮换只返回精确保留/删除 ID，不接收或直接删除路径；实际 checkpoint、文件归属复核与删除仍由 DuckDB/仓储适配器执行 |
| Phase 2 | App 加密备份安全公开状态 | 已完成 | 1 项新增测试通过；累计 232 项通过 | 2026-08-31 | 账户中心仅公开备份 ID、创建时间、schema 版本、源数据大小，以及 scheduled/due/running 维护状态、下次计划时间和保留数量；只消费已验证的加密清单，按创建时间与 backupId 稳定排序；文件路径、格式、算法、IV 和明文 SHA-256 等内部完整性字段不进入 App 响应 |
| Phase 1 | 加密备份隔离恢复准入计划 | 已完成 | 2 项新增测试通过；累计 234 项通过 | 2026-08-31 | 已认证备份仅在最低可迁移版本到当前 schema 范围内进入恢复；并发、过旧和高于服务端版本分别明确拒绝；候选库替换前固定要求 schema、账本哈希和 FIFO 隔离校验及人工确认；备份策略不含 Gmail 凭据时明确要求重新授权；计划不接收路径且不直接替换运行数据库 |
| Phase 0 | 稳定 API 错误协议与敏感信息脱敏 | 已完成 | 3 项新增测试通过；累计 85 项通过 | 2026-08-30 | 统一 request_id、错误码、details、retryable 与 HTTP 状态；未知异常不返回堆栈、路径、令牌或底层错误文本 |
| Phase 0 | 最小化安全审计事件与 DuckDB schema | 已完成 | 2 项新增测试及 DuckDB 1.5.5 migration smoke 通过；累计 90 项通过 | 2026-08-30 | 稳定覆盖设备、账户、导入、Gmail、账本、Agent、备份事件；metadata 禁止令牌、邮件正文、prompt 和隐藏推理；v4 表约束主体与结果语义 |
| Phase 0 | 导入、Gmail、Agent 状态错误类型化 | 已完成 | 相关既有测试升级为错误码断言；累计 90 项通过 | 2026-08-30 | 导入预览失效携带 changed_fields；Gmail 重扫确认/游标/授权/连接状态与 Agent 回合边界/不可用均使用稳定错误码，不再依赖消息字符串分支 |
| Phase 0 | App/服务端协议版本门禁 | 已完成 | 2 项新增测试通过；累计 94 项通过 | 2026-08-30 | 协议版本 1；缺失、非法、过旧或过新统一返回 426/CLIENT_VERSION_INCOMPATIBLE；握手元数据仅公开版本范围和 schema 版本 |
| Phase 0 | 设备级敏感修改幂等键与 DuckDB v10 | 已完成 | 2 项新增测试及 DuckDB v1-v10 migration smoke 通过；累计 236 项通过 | 2026-08-31 | 同设备、同操作、同幂等键按规范 JSON 请求指纹开始或重放；对象字段顺序不影响指纹；明文 Idempotency-Key 仅在请求内存中散列，数据库只保存 key/request SHA-256、状态、资源引用和 2xx 状态，不缓存响应正文或一次性凭据；有效键用于不同请求返回 IDEMPOTENCY_KEY_REUSED，处理中返回可重试 IDEMPOTENCY_REQUEST_IN_PROGRESS；默认 24 小时后允许仓储原子替换过期记录 |
| Phase 0 | 服务健康与外部集成降级快照 | 已完成 | 2 项新增测试通过；累计 229 项通过 | 2026-08-31 | 健康快照区分进程存活、数据库/schema/账本版本、写队列 readiness、Gmail 需处理连接数和 Agent 可用数量；数据库不可用或写队列 draining 标记 unavailable，Gmail/Agent 故障只标记 degraded，不阻断账本只读；未探测 Agent 明确 unknown；不公开邮箱、CLI 版本、路径或失败详情 |

## 验证记录

| 日期 | 命令 | 结果 | 说明 |
| --- | --- | --- | --- |
| 2026-08-30 | `uv run pytest -q` | 未执行测试 | `uv` 无法从 PyPI 获取构建依赖；此结果不证明 Schwab 测试通过或失败 |
| 2026-08-30 | `npm run check` | 通过 | TypeScript strict 编译成功；Node test 共 9 项，9 项通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增交易标准化测试 7 项；累计 16 项全部通过，含 Python 生成的跨语言哈希 golden |
| 2026-08-30 | `npm run check` | 通过 | 新增整批标准化测试 3 项；累计 19 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 FIFO 测试 10 项；累计 29 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 positions、expiring、summary 测试 4 项；累计 33 项全部通过 |
| 2026-08-30 | `.venv/bin/pytest -q` | 通过 | Schwab Python 现有测试 71 项全部通过，用作 TypeScript 迁移基线 |
| 2026-08-30 | `npm run check` | 通过 | 新增 monthly、allocation、attribution 测试 3 项；累计 36 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 drawdown、risk 测试 2 项；累计 38 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增综合交易分析测试 2 项；累计 40 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 eConfirm 解析测试 7 项；累计 47 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增邮件成交到账本桥接测试 1 项；累计 48 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增账本完整性审计测试 2 项；TypeScript strict 编译通过，累计 50 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增多账户邮件归属和只读对账测试 3 项；TypeScript strict 编译通过，累计 53 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增交易、已实现和现金流基础查询测试 4 项；TypeScript strict 编译通过，累计 57 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Gmail `format=full` 适配测试 3 项；TypeScript strict 编译通过，累计 60 项测试全部通过 |
| 2026-08-30 | Python DuckDB 1.5.5 内存 smoke | 通过 | schema 重复执行保持单一迁移记录；账户、交易、lot、realized、现金流视图和重复哈希约束验证通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 DuckDB schema 后 TypeScript strict 编译及既有 60 项测试仍全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增单写协调器测试 3 项；累计 63 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增导入预览快照与失效校验测试 2 项；累计 65 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增导入提交计划测试 2 项；TypeScript strict 编译通过，累计 67 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Agent 工具策略与回合边界切换测试 4 项；累计 71 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Agent 统一事件序号测试 2 项；累计 73 项全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Gmail refresh token AES-256-GCM 测试 2 项；累计 75 项全部通过 |
| 2026-08-30 | Python DuckDB 1.5.5 Gmail migration smoke | 通过 | v2 迁移幂等；显式尾号映射唯一；元数据表无正文/明文令牌字段；支持 pending_initial_scan |
| 2026-08-30 | `npm run check` | 通过 | 新增 Gmail history 状态机测试 3 项；TypeScript strict 编译通过，累计 78 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增设备配对、一次性挑战、公钥指纹及不透明凭据测试 4 项；TypeScript strict 编译通过，累计 82 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增稳定错误响应、领域错误映射和未知异常脱敏测试 3 项；TypeScript strict 编译通过，累计 85 项测试全部通过 |
| 2026-08-30 | Python DuckDB 1.5.5 device auth migration smoke | 通过 | v3 迁移幂等；设备指纹唯一；刷新凭据代际轮换可持久化；无配对码、私钥、access token 或明文 refresh token 字段 |
| 2026-08-30 | `npm run check` | 通过 | 新增设备认证 schema 后 TypeScript strict 编译及既有 85 项测试仍全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增刷新凭据签发、轮换、重用检测及设备撤销测试 3 项；TypeScript strict 编译通过，累计 88 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增最小审计事件、敏感 metadata 拒绝和失败错误码约束测试 2 项；TypeScript strict 编译通过，累计 90 项测试全部通过 |
| 2026-08-30 | Python DuckDB 1.5.5 audit migration smoke | 通过 | v4 迁移幂等；设备主体外键有效；成功/失败错误码约束生效；schema 不含令牌、邮件正文、prompt 或隐藏推理字段 |
| 2026-08-30 | `npm run check` | 通过 | 导入预览、Gmail 同步和 Agent 切换测试改为稳定错误类型与错误码断言；TypeScript strict 编译通过，累计 90 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Agent CLI 隔离版本探测、缺失可执行文件和超时测试 2 项；TypeScript strict 编译通过，累计 92 项测试全部通过 |
| 2026-08-30 | Agent CLI probe smoke | 通过 | 使用安全探测实现识别 Claude 2.1.233、Pi 0.84.2、OpenCode 1.18.21；仅执行 `--version`，未发送 prompt 或触发模型请求 |
| 2026-08-30 | Python DuckDB 1.5.5 Agent migration smoke | 通过 | v5 迁移幂等；规范会话、消息、工具调用写入成功；跨会话工具消息引用被外键拒绝；无隐藏推理或原始 CLI 状态字段 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Agent v5 schema 后 TypeScript strict 编译及既有 92 项测试仍全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增客户端协议版本门禁与公开握手元数据测试 2 项；TypeScript strict 编译通过，累计 94 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增短期访问令牌签发、认证、过期和撤销测试 2 项；TypeScript strict 编译通过，累计 96 项测试全部通过 |
| 2026-08-30 | Python DuckDB 1.5.5 access-token migration smoke | 通过 | v6 迁移幂等；仅保存 token_hash；撤销状态约束生效；无 access_token、Bearer 明文或 token 明文字段 |
| 2026-08-30 | `npm run check` | 通过 | 新增流式加密备份往返、清单认证和篡改拒绝测试 2 项；TypeScript strict 编译通过，累计 98 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Google OAuth 高熵 state、S256 PKCE、设备绑定、过期/重放拒绝和 Gmail scope 校验测试 3 项；TypeScript strict 编译通过，累计 101 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 eConfirm 固定发件人/主题、UTC 起始时间、任务消息上限和非法范围拒绝测试 2 项；TypeScript strict 编译通过，累计 103 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增导入文件格式、签名、大小、文件名和伪装文件拒绝测试 3 项；TypeScript strict 编译通过，累计 106 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增上传流透传、硬大小限制、有界样本与 SHA-256 测试 2 项；TypeScript strict 编译通过，累计 108 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增导入私有暂存、0600 权限、摘要复核及超限清理测试 2 项；TypeScript strict 编译通过，累计 110 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Schwab 八列表头、BOM、固定列序、行映射及异常单元格拒绝测试 2 项；TypeScript strict 编译通过，累计 112 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增账户 UUID 范围、活跃/归档选择、缺省拒绝及币种分组测试 2 项；TypeScript strict 编译通过，累计 114 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增 Schwab 账户创建、默认币种、重命名、不可变身份和幂等归档测试 2 项；TypeScript strict 编译通过，累计 116 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增导入账户精确匹配、后缀歧义、显式选择校验和归档目标拒绝测试 2 项；TypeScript strict 编译通过，累计 118 项测试全部通过 |
| 2026-08-30 | `npm run check` | 通过 | 新增导入作业合法全流程、幂等重放、绕过预览、终态回退和提交中取消拒绝测试 2 项；TypeScript strict 编译通过，累计 120 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增导入预览不透明令牌签发、哈希存储、指纹/作业状态绑定、过期和错误令牌拒绝测试 2 项；TypeScript strict 编译通过，累计 122 项测试全部通过 |
| 2026-08-31 | Python DuckDB 1.5.5 import preview migration smoke | 通过 | v1-v7 顺序迁移完成；preview_token_hash、preview_fingerprint、preview_expires_at 字段存在；v7 重复执行幂等 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Google 已验证身份、scope、refresh token AAD 加密、禁止 access token 持久化及断开清理测试 2 项；TypeScript strict 编译通过，累计 124 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 消息摘要、换行规范化、UTC 时间、internalDate、无正文持久化及非法时间拒绝测试 2 项；TypeScript strict 编译通过，累计 126 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 消息到账本标准化、FIFO、关联计划、重复消息幂等和同 ID 内容/账户冲突测试 2 项；TypeScript strict 编译通过，累计 128 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 首次显式映射、已有映射复用、改绑拒绝、归档账户拒绝及候选 UUID 脱敏测试 2 项；TypeScript strict 编译通过，累计 130 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 批次持久终态计数、重复候选去重、history 推进，以及失败、缺失、批次外结果拒绝测试 2 项；TypeScript strict 编译通过，累计 132 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 失败消息规范摘要、解析拒绝、账户/内容冲突状态，以及正文和失败详情不落盘测试 2 项；TypeScript strict 编译通过，累计 134 项测试全部通过 |
| 2026-08-31 | 本机 `claude/pi/opencode --help` 协议检查 | 通过 | 确认 Claude stream-json/strict MCP、Pi RPC/显式扩展/关闭内建工具、OpenCode ACP/pure/cwd 参数；未启动模型会话 |
| 2026-08-31 | `npm run check` | 通过 | 新增三 Agent 安全 argv、机器协议、隔离路径、无 prompt 参数和无关集成参数拒绝测试 2 项；TypeScript strict 编译通过，累计 136 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 开始、取消、完成、关闭、幂等重放、非法关闭和终态恢复拒绝测试 2 项；TypeScript strict 编译通过，累计 138 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent handoff 最近完整消息、既有摘要覆盖、账户/账本基线保留、无摘要与单条超限拒绝测试 2 项；TypeScript strict 编译通过，累计 140 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 金融系统上下文口径、工具排序、披露要求、能力禁令，以及非法版本/时间/工具拒绝测试 2 项；TypeScript strict 编译通过，累计 142 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 回合 running 快照、用户消息、连续起始事件、适配器输入，以及非 idle、空消息、重复 ID 拒绝测试 2 项；TypeScript strict 编译通过，累计 144 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 完整助手消息、idle 收口、连续完成事件、无正文事件，以及非法状态、空回答、重复 ID 拒绝测试 2 项；TypeScript strict 编译通过，累计 146 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 失败部分回答、取消空回答、稳定终态错误码、无正文失败事件、状态匹配和重复 ID 拒绝测试 2 项；TypeScript strict 编译通过，累计 148 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 工具 started/终态记录、服务端账本绑定、连续无原始数据事件、安全摘要、越权工具、敏感字段及非法终态拒绝测试 2 项；TypeScript strict 编译通过，累计 150 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 工具策略拒绝的 denied/rejected 记录、无参数事件、最小审计事件，以及非 running、缺失消息和非法工具名拒绝测试 2 项；TypeScript strict 编译通过，累计 152 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent CLI stdin/stdout JSON 帧、宿主环境隔离、显式凭据、非法输出、主动取消和禁止行为环境变量测试 2 项；TypeScript strict 编译通过，累计 154 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Pi prompt/abort 命令、文本/reasoning/工具/usage/settled 分流、纯 text 最终快照及原始供应商错误脱敏测试 2 项；TypeScript strict 编译通过，累计 156 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 文本/reasoning 连续事件、权威快照、settled 门禁、成功/取消终态桥接、部分回答保留及迟到信号拒绝测试 2 项；TypeScript strict 编译通过，累计 158 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 扩展既有 API 错误测试，覆盖 Agent 会话状态冲突、供应商协议错误可重试语义与内部原因脱敏；TypeScript strict 编译通过，累计 158 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Google PKCE 授权码 REST 交换、离线令牌、最小 scope、userinfo 已验证邮箱、到期时间、上游失败与敏感正文脱敏测试 2 项；TypeScript strict 编译通过，累计 160 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail messages.list 分页剩余额度、任务硬上限、format=full 拉取、截断标记、授权失效、临时上游故障和敏感信息脱敏测试 2 项；TypeScript strict 编译通过，累计 162 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Google refresh token 表单、短期 access token 到期时间、已验证 scope 复用、refresh token 不回传，以及失效授权脱敏映射测试 2 项；TypeScript strict 编译通过，累计 164 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail history.list messageAdded、message ID 去重、observed historyId、增量硬上限截断、format=full 获取和 404 游标过期测试 2 项；TypeScript strict 编译通过，累计 166 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 完整增量候选游标、截断旧游标保留、needs_resync 转换、稳定错误码和用户确认重扫门禁测试 2 项；TypeScript strict 编译通过，累计 168 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail profile-first 首扫顺序、邮箱身份匹配、history 基线、完整扫描完成游标和截断无游标测试 2 项；TypeScript strict 编译通过，累计 170 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 扩展 Gmail history REST 测试，验证新增邮件先请求 From/Subject metadata、Schwab eConfirm 白名单匹配、无关邮件忽略计数且不下载 full 正文；TypeScript strict 编译通过，累计 170 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 文本 attachmentId 补取、Base64URL 注入、既有 full 解析器复用、非文本不处理及非法/缺失 attachment data 拒绝测试 2 项；TypeScript strict 编译通过，累计 172 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增加密 Gmail 连接 AAD 解密、refresh 换 access、断开状态零网络调用、错误主密钥与敏感信息脱敏测试 2 项；TypeScript strict 编译通过，累计 174 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Google revoke POST、成功后本地断开、失败保留密文、可重试语义和原始错误正文脱敏测试 2 项；TypeScript strict 编译通过，累计 176 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Android Google 系统授权 URL、S256 PKCE、最小 scope、offline consent、redirect URI 校验和 PKCE 私密字段不暴露测试 2 项；TypeScript strict 编译通过，累计 178 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Claude stream-json 用户/interrupt/权限帧、text/reasoning 增量、result 权威快照、usage、Lyra MCP 工具收窄、越界控制与未知协议拒绝测试 2 项；启动计划补充唯一 `mcp__lyra__*` 预授权；TypeScript strict 编译通过，累计 180 项测试全部通过；未启动模型会话 |
| 2026-08-31 | `npm run check` | 通过 | 新增 OpenCode ACP v1 无 fs/terminal 能力握手、唯一 Lyra HTTP MCP、prompt/cancel、message/thought/tool/usage 更新、最终快照、权限取消、未知协议和跨会话拒绝测试 2 项；TypeScript strict 编译通过，累计 182 项测试全部通过；未启动模型会话 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent MCP 单会话凭据哈希保存、服务端工具上下文恢复、错误令牌、会话修订/供应商不匹配、过期与幂等撤销测试 2 项；扩展稳定 API 错误映射；TypeScript strict 编译通过，累计 184 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 MCP 认证工具执行边界测试 2 项，验证恶意范围参数剥离、服务端上下文注入、凭据使用时间更新，以及会话 revision 不匹配时 handler 零执行；TypeScript strict 编译通过，累计 186 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 17 个金融工具单一定义、唯一顺序、闭域输入 schema、只读能力标记、授权字段不公开及仅暴露已配置 handler 测试 2 项；TypeScript strict 编译通过，累计 188 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 扩展 MCP 认证执行测试，验证金融工具结果使用 data_only 信任边界，并携带服务端权威工具名、账户范围、账本版本和截止时间；TypeScript strict 编译通过，累计 188 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Claude/OpenCode MCP HTTP 配置测试 2 项，验证 Authorization-only 凭据传递、HTTPS/回环 HTTP、非回环明文地址、URL 内嵌凭据和非法令牌拒绝；TypeScript strict 编译通过，累计 190 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Claude 临时 MCP 配置文件测试 2 项，验证私有会话目录、随机路径、0600 权限、官方配置结构、幂等删除及公开目录拒绝；TypeScript strict 编译通过，累计 192 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 会话工作目录测试 2 项，验证随机 0700 空目录、临时内容终态清理、幂等 dispose、相对路径和非目录根路径拒绝；TypeScript strict 编译通过，累计 194 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 账户范围变更测试 2 项，验证回合边界、显式上下文消息、新账本基线、旧摘要失效、revision 递增、原快照不变，以及运行中/同范围拒绝；TypeScript strict 编译通过，累计 196 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 会话创建测试 2 项，验证初始 idle/revision/空历史、显式账户和账本基线、输入规范化，以及目标供应商不可用时不自动回退和内部原因脱敏；TypeScript strict 编译通过，累计 198 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 事件恢复测试 2 项，验证连续后缀续传、最新游标空增量，以及事件缺口、客户端超前和跨会话缓冲时强制快照；TypeScript strict 编译通过，累计 200 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent 同账户账本基线刷新测试 2 项，验证更高账本版本、截止时间、显式上下文消息、旧摘要失效、revision 递增、原快照不变，以及回合中刷新和版本回退拒绝；TypeScript strict 编译通过，累计 202 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Agent CLI 精确版本准入测试 1 项并更新既有探测测试，验证白名单版本可用、未验证版本明确 unavailable，且缺失进程与超时路径保持不变；TypeScript strict 编译通过，累计 203 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Schwab 官方文件定期对账提醒测试 2 项，验证默认自然月、月末钳制、到期当天、按账户关闭/调整周期和从未导入立即到期；TypeScript strict 编译通过，累计 205 项测试全部通过 |
| 2026-08-31 | Python DuckDB 1.5.5 reminder migration smoke | 通过 | v1–v8 顺序迁移并重复执行幂等；缺省启用/每月与按账户关闭/三个月配置经视图读取正确；非法周期被约束拒绝；最近日期只取 committed 官方导入 |
| 2026-08-31 | `npm run check` | 通过 | 标准交易显式贯穿 sourceType/sourceReference，Gmail 绑定 message ID，流水查询增加来源过滤；扩展既有标准化、查询和导入测试，跨语言哈希 golden 保持不变；TypeScript strict 编译通过，累计 205 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 交易与现金流水查询及 Agent trades/cashflow 工具 schema 统一支持 csv/excel/gmail 闭域来源过滤；同一业务交易从 CSV 与 Gmail 进入时显式断言交易哈希一致；TypeScript strict 编译通过，累计 205 项测试全部通过 |
| 2026-08-31 | Python DuckDB 1.5.5 transaction source view smoke | 通过 | v1–v9 顺序迁移并重复执行幂等；v_cashflows 可按 gmail 读取 source_type、message ID 来源引用与精确金额；既有交易事实未复制或改写 |
| 2026-08-31 | `npm run check` | 通过 | Gmail REST、状态机、批次推进和成功/失败消息元数据共用正十进制 history ID 规范化，并统一同步完成 UTC 时间；扩展既有测试确认非法伪游标拒绝、原快照不变和成功时间标准化；TypeScript strict 编译通过，累计 205 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | WriteCoordinator 新增只读 accepting/pendingOperations 状态；既有串行、失败隔离和关闭测试扩展验证执行中、排队完成及关闭后的计数变化；TypeScript strict 编译通过，累计 205 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 定时同步决策测试 2 项，验证默认每小时、到期增量请求、未到期下一时间、任务防重入、需确认重扫不自动运行，以及超大 history ID 保持字符串；TypeScript strict 编译通过，累计 207 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增交易流水稳定游标分页测试 1 项并扩展 Agent 工具 schema 测试，验证所有页拼接与一次性查询顺序一致且无重复、游标绑定账户范围与筛选条件、非法游标不回退首页，以及统一 1–200 页长限制；TypeScript strict 编译通过，累计 208 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 与 Agent 共用账户公开投影测试 1 项，验证活跃账户优先和稳定顺序、输入快照不变，以及 `legacyAccountKey` 和旧账户键内容不进入序列化结果；TypeScript strict 编译通过，累计 209 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 只读账本响应语境测试 2 项，验证权威账户范围、异币种分组与禁止伪合计、账本版本、UTC 截止/获取时间，以及非法版本、无时区时间和无效时钟拒绝；TypeScript strict 编译通过，累计 211 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增刷新凭据重用后的同族全代次撤销测试 2 项，验证旧代与当前活动代一并撤销、重复执行幂等、原记录不变，以及活动凭据伪触发、跨设备和跨凭据族计划拒绝；TypeScript strict 编译通过，累计 213 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增三 Agent 公开可用状态快照测试 2 项，验证 Claude/Pi/OpenCode 固定顺序、版本或稳定失败原因公开、路径与 stderr 不外泄，以及缺失、重复和无效完成时间拒绝；TypeScript strict 编译通过，累计 215 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 修正跨供应商 handoff，使目标 Agent 收到与新会话快照一致、明确包含来源和目标的 system 切换消息；扩展既有切换策略测试并确认原会话不变；TypeScript strict 编译通过，累计 215 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 导入预览安全公开投影测试 1 项，验证统计与账户 UUID 完整，同时内部交易、账户字段、交易哈希、来源引用及预览指纹不进入响应；TypeScript strict 编译通过，累计 216 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App Gmail 连接安全公开投影测试 1 项，验证邮箱、连接状态和同步时间完整，同时 Google subject、scope、history 游标与 refresh token 密文不进入响应；TypeScript strict 编译通过，累计 217 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | Gmail 同步 `lastErrorCode` 收窄为稳定闭集；扩展既有状态机与连接公开投影测试，验证 history 失效错码和持久化自由文本错误在公开前被拒绝；TypeScript strict 编译通过，累计 217 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 成功暂存导入结果新增幂等 `dispose()`；扩展既有测试验证原始内容、0600 权限及连续两次清理后暂存目录为空；TypeScript strict 编译通过，累计 217 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增已配对设备持久化创建测试 1 项，验证规范设备名、SPKI 公钥、指纹、算法、active 状态、UTC 时间、不含私钥，以及空名称和无效时钟拒绝；TypeScript strict 编译通过，累计 218 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 已配对设备安全公开列表测试 1 项，验证活跃优先与稳定排序、状态时间完整、仓储输入不变，以及公钥 PEM、指纹和算法不进入响应；TypeScript strict 编译通过，累计 219 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App Agent 会话安全公开快照测试 1 项，验证供应商、状态、权威账户与账本基线、规范可见消息及独立对象复制，同时内部 handoff summary 不进入响应；TypeScript strict 编译通过，累计 220 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App Agent 工具调用安全公开列表测试 1 项，验证稳定排序、安全参数/结果摘要、终态、账本版本与耗时完整，输入顺序及摘要对象不被修改，同时内部授权判定与冗余会话 ID 不进入响应；TypeScript strict 编译通过，累计 221 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App Gmail 多账户映射安全公开列表测试 1 项，验证脱敏尾号、账户 UUID、稳定排序和连接归属门禁，输入顺序不变且连接 ID 不进入逐条响应；TypeScript strict 编译通过，累计 222 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 导入作业安全公开状态测试 1 项，验证恢复流程所需的状态、计数和账本版本，以及导入错误码闭集；文件摘要、解析器版本、预览令牌摘要和底层错误详情不进入响应；TypeScript strict 编译通过，累计 223 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Gmail 映射用户显式改绑测试 2 项，验证目标账户、尾号与连接归属、CAS 旧账户前置条件、同目标幂等、原映射不变，以及跨连接、归档与不匹配目标拒绝；TypeScript strict 编译通过，累计 225 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | Agent reasoning 统一事件改为只公开 active 状态；扩展既有流式聚合测试验证事件不含隐藏分析文本，最终可见消息、序号和三供应商适配器测试保持通过；TypeScript strict 编译通过，累计 225 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增已实现与现金流水稳定 cursor 分页测试 1 项并扩展 Agent schema；验证跨页完整顺序、完整范围合计不随页变化和跨工具游标拒绝；TypeScript strict 编译通过，累计 226 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增成功认证后的低频设备活动更新测试 1 项，验证首次更新、默认五分钟节流、跨间隔更新、原记录不变和撤销设备拒绝；TypeScript strict 编译通过，累计 227 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增服务健康与外部集成降级快照测试 2 项，验证 healthy/degraded/unavailable、数据库与写队列 readiness、Gmail/Agent 仅降级，以及邮箱和 Agent 版本详情不公开；TypeScript strict 编译通过，累计 229 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增每日加密备份调度与保留轮换测试 2 项，验证默认 24 小时到期、防重入、同时间 backupId 稳定排序、输入不变和仅返回待删除清单 ID；TypeScript strict 编译通过，累计 231 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 加密备份安全公开状态测试 1 项，验证维护状态映射、下次计划时间、稳定排序、输入不变，以及路径、IV、算法和明文 SHA-256 不公开；TypeScript strict 编译通过，累计 232 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增加密备份隔离恢复准入测试 2 项，验证 schema 迁移兼容边界、并发恢复拒绝、三项强制一致性检查、人工确认及 Gmail 凭据缺失时重新授权；TypeScript strict 编译通过，累计 234 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 新增设备级敏感修改幂等测试 2 项，验证规范 JSON 指纹、只持久化键哈希、完成结果引用重放、不同请求复用键冲突和过期记录替换；TypeScript strict 编译通过，累计 236 项测试全部通过 |
| 2026-08-31 | Python DuckDB 1.5.5 内存 smoke | 通过 | v1-v10 migration 连续重复执行保持 10 条唯一版本记录；mutation_idempotency_records 不含明文键列，同设备/操作/key_hash 重复写入由唯一约束拒绝 |
| 2026-08-31 | `npm run check` | 通过 | 新增 App 账本明细安全公开投影测试 2 项，验证 Lyra 账户 UUID 映射、Decimal 显式字符串化、内部哈希与来源引用隔离，以及范围外账户拒绝；TypeScript strict 编译通过，累计 238 项测试全部通过 |
| 2026-08-31 | `flutter analyze` | 通过 | Flutter Android 工程静态分析无问题 |
| 2026-08-31 | `flutter test` | 通过 | 1 项聚焦组件测试通过：手机宽度五主导航可切换，总览未连接态不伪造金额 |
| 2026-08-31 | `GRADLE_USER_HOME=/private/tmp/lyra-gradle-home-20260831 flutter build apk --debug` | 通过 | 生成 `apps/mobile/build/app/outputs/flutter-apk/app-debug.apk`；临时 Gradle 用户目录仅用于避开全局阿里云镜像对 Flutter 官方 Maven 仓库的覆盖，未修改全局配置 |
| 2026-08-31 | Android CLI 设备 smoke | 通过 | Medium_Phone 模拟器安装并正常启动 `com.lyrafinance.lyra_mobile`；截图目视无溢出；布局树确认五个导航入口均可点击并具备 Tab 1–5 of 5 语义；验证后已停止模拟器 |
| 2026-08-31 | `npm run check` | 通过 | 新增 Fastify 首条纵向 API 测试 3 项，覆盖公开协议握手、健康状态码、UUID request ID、受保护路由协议门禁和未知异常脱敏；TypeScript strict 编译通过，累计 241 项测试全部通过 |
| 2026-08-31 | `LYRA_PORT=3142 npm start` + loopback HTTP smoke | 通过 | 编译后单服务入口成功监听 `127.0.0.1:3142`；`GET /v1/system/info` 返回 200 与协议 1/schema 10；`GET /v1/health` 因 DuckDB Node 尚未接入如实返回 503；两者均返回 `x-request-id`；验证后已停止进程 |
| 2026-08-31 | `npm run build` + `node --test dist/test/schwab-csv.test.js` | 通过 | csv-parse 适配器聚焦测试 2 项通过，覆盖有效 BOM/CRLF/转义 CSV 到统一预览，以及列数、引号和 UTF-8 拒绝路径 |
| 2026-08-31 | `npm run check` | 通过 | Schwab CSV 严格解析接入后 TypeScript strict 编译成功；累计 243 项测试全部通过 |
| 2026-08-31 | `npm run build` + `node --test dist/test/schwab-xlsx.test.js` | 通过 | ExcelJS 适配器聚焦测试 2 项通过，覆盖日期/数值单元格到统一预览，以及隐藏行、公式和多候选工作表拒绝路径 |
| 2026-08-31 | `npm run check` | 通过 | Schwab XLSX 适配器接入后 TypeScript strict 编译成功；累计 245 项测试全部通过 |
| 2026-08-31 | `npm audit --json` + ExcelJS 调用路径检查 | 已评估 | 当前 2 个 moderate 条目均源于 ExcelJS 的 uuid@8.3.2；公告影响 v3/v5/v6 且要求调用方传入 buffer，ExcelJS 4.4.0 仅在条件格式写出代码调用无 buffer 的 v4，Lyra 生产适配器仅使用 `xlsx.load`；不执行会降级到 ExcelJS 3.4.0 的 `--force` 修复，持续跟踪上游更新 |
| 2026-08-31 | `npm run build` + `node --test dist/test/duckdb-database.test.js` | 通过 | 官方 DuckDB Node API 真实文件测试 2 项通过：v1–v10 原子迁移与重复打开、迁移后无 WAL、未来 v11 数据库拒绝 |
| 2026-08-31 | `npm run check` | 通过 | DuckDB Node 启动迁移与健康读取接入后 TypeScript strict 编译成功；累计 247 项测试全部通过 |
| 2026-08-31 | `LYRA_DATABASE_PATH=<临时文件> npm start` + loopback health smoke | 通过 | 全新数据库完成迁移并监听本机端口；`GET /v1/health` 返回 200、database available、schemaVersion 10、ledgerVersion 0；迁移后立即 CHECKPOINT；测试进程强制中断后数据库仍可重新打开并通过健康读取 |
| 2026-08-31 | `npm run build` + `node --test dist/test/duckdb-import-commit.test.js` | 通过 | 真实 DuckDB 文件测试 2 项通过：首次导入原子写入交易、账本版本及 FIFO；无新增导入复用当前版本；任一交易约束失败后作业恢复 `preview_ready`，且版本、交易和 FIFO 均无残留 |
| 2026-08-31 | `npm run check` | 通过 | 导入提交与 FIFO DuckDB 仓储接入后 TypeScript strict 编译成功；111 个测试套件、累计 249 项测试全部通过 |
| 2026-08-31 | `npm run build` + `node --test dist/test/rfc-mime.test.js` | 通过 | mailparser RFC MIME 适配器聚焦测试 2 项通过：multipart/base64 `.eml` 可进入既有 eConfirm 解析；无 Message-ID 使用原文 SHA-256；错误发件人和附件式正文明确拒绝 |
| 2026-08-31 | `npm run check` | 通过 | RFC MIME 适配器接入后 TypeScript strict 编译成功；112 个测试套件、累计 251 项测试全部通过；依赖安装审计仍报告既有 2 个 moderate 条目，数量未增加 |
| 2026-08-31 | `npm run build` + `node --test dist/test/agent-mcp-http.test.js` | 通过 | 官方 MCP SDK transport 聚焦测试 2 项通过：无状态 initialize、工具发现及调用成功；工具结果绑定服务端账户/账本/as-of；缺失 Bearer、越权参数、未启用工具和非 POST 方法均在 handler 前拒绝 |
| 2026-08-31 | `npm run check` | 通过 | 官方 MCP Streamable HTTP transport 接入后 TypeScript strict 编译成功；113 个测试套件、累计 253 项测试全部通过；依赖安装审计仍为既有 2 个 moderate 条目 |
| 2026-08-31 | `npm run build` + `node --test dist/test/agent-runtime-store.test.js` | 通过 | Agent 运行时存储聚焦测试 2 项通过：真实 DuckDB 文件可恢复完整规范会话和稳定消息顺序；进程内凭据存储只按哈希读取，认证使用后更新时间，并可按会话撤销全部短期凭据 |
| 2026-08-31 | `npm run check` | 通过 | Agent 会话读取与短期 MCP 凭据运行时存储接入后 TypeScript strict 编译成功；114 个测试套件、累计 255 项测试全部通过 |
| 2026-08-31 | `npm run build` + `node --test dist/test/financial-tool-runtime.test.js` | 通过 | MCP 核心金融工具聚焦测试 2 项通过：正式导入产生的真实交易与 FIFO 可查询账户、分币种总览、持仓和到期持仓；公开结果不含旧账户键；账本版本落后时明确拒绝读取 |
| 2026-08-31 | `npm run check` | 通过 | 首批 4 个真实金融查询 handlers 接入后 TypeScript strict 编译成功；115 个测试套件、累计 257 项测试全部通过 |
| 2026-08-31 | `npm run build` + `node --test dist/test/financial-tool-runtime.test.js dist/test/agent-tool-definitions.test.js` | 通过 | 扩展既有聚焦测试验证真实 `trades`、`realized`、`cashflow` 筛选与合计、内部哈希/来源隔离，以及交易动作 schema 与账本动作闭集一致；4 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 三类 MCP 账本明细 handlers 接入后 TypeScript strict 编译成功；115 个测试套件、累计 257 项测试全部通过 |
| 2026-08-31 | `npm run build` + `node --test dist/test/financial-tool-runtime.test.js` | 通过 | 扩展既有真实账本聚焦测试，验证完整 17 工具均已配置并可从正式导入交易/FIFO 运行；金额类分析按币种分组，最佳/最差交易不公开哈希，旧 Agent 版本仍被拒绝；2 项测试全部通过 |
| 2026-08-31 | `npm run check` | 通过 | 完整 17 个真实金融工具注册表接入后 TypeScript strict 编译成功；115 个测试套件、累计 257 项测试全部通过 |
| 2026-08-31 | `npm run build` + `node --test dist/test/agent-session-write.test.js dist/test/agent-runtime-store.test.js` | 通过 | Agent 写仓储聚焦测试 3 项通过：新会话与事件游标可从真实 DuckDB 恢复；用户回合原子写入 running 状态、revision、完成态用户消息和序号；重复计划 CAS 失败且不产生重复消息 |
| 2026-08-31 | `npm run check` | 通过 | Agent 会话创建与用户回合原子启动仓储接入后 TypeScript strict 编译成功；116 个测试套件、累计 258 项测试全部通过 |
| 2026-09-01 | `npm run check` | 通过 | 修复上一会话遗留的 v11 迁移：`agent_messages`/`agent_tool_calls` 经无约束 staging 表原位重建，消息外键改指不可变 `agent_session_identities`，会话写路径同步登记身份行；真实 DuckDB 会话写侧测试覆盖成功/失败回合收口；修复前 8 项真实数据库测试失败，修复后 116 个测试套件、累计 258 项测试全部通过 |
| 2026-09-01 | `npm run check` | 通过 | 新增 Agent 工具调用写仓储测试 1 项，验证 started/成功/失败/拒绝原子持久化、事件序号 CAS 连续推进、重复计划冲突回滚，以及拒绝与 `agent.tool_rejected` 审计事件同事务落库；117 个测试套件、累计 259 项测试全部通过 |
| 2026-09-01 | `npm run check` | 通过 | 新增 Agent 回合编排器测试 2 项，验证假执行器驱动成功/失败/取消回合、越权与合法工具调用经编排器持久化并含审计、流事件与工具事件序号全局连续、持久化序号单调跳过临时 delta 且取消空部分不伪造消息；118 个测试套件、累计 261 项测试全部通过 |
| 2026-09-01 | `npm run check` | 通过 | 新增 Agent MCP 生产装配集成测试 1 项：进程内凭据 + 真实 DuckDB 会话恢复授权，完成工具发现、真实账本 `accounts` 调用与撤销后拒绝；119 个测试套件、累计 262 项测试全部通过 |
| 2026-09-01 | `npm run check` | 通过 | 新增三供应商回合执行器受控假进程契约测试 5 项：Claude stream-json 翻译与权限拒绝、取消 cancelled 收口、协议异常 failed 收口、Pi RPC 持久进程回收、OpenCode ACP 握手与 Bearer 绑定；`AgentTurnExecutor` 下沉领域层，编排器执行器异常在已取消时按 cancelled 收口；120 个测试套件、累计 267 项测试全部通过；未启动真实模型会话 |
| 2026-09-01 | `LYRA_PORT=3143 LYRA_DATABASE_PATH=<临时文件> npm start` + loopback smoke | 通过 | 单服务入口完成 v1–v11 迁移并监听本机端口；健康端点返回 200 与 schema 11；`GET /mcp` 明确 405；无凭据与未知凭据的 `tools/list` 均返回 401 稳定 JSON-RPC 错误；SIGTERM 后写队列关闭、进程干净退出 |

## 当前环境事项

| 事项 | 状态 | 后续动作 |
| --- | --- | --- |
| Node.js 22 | 可用 | 用于 TypeScript 服务端 |
| Flutter SDK | 可用 | Flutter 3.47.2 stable、Dart 3.13.2；Android 授权已接受 |
| Android 工具链 | 可用 | Android CLI 1.0.15498356、Android SDK 36.1.0、JDK 21；Medium_Phone 设备 smoke 已通过 |
| 外部网络代理 | 可用 | `127.0.0.1:7890` 监听正常；默认及显式代理 GitHub 探测均为 HTTP 200；受限沙箱内的 loopback 失败不代表代理离线 |
| Gradle 全局镜像 | 需隔离 | `/Users/Gin/.gradle/init.d/aliyun-mirror.gradle.kts` 会清空 Flutter 官方 Maven 仓库；Flutter 构建使用独立 `GRADLE_USER_HOME`，不修改用户全局配置 |
| 服务端外部依赖 | TypeScript、Node 类型、Decimal、Fastify、Zod、DuckDB Node API、csv-parse、ExcelJS、mailparser 与官方 MCP SDK 已安装 | Pi 扩展 SDK 尚未安装；代理已恢复，可按后续功能点逐项引入并验证 |
| ExcelJS 上游审计项 | 已评估、待上游修复 | uuid advisory 不命中 Lyra 的 XLSX 只读路径；保留 ExcelJS 4.4.0 精确锁定并跟踪升级，不用 `npm audit fix --force` 降级主依赖 |
