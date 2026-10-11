# Lyra 实现进度

> 本表只记录已经落盘的实现和实际验证结果。未通过验证的功能不得标记为完成。  
> 当前架构见 [Genkit 与多来源登录](genkit-oauth.md)，容器部署见 [VPS 部署指南](deployment.md)。早期总体方案见 [Lyra 移动资产账本与 AI Agent 应用完整方案](plans/lyra-product-technical-design.md)。

## 当前迁移：Genkit、独立登录页、深色界面与界面切换行情源

本节记录当前分支状态。下方历史进度保留当时的实现与验证；其中 MCP、多 CLI、助手供应商切换和匿名配对设计已被本次迁移替代。

| 功能 | 状态 | 实际验证与边界 |
| --- | --- | --- |
| 单一 Genkit 助手 | 已完成 | 一个 `genkit` provider，界面名称 Lyra；官方兼容插件、真实 Genkit 工具循环、DuckDB 和本机 HTTP/SSE 模型测试通过；保留 17 个既有账本工具，新增独立外部只读 `market-positions`，共 18 个 |
| 删除 MCP 与多 CLI 接入 | 已完成 | 移除项目 MCP HTTP 服务、凭据/配置/进程适配、Claude/Pi/OpenCode 运行时及切换入口；实际进程测试确认 `/mcp` 返回 404；构建先清理 `dist`，旧产物不会残留 |
| Google、LINUX DO、GitHub 登录与设备凭据 | 已完成 | 统一提供方目录、授权事务、App PKCE、共享来源限流、一次性兑换与设备凭据；保留原 Google OIDC 接口。GitHub / LINUX DO 使用固定端点验证稳定数字 ID，不要求邮箱；每方明确配置 owner 并在数据库固定 subject，同一私有账本，不按邮箱或昵称自动关联 |
| 协议 2 / schema 15 迁移 | 已完成 | v15 支持每个来源固定一个 owner subject；真实已填充 v14 文件库重开升级后逐表核对原 Google 身份、全部设备凭据、财务数据、持仓与行情选择不变，已有 access / refresh 继续有效。旧版本升级仍保留 v12/v13/v14 的原迁移规则 |
| 回合取消与重启恢复 | 已完成 | 测试覆盖退出发生于回合启动等待期间、取消持久化与模型完成交错、工具调用取消、迟到输出抑制、重启恢复及重复恢复幂等 |
| 证券账户创建接口 | 已完成 | 新增已鉴权 `POST /v1/ledger/accounts`；复用领域账户模型、共享写队列和公开脱敏投影；账户、审计与幂等结果同事务，空建账户不增加交易或账本版本；测试覆盖请求重放、并发重复、注销排队和失败回滚 |
| 首次导入与账户标识一致性 | 已完成 | 真实空库经受控 Google 登录签发设备后，创建账户、上传 CSV、预览、确认并读取持仓/已实现；账户匹配规范化后，交易和哈希沿用数据库原账户键；不同大小写文件名重传仍识别为既有交易。导入解析器版本为 2，财务数据继续沿用既有表与口径 |
| 服务端完整回归 | 已完成 | **本次独立登录页 / 三提供方**：Node 24.19.0，`npm run check` 干净 TypeScript 构建及 **132 套件 / 400 项**全部通过，0 失败、0 取消、0 跳过。下节保留 2026-10-11 修复轮对历史不可复现统计的纠正及 368 项通过记录 |
| Flutter 登录、单助手与首次账户界面 | 已完成 | 空账户首页与账户选择器提供创建入口；导入保留原文件、创建后继续预览；创建重试、重复账户、预览失败恢复、取消、身份切换和组件卸载均有通过的回归；空库账本版本保持 0，首次财务导入才变为 1 |
| Flutter 凭据队列与汇总刷新 | 已完成 | 按凭据存储归属排队，所有生产 SecureCredentialStore 实例保持共享队列；独立内存存储互不阻塞，队列失败/空闲后正确释放；登录、刷新、退出及助手会话 ID 保持串行。汇总刷新有身份与请求代次检查，旧成功/失败不能覆盖较新的结果 |
| Flutter 用户页面与账户管理 | 已完成 | 登录后四个主入口为总览、股票、交易、我的；用户页显示可用的邮箱或昵称与登录方式，提供证券账户列表和新增、偏好、行情、关于和确认退出。未登录进入独立登录页，只选择账号；已移除普通页面的连接配置入口，退出或身份变化清除财务二级路由 |
| 金额隐私与金融配色 | 已完成 | 启动隐藏与本次眼睛按钮分离；财务金额、价格/费用/行权价、期权代码、Tooltip、读屏语义及金额图表同步处理；数量/日期/比例保留，AI 对话文字不处理。20 项隐私与 5 项配色回归通过，包含 14 类报告展开明细与图表像素检查 |
| 本机偏好与启动账户 | 已完成 | 独立安全存储键、串行读改写、失败保留最后成功值；按服务端、登录来源和稳定 subject 隔离账户偏好，保留原 Google 偏好键。恢复验证活跃账户，失效回 all，迟到恢复不覆盖主动选择；失败后手选单账户或 all 均可恢复查询 |
| 后台自动配置与应用版本 | 已完成 | 新安装自动使用发布时注入地址，保留历史地址与旧凭据绑定；偏好恢复完成前不请求另一个后台。登录页不展示地址或配置状态，总览 / 我的移除连接管理，关于只读取 App 版本；公开系统接口保留为内部部署诊断 |
| Flutter 依赖、分析与测试 | 已完成 | Flutter 3.47.2 / Dart 3.13.2；本轮移除技术配置后，静态分析零问题，8 个相关测试文件的 **108 项聚焦回归通过**，另显式运行 **5 个实际截图场景通过**。完整 236 项及图标修正 21 项为下方对应阶段的历史验证；依赖版本与锁文件未改变 |
| Flutter 深色界面 | 已完成 | 按用户批准的 Ticket assistant 风格精修稿，统一石墨灰 / 冰蓝主题、总览、持仓 / 交易、报告、我的、登录 / 导入与助手；金额隐私和多币种逻辑通过既有回归，Android 启动及系统栏同步深色 |
| Flutter 界面渲染检查 | 已完成 | 三方品牌图标保持统一；移除连接配置后重新渲染并目视检查 5 张图：390 px 登录、320 px / 1.6 倍字体、只启用 GitHub、我的与关于。未显示后台地址、连接管理或服务端版本；合成数据和关于版本仅用于截图 |
| Android 调试 APK | 本轮未构建 | 历史深色版和后续修复版均曾使用 1.0.0+3，具体产物及模拟机验证见下方历史记录；本轮独立登录页 / 新来源尚未打包，新体验需要从当前源码构建，并在正式分发前处理版本号与签名 |
| 可替换行情与参考估值 | 实现与受控验证完成 | 统一契约、按资产选源、Alpaca / MarketData 适配、缓存 / 请求预算 / 显式备用源、标准接口与 Genkit 工具已接入；USD 股票和标准期权按多空及乘数估值，缺价 / 过期不补零或合计；默认关闭，真实提供方权限未联调 |
| Flutter 行情面板 | 已完成 | 总览 / 股票增加参考估值和可展开报价明细，我的提供按需入口；价格保持十进制字符串，完整合约与账户匹配，金额隐私、启动范围恢复、迟到响应、账本版本变化、320 px 大字体均有回归；新增 16 项通过 |
| 界面切换行情源 | 已完成 | 「我的 → 行情与估值」分别选择股票 / 期权主源、备用或关闭；选择后自动提交并在服务端确认后生效，无保存 / 取消按钮；显示切换状态，失败恢复界面确认值，可重试或冲突后重载。schema 14 持久化，App / Genkit 共用；20 项设置回归覆盖关闭重开、逐项修复失效源、同会话刷新及迟到报价隔离 |
| 部署配置检查与服务探测 | 已完成 | 新增启动前 CLI、一次性配置生成、账本健康检查和公开探测；19 项新增测试通过。实际临时 DuckDB 经容器启动脚本启动、迁移至 schema 13、通过协议/鉴权/旧 MCP 探测并以 SIGTERM 正常退出；不代表容器或第三方联调 |
| Docker / Compose / Caddy 部署模板 | 进行中 | 多阶段构建、非 root 单进程、持久数据卷、临时上传目录、11 分钟退出宽限和可选 HTTPS 已落盘；Compose 通过官方 JSON Schema 静态校验。当前环境无 Docker Engine，镜像构建、Compose 启停及 Caddy 证书待实际验证 |
| 真实第三方登录 / 模型 / 行情 / 手机联调 | 待开始 | 本轮未配置真实 Google / LINUX DO / GitHub OAuth 应用和公开回调，也未新增模型 / 行情凭据；外部登录使用受控响应，完整真实系统浏览器往返仍需部署后验收 |
| GitHub 草稿 PR 交付 | 已完成 | 独立登录页、三种 OAuth、schema15 与后续图标 / 用户流程精简继续交付至[草稿 PR #1](https://github.com/GinPonson/Lyra/pull/1)；本轮基于 92f30dd 移除技术连接入口并更新 5 张截图。真实第三方登录 / 模型 / 行情 / 手机完整联调待验，本轮未合并、部署或构建 APK |

配置模板在 [`server/.env.example`](../server/.env.example)，启动、升级和待验步骤见 [迁移说明](genkit-oauth.md)。界面切源需要更新 App 与服务端；迁移前备份数据库并保留原加密主密钥。v14/v15 保留 v13 的 OAuth 凭据；从 v12 或更早版本升级时，重新登录并新建 Lyra 会话。真实第三方登录 / 模型 / 手机联调完成前，迁移 PR 保持草稿。

### 登录与设置移除技术配置（2026-10-11）

在图标补齐提交 `92f30dd` 上按用户反馈移除登录页「服务端连接」卡片、地址输入 / 保存 / 更换，以及总览和「我的」的连接管理入口。「关于」仅保留 App 版本与项目链接，不显示服务端版本或自动请求健康检测。登录内容按可用高度居中，大字体仍可滚动。

后台地址由发布构建的 `LYRA_SERVER_URL` 注入，保留凭据绑定地址和历史 `rememberedServerUrl` 的兼容；等本机偏好恢复完成再读取登录方式，配置缺失 / 无效时不发请求。原本需要用户处理的「未配置」「检查服务端地址」「更新服务端」等提示改为「暂不可用」与可重试的登录提示；登录异常按稳定错误码映射用户文案，不直接透传后台消息。行情来源切换入口继续保留，其连接失败提示也不再要求用户管理服务器。

旧凭据自动过期时，先撤销本机认证状态并清除凭据，再尽力记住原账本地址；地址写入失败仍保留本轮重新登录需要的原地址。这样既不会把旧令牌发给构建配置中的另一后台，也不会因取消地址输入而使旧安装失去登录入口。本机清理重试独立于登录目录加载，不等待网络请求完成。

**实际验证**：

- `flutter analyze --no-pub` 零问题。
- `flutter test --no-pub --reporter expanded --timeout 60s --concurrency=2`，聚焦 `login_screen_test.dart`、`profile_screen_test.dart`、`session_flow_test.dart`、`widget_test.dart`、`preferences_test.dart`、`system_repository_test.dart`、`market_settings_test.dart`、`market_widgets_test.dart`，**108 项全部通过**。覆盖首次安装自动配置、缺失 / 非法地址、偏好延迟读取、启动 / 刷新 401、原地址持久化与重启、偏好写失败不阻塞凭据清理、取消 / 退出、版本重试、无健康检测及既有行情切源。
- 显式运行 `login_capture_test.dart`，**5 个实际 Flutter 截图场景全部通过**，逐张目视检查无溢出：[390 px 登录](plans/assets/login-20261011-390.png)、[320 px / 1.6 倍字体](plans/assets/login-20261011-320-large.png)、[部分方式不可用](plans/assets/login-20261011-390-unavailable.png)、[我的](plans/assets/login-20261011-profile-390.png)、[关于](plans/assets/login-20261011-about-390.png)。全部为合成配置；关于截图中的版本为测试注入。
- 本轮未改服务端、依赖版本或锁文件；未重复运行服务端全量套件或 Flutter 全量套件。下方服务端 400 项、Flutter 236 项和图标修正 21 项保留为对应提交的历史结果。

### 登录按钮图标补齐（2026-10-11）

在独立登录页提交 `9ee4e0d` 上补齐 Linux.do 与 GitHub 图标。三种登录方式均使用官方原始 PNG，统一为 20 × 20 逻辑像素图标占位、12 逻辑像素文字间距；图片排除重复读屏语义。品牌资产来源与校验值见[资产说明](../apps/mobile/assets/auth/README.md)。

**本次验证**：`flutter analyze --no-pub` 零问题；既有 18 项登录组件回归与 3 个真实 Flutter 截图场景合计 **21 项全部通过**。逐张检查 390 px、320 px / 1.6 倍字体、部分来源未配置三种页面，图标清楚、对齐一致、无溢出；测试打包的 Linux.do 资产与最终源码文件 SHA-256 一致。下节 400 项服务端与 236 项 Flutter 完整回归为 `9ee4e0d` 实现轮的历史验证，本次静态图标修正未重复运行全量套件。

已更新现有三张[登录页截图](plans/assets/login-20261011-390.png)，登录流程、依赖版本及锁文件未改动。

### 独立登录页与 Google / Linux.do / GitHub（2026-10-11）

本轮在 `76e0002` 的修复后代码上实现独立登录入口。未登录、凭据失效或退出时回到完整登录页；成功后进入原四 Tab 工作台。页面提供三种方式和服务端地址管理，等待授权可取消；未配置、目录读取失败和旧服务器缺少目录均有明确状态和重试入口。更换服务器需确认，保存新地址后退出原登录，再到新地址选择方式。

服务端新增 `LoginProviderClient` 与统一 `OwnerLoginService`，公开 `/v1/auth/providers`，共用 `/v1/auth/{google|linuxdo|github}/start`、`callback`、`exchange`。原 Google URL 与设备凭据契约保留。三种方式共享来源限流与容量、App PKCE 和一次性消费；Google 保留 OIDC 校验，GitHub / Linux.do 使用固定用户 API 核对显式允许的稳定数字 ID。上游 token 不返回 App，公开目录不含密钥或 owner ID。

每个来源独立固定所有者身份，经服务器明确配置的各来源进入同一私有账本，不按邮箱或昵称自动合并。schema15 迁入原 Google owner 行并允许新来源无邮箱；真实 v14 文件库升级测试逐表核对 owner、设备凭据、账本、持仓和行情选择完全保留，旧 access / refresh 可继续使用。当前协议仍为 2。

移动端保留旧 Google 安全存储和偏好键，新增来源用 provider + subject 隔离偏好；令牌刷新不清导航，退出 / 身份变化清除旧二级页。退出本机清理失败可在登录页重试，迟到的远端撤销失败提示不会覆盖新登录。市场来源的选择即生效行为继续通过原回归。

**最终验证**：

- Node **24.19.0**，`npm run check` 退出码 0，干净构建后 **132 套件 / 400 项全部通过**，0 失败、0 取消、0 跳过；包括 15 项 OAuth client 边界、14 项统一服务 / 路由 / 实库迁移测试及部署配置和真实启动。
- Flutter **3.47.2** / Dart **3.13.2**，`flutter analyze --no-pub` 零问题；`flutter test --no-pub --reporter expanded --timeout 60s --concurrency=2` **236 项通过 / 3 个截图入口按默认跳过**。独立登录页、三种浏览器入口、无邮箱、取消 / 退出、身份切换、旧 Google 恢复和导航均有回归；旧测试中未登录仍显示财务导航的预期已按本次产品行为更新。
- 显式运行 `login_capture_test.dart` 的 **3 个实际 Flutter 渲染场景通过**，逐张目视检查标题、三按钮、服务端地址、不可用状态及大字体，无溢出。图中域名与来源可用性为演示配置；未用真实账号授权。
- 未升级依赖版本，Node / Flutter 锁文件不变；仅增加 Google 官方 G 图片资产及其来源说明。未重建 APK、未部署，真实三方 OAuth 往返仍需对应应用凭据和正式回调。既有浏览器授权者与发起设备的协议边界继续记录在[登录说明](genkit-oauth.md#app-登录过程)。

截图：[390 px 登录页](plans/assets/login-20261011-390.png) · [320 px / 1.6 倍字体](plans/assets/login-20261011-320-large.png) · [部分来源未配置](plans/assets/login-20261011-390-unavailable.png)。复现命令见[移动端说明](../apps/mobile/README.md)。

### 缺陷修复轮（2026-10-11）

对 `feat/genkit-oauth` 做了一轮完整代码审查后的修复，所有结论均以实测为准。

**先纠正不可复现的验证结论**：在干净检出（`npm ci` 后）上运行 `npm run check`，得到的是 **127 套件 / 364 项：360 通过、1 失败、3 取消**，与先前记录的「全部通过」不符。两个根因：

- `genkit-runtime.ts` 的回合超时定时器调用了 `timer.unref()`。模型调用若只是挂起的 Promise 且进程内无其它 handle，事件循环会在 100ms 截止时间之前就判空，超时**永不触发**；`genkit-runtime.test.ts` 的超时用例因此挂起，并连带取消后续 2 项。移除 `unref()` 后该套件 8/8 通过。
- `deployment-config.test.ts` 断言子进程 `stderr` 严格为空，而 `check-config` 的导入链（genkit → compat-oai → openai → node-fetch@2 → whatwg-url@5 → 内建 punycode）会在 Node ≥ 21 打印 `DEP0040`。断言改为忽略 Node 对第三方依赖的运行时警告，仍要求 CLI 自身零诊断输出。

**其余已修复缺陷**：

- 流式契约错位：服务端发 `payload.delta`，App 读 `payload.text`，导致 AI 回复**永不流式显示**，只在终态快照时一次性出现；服务端改为发 `text` 并新增契约断言（该错位在 `main` 上已存在）。
- 结算落库失败会永久占用内存中的活跃回合，使 start/cancel/close 全部 409、只能重启恢复；改为失败时也释放回合并丢弃缓存运行时，错误照常上抛。
- 供应商未返回 token 用量时被上报为 `0`（与「未知不估算为零」矛盾）；改为无法确定时不发用量事件。
- 启动顺序：信号处理器与暂存目录创建移到打开数据库（含不可逆迁移）之前；实测暂存目录不可创建时启动失败且**不会**创建/迁移数据库。
- 登录 `/start` 无凭据：改为按来源滚动窗口限流，容量被占满时淘汰最早事务而非对所有人 429，终态事务短暂保留后立即释放容量。
- 行情层：单证券 `invalid_response` 不再冷却整个来源；单行坏账本数据降级为无法估值而非整体 500；「形状合法但无可用参考价」改报 `no_data`；HTTP 403 不再误报为密钥被拒；重复查询参数不再静默忽略（返回 400）；零买价不再参与中间价。
- 部署：`.gitignore` 覆盖 `deploy/server.env*`（原先 `server.env.bak` 等含主密钥的文件不被忽略）；预检与运行时对 loopback HTTP 回调的判定统一；DuckDB 溢出目录固定到数据目录同级 `tmp/`（支持只读根文件系统）；Caddy 关闭管理 API；健康检查 `start-period` 与文档化的 `--wait-timeout` 对齐。
- Flutter：Agent 页非协议异常不再被吞并锁死（可重试错误态）；汇总缺失金额不再伪造成 `0`；畸形金额/标识字段在异步数据边界拦截而非渲染期崩溃；WebSocket 保留部署子路径；Android 安全存储改用 EncryptedSharedPreferences 并关闭 Auto Backup。

**验证**：`npm run check` 退出码 0，**128 套件 / 368 项全部通过**；`flutter analyze --no-pub` 零问题，`flutter test --no-pub` **212 项通过 / 2 项跳过**（修复前 198 / 2）；端到端 smoke 复验协议 2、schema 14、`/mcp` 404、未认证 401、SIGTERM 干净退出，并新增「暂存目录不可创建即失败且不迁移」的实测。

**本轮刻意未修**：登录事务绑定的是发起方而非浏览器授权方，未认证者可通过钓鱼链接取得所有者凭据（详见 [genkit-oauth.md 的安全边界](genkit-oauth.md#app-登录过程)）；补齐需要人类共享秘密，属协议与交互改动，待明确方案后再实施。另有一批低危清理项（删除 MCP/CLI 后的死代码与相关域函数）未做，避免为纯卫生问题牵动多个测试套件。

### Android 模拟机启动测试（2026-10-11）

对本轮修复后的代码做真机（模拟器）启动与连通性验收。AVD `Medium_Phone`（`emulator-5554`，Android 17）在测试前已在运行，未新建模拟器。

- **构建**：环境中 `JAVA_HOME` 指向不存在的 `/opt/homebrew/opt/openjdk@21/...`，Gradle 直接拒绝启动；改用本机已装的 Amazon Corretto 21（`/Users/Gin/Library/Java/JavaVirtualMachines/corretto-21.0.12.1/Contents/Home`）后构建成功。`flutter build apk --debug` **57.4s**，使用默认 `~/.gradle`（全局镜像可直接用于 Flutter 构建）。仅有既有的 `package_info_plus` KGP 提示与 SDK XML 版本提示，不影响产物。
- **产物**：`apps/mobile/build/app/outputs/flutter-apk/app-debug.apk`，**167,108,575 字节**，SHA-256 `b296ec5d2ef3d338344f50792757cdf689daf26d0cadc9166ee7109cbfcd071a`；`aapt2 dump badging` 确认 `com.lyrafinance.lyra_mobile`、versionCode **3**、versionName 1.0.0、minSdk 24 / targetSdk 36。注意该 APK 与先前交付的 `1.0.0+3` 版本号相同，但已包含后来的行情面板与本轮修复，发布前应递增 versionCode。
- **安装与启动**：`android install` 成功；`am start` 后进程存活，`logcat` crash buffer 为空、无 Flutter 异常，四个主 Tab（总览 / 股票 / 交易 / 我的）均正常渲染。
- **修复项实测**：未登录总览的「净入金 / 现金余额 / 股息收入」显示 **`—`**，不再是伪造的 `0.00`（对应本轮「缺失金额不再编造为零」）；「我的」页展示 Google 登录入口、证券账户、启动时隐藏金额、启动账户、涨跌配色等分区；金额隐私开关可正常交互。
- **连通性（`adb reverse` + 本地服务端）**：在 App 内保存服务端地址 `http://127.0.0.1:3141` 后，`POST /v1/auth/google/start` 返回 201（协议 2 / schema 14），App 随即调起系统浏览器打开 `accounts.google.com` 授权页；服务端日志同时出现 `/v1/system/info` 与 `/v1/health` 探测。
- **边界**：使用合成 `client_id`，未完成真实 Google 授权，因此未签发设备凭据、未进入账本页面；本次只验证启动、渲染与 App↔服务端链路，不代表真实 OAuth、模型或行情联调完成。登录事务被放弃后 App 仍按约 2 秒间隔轮询 `/v1/auth/google/exchange`，直至事务十分钟过期，属既有设计行为。
- 截图：[未登录总览（金额显示 `—`）](plans/assets/android-smoke-20261011-overview.png)、[我的与偏好](plans/assets/android-smoke-20261011-profile.png)。

### 选源即时生效验证（2026-10-11）

- 移除行情设置的「保存 / 取消」按钮及未提交草稿。股票、期权的主来源、备用来源或关闭每次选择后立即提交；服务端确认后持久化并刷新全部行情查询。重复选择当前值不发请求。
- 请求期间显示「正在切换…」并禁用来源控件，关闭后重开仍可看到待确认状态。同一登录会话中关闭面板不打断已提交请求；换服务器或登录后旧响应不能影响新会话。
- 失败时界面恢复最后确认的选择；重试沿用原目标和原 `expectedRevision`，服务端已提交但响应丢失时仍可幂等确认。网络失败不代表服务端回滚。配置变化或 409 冲突要求显式重新加载后再选择，避免覆盖其他设备的新值。
- 服务端允许在原资产、原位置保留已失效的旧来源，支持逐项修复多个失效设置；有效路由继续过滤这些失效来源。新增或移动来源仍需通过目录、资产支持与凭据校验，版本冲突先于变更校验处理。协议 2、schema 14 保持不变。
- 服务端 `npm run check`：Node 24.19.0，干净 TypeScript 构建，**127 套件 / 364 项全部通过**，0 失败、0 跳过。较手动保存版新增 3 项回归，覆盖跨资产 / 同资产多失效来源逐项修复、保留原位置的幂等重试，以及新增 / 移动失效来源与陈旧版本拒绝。（注：该轮记录的「全部通过」在干净检出上不可复现，见[缺陷修复轮](progress.md)。）
- Flutter `analyze --no-pub` 零问题；`test --no-pub --reporter expanded --timeout 60s --concurrency=2` **198 项全部通过**，默认图片捕获入口 2 项跳过。20 项设置测试已更新为即时提交，包含失败恢复、已提交但响应丢失后的重试、关闭重开时的等待状态、冲突和迟到报价隔离；另显式启用 2 个截图用例，与设置测试合计 **22 项通过**。
- 生成并目视核对 3 张实际 Flutter 新截图，使用合成 HTTP 响应并显示“演示配置”：普通手机完整设置页、打开来源下拉，以及 320 px / 1.6 倍字体期权来源区。依赖版本和锁文件未改变；未使用真实行情凭据、部署服务或重新构建 APK。

当前截图：[390 px 设置页](plans/assets/market-settings-instant-20261011-390.png)、[来源下拉菜单](plans/assets/market-settings-instant-20261011-390-picker.png)、[320 px / 1.6 倍字体期权来源区](plans/assets/market-settings-instant-20261011-320-large.png)。从 `apps/mobile` 复现：

```bash
flutter test --no-pub test/market_settings_capture_test.dart \
  --dart-define=LYRA_REVIEW_DIR=/absolute/output \
  --dart-define=LYRA_CJK_FONT=/absolute/NotoSansCJKsc-Regular.otf \
  --dart-define=LYRA_FLUTTER_ROOT=/absolute/flutter \
  --reporter expanded
```

### 界面切换行情源验证（2026-10-11，手动保存版历史记录）

以下记录首次实现时的保存 / 取消交互与当时验证结果；当前行为已由上节即时生效交互替代。

- 「我的 → 行情与估值」增加独立的行情来源设置：股票、期权分别选择主来源和一个可选备用来源，支持关闭和取消未保存的修改。来源名称、支持资产及凭据配置状态来自服务端目录，Flutter 不写死供应商名单。未配置来源不可选，合法但已移除的旧来源保留显示，供用户修复。
- 新增已鉴权 `GET / PUT /v1/market/settings`，使用 `expectedRevision` 检测并发冲突；保存走既有写队列，在队列中重新鉴权、重读当前版本并提交事务，成功后同步热切换。schema 14 保存全局来源顺序、版本和更新时间；真实 DuckDB 重开测试验证恢复，账本版本及财务记录不变。env 来源值只在没有保存记录时作为默认，模板继续为 `none`。
- 每次持仓估值在外部请求前固定来源快照，所有批次和最终 `settingsRevision` 一致。热切源不重建供应商，不清空已有缓存、预算或故障冷却；切回原来源仍受原请求额度约束。App 成功后刷新全部行情查询，切源前的迟到报价不能覆盖新结果。
- 设置与持仓读取独立，空持仓和账本失败不阻止选择。失败保留草稿，409 提供显式重新加载，快速 GET 也会正确替换旧草稿；旧服务端 404 明确提示升级。保存时关闭面板仍完成同会话结果刷新，令牌或账本刷新不打断保存；真正更换服务器或登录时，旧响应被丢弃。
- 服务端 `npm run check`：Node 24.19.0，干净 TypeScript 构建，**127 套件 / 361 项全部通过**，0 失败、0 跳过。较行情抽象阶段新增 16 项，包含设置持久化、事务失败回滚、重启、冲突、排队注销、配置目录、缓存保留与多批次快照；实际 `main.js` 启动检查新增设置 GET / PUT 未登录拒绝断言。
- Flutter `analyze --no-pub` 零问题；`test --no-pub --reporter expanded --timeout 60s --concurrency=2` **198 项全部通过**，图片捕获入口 2 项按默认跳过。包含新增设置测试 **20 项**；另显式启用实际 Flutter 截图用例，**2 项通过、3 张 PNG**。图片使用合成 HTTP 响应并显示“演示配置”，不调用行情商。已目视核对普通手机、打开下拉以及窄屏放大字体的选择与保存操作。未修改依赖版本或锁文件。
- 本次未使用真实供应商凭据，未部署服务或重新构建 APK。此前 `1.0.0+3` APK 不包含行情面板与来源设置；实际权限、VPS 和手机联调仍待执行。说明与 API 见[行情接入与换源](market-data.md)。

该阶段历史截图：[390 px 设置页](plans/assets/market-settings-20261011-390.png)、[来源下拉菜单](plans/assets/market-settings-20261011-390-picker.png)、[320 px / 1.6 倍字体保存区](plans/assets/market-settings-20261011-320-large.png)。当前截图复现命令见上节。

### 行情抽象与参考估值验证（2026-10-11）

- 新增供应商无关的 `MarketInstrument`、`MarketQuote`、`MarketDataProvider` / `MarketDataReader`，由独立报价服务处理按资产路由、显式备用顺序、并发合并、有界缓存、请求预算与超时。缓存仅保存公开行情；归一化剥离额外字段并复制对象，未知错误文本不出现在响应中。
- Alpaca 使用截止当前时间前 16 分钟的历史 SIP 股票买卖价和 OPRA 期权一分钟 K 线；期权 close 时间使用区间结束。MarketData 的股票 / 期权请求显式使用 `mode=historical` 或 `mode=delayed`，防止账户升级后默认模式变为 live 而标签不变。代码使用最近可得数据，不传历史日期，不提供历史组合净值或对齐到同一天的全组合估值。
- App 路由 `GET /v1/market/positions` 和 Genkit 的第 18 个工具 `market-positions` 共用授权持仓与估值层。原有成本及 17 个账本工具口径保持；新估值统一选价、多空方向、标准期权 100 倍乘数和费用排除规则。非 USD、无法准确识别的合约返回明确缺失状态；缺价 / 过期的币种组不输出完整合计。
- 服务端 `npm run check`：干净 TypeScript 构建通过，**125 套件 / 345 项全部通过**，0 失败、0 跳过。相对部署阶段增加 47 项，覆盖适配器、配置、缓存 / 并发 / 预算 / 超时、完整证券身份、金额精度与真实 DuckDB 持仓到市场估值的集成；既有路由与配置进程测试同步扩展。
- Flutter `analyze --no-pub` 零问题；`test --no-pub --reporter expanded --timeout 60s --concurrency=2`：**178 项全部通过**，默认截图入口 1 项跳过。新增 16 项覆盖标准响应解析、账户和完整合约匹配、缺价 / 过期、隐私及辅助阅读、启动恢复、范围切换和 ledgerVersion 变化；320 px 放大字体与常规手机尺寸没有测试报告的溢出。依赖由现有缓存离线恢复，锁文件未改变。
- 没有真实 API Key 或外部行情请求；没有注册供应商账户、开通付费计划、部署服务或重建 APK。缓存 / 预算是进程内状态，重启清空，TTL 只在按需读取时触发刷新，不是后台每日定时任务。当前协议 2、schema 13 不变。
- 配置、换源步骤、价格时间语义与未来新增适配器的最小改动范围见[行情接入与换源](market-data.md)。

### 部署准备验证（2026-10-11）

- 新增 `Dockerfile`、只包含服务构建输入的 `.dockerignore`、`deploy/compose.yaml` 与可选 Caddy HTTPS 配置。服务端口只发布到主机回环地址，容器内监听 `0.0.0.0`；账本目录持久化，上传暂存使用 256 MiB tmpfs，退出宽限覆盖 600 秒回合上限。Caddy 默认运行日志删除请求 URI，访问日志保持关闭。
- `check-config` 复用正式入口的基础环境、Google 和 Genkit 配置解析，在打开数据库之前拒绝占位值、部分模型配置、缺少 owner 和不匹配的 HTTPS 回调；输出固定文案与变量名。新配置生成使用独占创建和 `0600` 权限，保留已有文件与主密钥；不会读取或执行 env 文件中的 shell 内容。
- `npm run check`：干净 TypeScript 构建通过，**119 套件 / 298 项全部通过**，0 失败、0 跳过。新增 10 项配置 CLI 测试与 9 项部署工具测试，包含进程级网络阻断、现有数据库不被打开、日志不泄露配置、初始化幂等、降级健康状态、鉴权与协议门禁、禁止跟随重定向及 GET / POST 旧 MCP 入口检查。
- 使用临时数据库和合成 Google 配置实际运行 `server/scripts/start-container.sh`：配置检查通过，数据库创建至 schema 13、协议为 2，健康脚本与公开探测全部通过；发送 SIGTERM 后退出码为 0。没有访问正式账本、Google 或模型服务。
- Compose YAML 通过官方 `compose-spec` JSON Schema（blob `c9db4d1cd27cd0353d679b58ab83fa8dfd442aaf`）与 Ajv 2020 静态校验；shell 语法和差异空白检查通过。当前环境没有 Docker Engine，不能声称已构建镜像、运行 Compose、验证 Caddy 配置或获取真实证书。
- 新工作目录离线安装因锁文件中的腾讯镜像 tarball 未缓存而未完成；随后复用同一锁文件下已存在的依赖完成构建与测试，没有修改锁文件。VPS 构建需能访问 npm 与腾讯镜像，尚未验证从零安装。
- 本次未改移动端，也未重建 APK。重新计算既有 `1.0.0+3` APK 的 SHA-256，与下方深色界面交付记录一致。真实域名、Google OAuth、兼容模型和手机联调继续待验，PR 保持草稿；操作顺序和停服备份见 [部署指南](deployment.md)。

### 深色界面交付验证（2026-10-11）

- 按批准的精修图落地现有 Flutter 应用；共享 Material 暗色状态、系统栏、总览和列表层级、设置与各弹层均已实现。保留已有 PNG 标识、字体、四 Tab、展开明细、账户范围及金融数据语义。
- `flutter analyze --no-pub`：零问题。
- `flutter test --no-pub --reporter expanded --timeout 60s`：**162 项全部通过**；另有 1 个需显式启用的截图入口按设计跳过。扩大正盈亏显示检查，不放宽原精度、隐私、账户或身份断言。
- 单独启用 `test/design_capture_test.dart`：**4 个场景通过，15 张 PNG**。在实际 MaterialApp 内断言 1.6 倍文字缩放，截图前等待证券图解码；目视核对精修图和实际画面的并排对照。最后修复的小屏标题、分组头和导入把手经独立目视复核通过。
- `flutter build apk --debug --no-pub`：退出码 0，Gradle 56.1 秒；保留当前依赖与构建配置。构建输出有现有 KGP 兼容性和 SDK XML 版本提示，不影响本次成功产物。
- APK：`1.0.0` / versionCode `3`，`com.lyrafinance.lyra_mobile`；min SDK 24、target SDK 36，arm64-v8a / armeabi-v7a / x86_64；**167,049,415 字节**。`aapt dump badging` 与 `apksigner verify --verbose --print-certs` 通过，Android Debug / v2 签名。
- APK SHA-256：`c349c256af67d97dfc9b815681a91d3901585488ff3e35f0f2de8db9e1dab7d1`。
- 构建前后核对 114 个移动端源码、Android 配置、依赖锁文件和资产；内容清单 SHA-256 为 `1263a9f1aef9acd695a55d3df89243cd58967835f038e9b18877fdb189b54865`，交付副本与原构建 APK 哈希一致。
- 视觉依据和差异说明见 [design-qa.md](../design-qa.md)。本轮无服务端变更；服务端 117 套件 / 279 项为此前通过记录，未重新声称真实 Google、模型或 Android 真机联调已完成。

### 用户页面交付验证（2026-10-11，先前 versionCode 2）

- `flutter analyze --no-pub`：零问题。
- `flutter test --no-pub --reporter expanded --timeout 60s`：**162 项全部通过**；原有 91 项 + 新增 71 项，没有放宽既有财务或身份断言。
- 实际 Flutter 渲染检查：390 px 手机、320 px / 1.6 倍字体、1440 px 宽屏，以及未登录、连接、配色、关于与隐藏金额总览；均使用合成数据，未连接真实账户。
- `flutter build apk --debug --no-pub`：最终退出码 0。首次打包因原生库合并目录残留临时文件导致符号处理失败；仅清理对应生成中间产物后重建成功，没有修改 SDK、签名规则或应用源码。
- APK：`1.0.0` / versionCode `2`，`com.lyrafinance.lyra_mobile`；min SDK 24、target SDK 36，arm64-v8a / armeabi-v7a / x86_64；**167,030,491 字节**。`aapt dump badging` 与 `apksigner verify --verbose --print-certs` 通过，Android Debug / v2 签名。
- APK SHA-256：`d1409d9e5ac9e0f5ea25734e1c4b77e4b090e7f9211cea0866f3f0b26f957596`。
- 构建前后核对 112 个移动端源码、Android 配置、依赖锁文件和资产；内容清单 SHA-256 为 `d79078a11b7a2f5a264fe415e531ab525540ace8e534cb57ae01731ca7be93bf`，交付副本与原构建 APK 哈希一致。
- 本轮没有服务端改动；117 套件 / 279 项是上一轮服务端完整回归的通过结果。本轮仍未进行真实 Google、真实模型或 Android 真机验收。

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
| Phase 2 | Agent 会话 REST 路由与设备认证门禁 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过（缺/错令牌 401、探测快照、会话创建、未知账户 409、回合运行、终态后取消 409、关闭与 closed 快照读取）；真实进程 smoke 验证装配后缺/错凭据均 401 且 SIGTERM 干净退出；累计 269 项通过 | 2026-09-02 | `registerAgentRoutes` 暴露 `GET /v1/agents`、`POST/GET/DELETE /v1/agent/sessions[/:id]`、`POST .../messages`、`POST .../cancel`，全部要求设备访问令牌 Bearer；认证网关只加载 v6 令牌哈希与设备记录并复用既有领域认证，失败统一转为 401 稳定设备错误码；创建会话的账本版本、截止时间与可用性由服务端权威判定；回合 POST 阻塞到终态并返回规范快照与事件；`main` 以环境变量装配执行器工厂、精确版本白名单与共享 MCP 凭据存储，可用性探测懒执行并缓存，不阻塞启动 |
| Phase 4 | Agent WebSocket 事件表面 | 已完成 | 1 项真实监听端口集成测试通过（未认证 4001 关闭、空游标订阅、实时 started/completed 推送、游标 1 续传事件 2、超前游标 snapshot_required、非法帧稳定错误）；累计 269 项通过 | 2026-09-02 | `GET /v1/events` 经 @fastify/websocket 11.1.0 升级并同受协议门禁与设备认证；客户端以 `subscribe` 帧绑定会话与游标，服务端从 orchestrator 有界缓冲续传或要求重新取快照，随后实时推送该会话的持久化事件；路由不补造事件、不回放已溢出缓冲；orchestrator 新增 `subscribe` 订阅与 `onEvent` 外发钩子 |
| Phase 4 | 回合边界跨供应商切换路由与 handoff 投递 | 已完成 | 会话路由集成测试扩展第 2 项：同供应商切换 400、切换后 provider/revision/边界 system 消息持久化、切换后回合由新供应商执行且 handoff 一次性投递（记录 provider 与投递标记）；累计 270 项通过 | 2026-09-02 | `POST /v1/agent/sessions/:id/switch` 复用领域 `prepareAgentSwitch`；provider/revision 与边界消息同事务 CAS 落库，orchestrator 采纳新快照、撤销旧 MCP 凭据并把有界 handoff 格式化为一次性 prompt 前缀交给切换后首个回合；执行器契约新增 `handoffContext` 并由三个执行器并入 prompt；AgentSwitchError 映射 409，同供应商 RangeError 映射 400 |
| Phase 2 | 账本与分析只读 REST 路由 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过（无令牌 401、缺省范围 400、空账本 summary 权威语境、days 字符串收窄、缺 days 400、未知账户 400、trades 分页参数、analytics 路由）；真实进程 smoke 验证 main 注册后 401/426 门禁；累计 271 项通过 | 2026-09-02 | `registerLedgerRoutes` 暴露 7 条 `/v1/ledger/*` 与 9 条 `/v1/analytics/*` 只读路由，全部要求设备访问令牌；金融计算复用 Tool Registry 的唯一执行路径（App 与 Agent 消费同一份确定性结果），账户范围必须显式携带，账本版本/截止时间/获取时间由服务端生成并附 `LedgerReadResult` 元数据；查询参数按工具公开 schema 收窄校验，新增 `resolveLedgerReadScope` 与既有领域范围解析共用规则 |
| Phase 2 | 导入上传、预览、提交与取消业务路由 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过：上传解析预览（2 行、目标账户映射）、preview_ready 状态读取、错误令牌 409 IMPORT_PREVIEW_STALE、确认提交产生账本版本 1 与 2 条交易、重放提交冲突、重复文件 insertableRows 0、取消进 cancelled；真实进程 smoke 验证路由 401 门禁；累计 272 项通过 | 2026-09-02 | `registerImportRoutes` 暴露 `POST /v1/imports/preview`（octet-stream 原始流 + 仅展示用文件名头 + 显式账户选择）、`GET /v1/imports/:id`、`POST .../commit`、`POST .../cancel`，全部要求设备访问令牌；新增 `import-job-repository`（v7 作业创建/状态 CAS/预览与令牌哈希落库/授权读取/账户列表）与 `ImportService` 进程内编排：解析结果只存进程内，作业状态、指纹与一次性确认令牌哈希持久化；提交在写队列内以最新账户事实生成计划并由 commit 仓储事务内把关版本、指纹与状态；解析或映射失败 CAS 到 rejected/failed 并清理 0600 临时文件；空账本 `preview_ledger_version` 按 NULL 落库与提交仓储约定一致；幂等键路由接线已于 2026-09-05 完成 |
| Phase 3 | Gmail 连接、映射与授权事务业务路由 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过（注入假 Google HTTP 边界）：授权事务创建含 PKCE 授权 URL、错误 state 400 稳定码、完成交换建立 pending_initial_scan 连接并验证邮箱身份、同 state 重放 400、连接列表、空映射列表、尾号不匹配改绑 409 ACCOUNT_MAPPING_REQUIRED、合法改绑 CAS 生效、`revoke=true` 断开撤销后密文清空；累计 273 项通过 | 2026-09-02 | `registerGmailRoutes` 暴露授权事务创建/完成、连接列表、映射列表与显式改绑、断开（可选远端撤销）六条路由，全部要求设备访问令牌；授权事务 record 只存进程内内存（重启即失效，App 重新发起授权）；`gmail-connection-repository` 持久化 v2 连接/映射并按稳定错误码闭集收窄读取；远端撤销失败不清除本地密文可重试；新增 `LYRA_SECRET_MASTER_KEY` 主密钥与 Google OAuth 客户端配置环境项；同步触发路由随同步编排器另行挂接 |
| Phase 2 | 设备配对与访问令牌签发路由 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过：配对码创建（展示格式校验）、错误码 400 PAIRING_CODE_INVALID、Ed25519 SPKI 公钥配对签发凭据、配对签发的访问令牌通过账本路由门禁、配对码一次性重放 400、刷新轮换签发新凭据、旧刷新凭据重放 401 DEVICE_CREDENTIAL_REUSED 并同族全代次撤销（新凭据同时失效）；累计 275 项通过 | 2026-09-02 | `registerDeviceRoutes` 暴露 `POST /v1/devices/pairing-codes`、`POST /v1/devices/pair`、`POST /v1/devices/tokens` 三条 Bootstrap 路由（凭据合法签发来源，不要求既有设备认证）；配对码记录只存进程内内存（短时一次性，重启即失效）；新增 `device-pairing-repository` 持久化设备、刷新凭据代次轮换（旧代 CAS + 新代插入单事务）与访问令牌哈希；刷新凭据重放触发 `planRefreshCredentialReuseRevocation` 同族全代次撤销；`main` 装配 `DevicePairingService` |
| Phase 2 | App 与 Agent 账户列表只读 REST 路由 | 已完成 | 既有账本路由集成测试扩展 accounts 端点断言：`scope=all` 返回空账本权威语境与空行集合；TypeScript strict 编译及完整回归累计 275 项通过 | 2026-09-05 | `LEDGER_TOOL_ROUTES` 新增 `/v1/ledger/accounts`，复用 `accounts` 工具既有执行路径、设备认证门禁与 `LedgerReadResult` 权威语境；App 导入账户选择与 Agent 共用同一份公开投影 |
| Phase 2 | App 侧 Schwab 文件导入上传界面 | 已完成 | `flutter analyze` 无问题；`flutter test` 7 项全部通过（新增导入仓储账户列表/显式账户预览/提交解析、409 映射候选与选择账户后重试提交的全流程 widget 测试、未配对提示测试）；既有导航与会话 widget 测试无回归 | 2026-09-05 | 新增 `ImportSheet`（总览页已配对入口）与 `ImportRepository`：octet-stream 原始字节上传 + 展示用文件名头；`ACCOUNT_MAPPING_REQUIRED` 时读取 `/v1/ledger/accounts` 候选列表，选择账户后携带 `accountId` 重新预览；客户端边界显式过滤归档账户；`LyraApiError` 扩展结构化 `details`（仅消费 `candidate_account_ids`） |
| Phase 2 | App Agent 会话界面（REST 回合联调） | 已完成 | `flutter analyze` 无问题；`flutter test` 10 项全部通过（新增 Agent 数据层可用性/会话创建/快照/回合/取消解析测试、Agent 页创建会话→发送回合→终态消息渲染全流程 widget 测试、未配对不给会话入口测试；既有导航/会话/导入测试无回归） | 2026-09-05 | Agent 导航页从占位骨架替换为真实界面：会话创建入口（固定 Claude/Pi/OpenCode 顺序）、消息流（用户/助手气泡 + 失败状态）、回合运行中的乐观用户消息与取消、账本版本与截止时间语境；`AgentRepository` 复用 `LyraApiClient` 认证与 401 刷新，`POST /messages` 阻塞到终态并解析快照与统一事件序；未配对与错误态只显示稳定错误码，不伪造金融内容 |
| Phase 2 | App Agent WebSocket 事件流 + 回合边界切换 + 会话恢复 | 已完成 | `flutter analyze` 无问题；`flutter test` 20 项全部通过（新增帧解析 subscribed/events/snapshot_required/error/unknown 五类、控制器 delta 累积与游标推进、重复事件忽略、序号缺口与 snapshot_required 触发权威快照刷新、terminal 帧清空预览并刷新、switch 请求解析与 409 稳定错误、双容器重启恢复会话视图、404 清除本地会话 ID、idle 会话切换供应商 widget 全流程；既有 13 项无回归） | 2026-09-05 | `AgentLiveEvents` 连接 `/v1/events`：Bearer 头认证、subscribe 帧绑定会话与游标，`message.delta` 实时累积为流式预览气泡（▍光标），重复事件忽略、序号缺口或 snapshot_required 一律重取权威快照（快照为唯一事实，客户端不补造事件）；回合边界切换入口仅在 idle 且非运行中可用，409 显示稳定错误码；会话 ID 经安全存储持久化，进入 Agent 页自动用服务端快照恢复、会话已关闭则清除本地 ID 回到创建视图；本地只存会话 ID 不缓存金融数据 |
| Phase 2 | App Agent 工具调用列表展示 | 已完成 | `npm run check` 127 套件 277 项全部通过（新增工具调用公开列表路由集成测试：列表稳定排序、状态/摘要/耗时/时间归一、内部授权判定与会话 ID 不进入公开记录、未知会话 404）；`flutter analyze` 无问题；`flutter test` 21 项全部通过（新增数据层三态解析：completed/started/failed 摘要与错误码、回合完成后渲染工具调用卡片 widget 测试；既有项无回归） | 2026-09-05 | 服务端新增 `GET /v1/agent/sessions/:id/tool-calls`：`loadAgentToolCalls` 只读投影（状态/授权/错误码闭域校验、JSON 摘要扁平化、TIMESTAMPTZ 归一 UTC ISO），会话区分「不存在→404」与「无调用→空列表」，经既有 `createPublicAgentToolCallList` 稳定排序输出；App 端会话页在会话进入、回合终态与快照刷新后拉取并渲染「工具调用」卡片（工具名、状态与耗时、参数/结果摘要、稳定错误码），失败不阻塞会话流 |
| Phase 2 | App Agent 事件流断线自动重连退避 | 已完成 | `flutter analyze` 无问题；`flutter test` 24 项全部通过（新增 3 项重连退避测试：意外断开按指数退避自动重连且 subscribe 帧带最新游标、主动 close 与 dispose 完全静默、连续失败退避封顶且 subscribed 确认后计数重置；既有 21 项无回归） | 2026-09-05 | `AgentLiveEvents` 内建自愈重连：意外断开按 1s 起指数翻倍、30s 封顶自动重连，重连带最新事件游标由服务端续传缓冲或要求快照；退避计数只在收到服务端 `subscribed` 确认后重置，`onError`/`onDone` 双触发只排一次重连；主动 close 与 dispose 不再重连；通道工厂可注入（`stream_channel` 假通道测试真实断开/重连生命周期），控制器保留句柄不再在断开时丢弃 |
| Phase 4 | MCP 生产入口与 Agent 回合编排 | 已完成 | 官方 transport、短期凭据、完整 17 工具注册表、会话/回合/工具调用写仓储、回合编排内核、生产入口 `/mcp`、三供应商回合执行器、会话 REST 路由、WebSocket 事件表面、回合边界切换、工具调用公开列表路由、账本只读路由、导入业务路由、Gmail 连接/映射/授权/同步路由、设备配对签发路由、Flutter 真实数据接入、App 侧文件导入上传界面与 App Agent 会话界面（REST 回合联调、WebSocket 事件流、回合边界切换、会话恢复、工具调用列表展示、断线自动重连退避）均已通过测试 | 2026-09-05 | 服务端 127 套件 277 项、App 24 项测试全部通过；App Agent 界面主链路与全部已排期增量完成，进度表内无未完成排期项，后续新功能待产品排期；不得绕过生命周期直接在启动时构造虚假 running 会话或长期凭据 |
| Phase 3 | Gmail 同步编排器与同步触发路由 | 已完成 | 1 项真实 DuckDB + 完整 HTTP 服务集成测试通过（注入覆盖 token/profile/messages/history 的假 Google 边界）：首次有界扫描缺确认 409、确认后建立 active 连接并推进游标 900、增量拉取导入新交易并推进游标 950、空批次保持游标；库内 gmail 来源交易 2 条、imported 消息 2 条、映射 1 条；累计 274 项通过 | 2026-09-02 | `GmailSyncService` 装配状态机决策、token 刷新（授权失效 CAS 连接进 error 并返回 401 GMAIL_AUTH_REVOKED）、增量 history 拉取（404 映射 GMAIL_HISTORY_EXPIRED 落 needs_resync）、首次扫描 profile 基线与有界查询、eConfirm 确定性解析、显式账户映射（首个尾号随同步持久化）、`persistGmailMessageImport` 单消息原子提交（复用标准交易/FIFO 写入形状，gmail_import 版本）与游标仅在全批持久终态后推进；`POST /v1/google/connections/:id/sync` 接入 main |
| Phase 2 | Flutter 接入真实数据（配对、API 客户端与账本汇总） | 已完成 | `flutter analyze` 无问题；`flutter test` 4 项全部通过（原导航骨架、配对→汇总全流程 widget 测试、summary 模型解析 2 项）；`GRADLE_USER_HOME=... flutter build apk --debug` 成功；Medium_Phone 模拟器安装启动正常（无 MissingPlugin/崩溃，配对面板可见、五导航语义保留）；累计服务端 275 项测试通过 | 2026-09-02 | 新增 `LyraApiClient`（Dio：协议头、Bearer 注入、401 刷新一次重放、稳定错误码转换）、凭据安全存储抽象（flutter_secure_storage 9.2.4 + 内存测试实现）、配对/账本数据层与 Riverpod 会话控制器；配对时生成 Ed25519 设备密钥对（公钥 SPKI 提交、私钥入安全存储）；总览未配对展示配对表单、已配对展示真实账本汇总（金额保持服务端十进制字符串原样、多币种无伪合计）；修复 `_StatusRow` 长时间戳窄屏溢出；`flutter_secure_storage` 固定 9.2.4（最新版 AAR 要求 compileSdk 37，本机 SDK 36） |
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
| Phase 0 | 导入提交幂等键路由接线 | 已完成 | 新增导入路由集成测试 1 项：非法幂等键 400、业务事务内失败（注入 LEDGER_VERSION_CONFLICT）后 in_progress 记录释放并可同键重试、带键首次提交完成记录（result_resource_id 指向 importId）、进程内解析结果丢弃后同键重放返回持久化结果、同键不同请求 409 IDEMPOTENCY_KEY_REUSED、处理中同键请求 409 IDEMPOTENCY_REQUEST_IN_PROGRESS（retryable）；TypeScript strict 编译及完整回归累计 276 项通过 | 2026-09-05 | 新增 `mutation-idempotency-repository`（v10 表读写、过期替换、事务内 CAS 收口、失败释放）；`ImportService.commitImport` 接受设备作用域幂等上下文：幂等判定先于进程内 entry 检查（成功后重放不依赖易失解析结果）、请求指纹为 importId+令牌散列、in_progress 先于写队列落库、完成收口在提交事务内与业务写入同生共死；`POST /v1/imports/:id/commit` 接受可选 `Idempotency-Key` 头（16–200 位安全字符，非法明确 400）；App 端 `commit` 发送 `commit-<importId>` 幂等键并经假适配器断言 |
| Phase 0 | 服务健康与外部集成降级快照 | 已完成 | 2 项新增测试通过；累计 229 项通过 | 2026-08-31 | 健康快照区分进程存活、数据库/schema/账本版本、写队列 readiness、Gmail 需处理连接数和 Agent 可用数量；数据库不可用或写队列 draining 标记 unavailable，Gmail/Agent 故障只标记 degraded，不阻断账本只读；未探测 Agent 明确 unknown；不公开邮箱、CLI 版本、路径或失败详情 |

| Phase 2 | App 设备配对与令牌刷新真机修复 | 已完成 | `flutter analyze` 无问题;`flutter test` 26 项全部通过(新增配对/刷新响应形状解析回归 2 项);`flutter build apk --debug` 成功;Medium_Phone 模拟器配对真实服务端成功并加载空账本权威语境 | 2026-09-05 | `PairingRepository` 配对与刷新请求统一携带 `x-lyra-protocol-version`(裸 Dio 此前缺协议头,真实服务端 426 拒绝配对);刷新响应改用独立四字段 `RefreshedDeviceTokens` 模型(服务端 `/v1/devices/tokens` 不含 deviceId/displayName,原共用六字段模型在 401 刷新时 Null 强转抛未捕获异常并中断请求重放);刷新后设备身份沿用本地安全存储快照 |
| Phase 2 | 服务端配对码消费顺序修复 | 已完成 | `npm run check` 127 套件 278 项全部通过(新增回归 1 项:过期记录不阻塞其他配对码匹配);修复后真实配对链路复验成功 | 2026-09-05 | `consumePairingChallenge` 改为先做哈希等值比较再检查 used/expired 状态;原实现下服务端遍历挑战记录时,任意一条过期记录会先行抛 410,首个过期配对码即永久阻塞后续所有设备配对直至进程重启(模拟器联调中发现) |
| Phase 4 | Agent 事件流连接失败安全收口 | 已完成 | `flutter analyze` 无问题;`flutter test` 26 项在服务端运行与停止两种环境下均全部通过(含干净 HEAD 上即失败的既有 4 项编排测试);`flutter build apk --debug` 成功 | 2026-09-05 | `WebSocketChannel.connect` 的 `ready` Future 失败此前无人处理,连接被拒或升级失败时成为未处理异步异常(干净 HEAD 复测确认既有缺陷,与本次改动无关);显式吞掉 `ready` 错误,断开重连统一由 stream 的 onError/onDone 驱动 |
| Phase 2 | Medium_Phone 模拟器端到端 smoke | 已完成 | 全新 Flutter 模板项目复现 file_selector_android 0.5.2+10 构建失败,dependency_overrides 钉回 0.5.1+9(groovy 构建)后 `flutter build apk --debug` 成功;模拟器安装启动无崩溃、五导航完整;配对→空账本权威语境(已同步)→导入面板→系统文件选择器选 CSV→octet-stream 上传→`ACCOUNT_MAPPING_REQUIRED` 稳定错误展示→Agent 页三供应商会话入口按固定顺序渲染→Claude 会话创建成功(空闲、账本版本语境);截图存档 `plans/assets/smoke-20260905-*.png` | 2026-09-05 | 0.5.2+10 的 Kotlin DSL 构建脚本在 Flutter 3.47.2(AGP 9.1.0/Gradle 9.3)下 `kotlin{}`/`android{}` 访问器无法解析且与项目配置无关,上游修复前保留钉版;导入提交与 Agent 真实回合不在 smoke 范围(空账本无账户可映射,账户创建路由待排期;真实模型会话按既有纪律不启动) |

| Phase 2 | App 极简专业白色主题重构 | 已完成 | `flutter analyze` 无问题;`flutter test` 26 项全部通过;`flutter build apk --debug` 成功;Medium_Phone 模拟器目视验收总览/持仓/Agent 会话三页,截图存档 `plans/assets/theme-20260905-*.png` | 2026-09-05 | 按用户要求弃用深色主题,改为纯白画布 + 近黑正文 + 发丝分割线 + 单一靛蓝强调(Stripe/Linear 式极简专业风);`LyraColors` 重写为语义化调色板(accent/ink/hairline 等),`LyraTheme` 提供浅色 Material 3 主题并全局启用等宽数字;总览页移除装饰性光晕绘制改为排版主导的 Hero 卡片;五个页面与导航、输入框、按钮、气泡、芯片全部适配;系统栏图标切换为深色 |

| Phase 2 | App 轻量机构风主题(参照 Coinbase 设计系统) | 已完成 | `flutter analyze` 无问题;`flutter test` 26 项全部通过;`flutter build apk --debug` 成功;Medium_Phone 模拟器目视验收总览与 Agent 会话页,截图存档 `plans/assets/theme-20260905-04/05-*.png` | 2026-09-05 | 应用户要求在 `voltagent/awesome-design-md` 73 个品牌设计系统中重选风格;对比 Wise(青柠绿/鼠尾草)、Revolut(即原深色主题的 token 来源,被否)、Coinbase 三份 DESIGN.md 后采用 Coinbase token:白底 + ink `#0a0b0d`、蓝色 `#0052ff` 单一电压只做主 CTA/选中态/链接、发丝线 `#dee1e6`、软灰表面 `#f7f7f7`/`#eef0f3`、涨跌语义仅文字色、展示级标题 weight 400 不加粗、金额与时间戳改等宽字形、CTA 改药丸形、输入框聚焦 2px 蓝边;候选对比与 token 出处记录于 progress 备注 |

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
| 2026-09-02 | `npm run check` | 通过 | 新增 Agent 会话 REST 路由集成测试 1 项（设备认证门禁、探测快照、会话创建/快照/回合/取消/关闭全流程、未知账户拒绝）与 WebSocket 事件表面集成测试 1 项（未认证 4001、实时推送、游标续传、snapshot_required、非法帧）；新增 v6 访问令牌与设备记录仓储加载、认证网关、可用性探测缓存、会话创建/关闭命令与 `persistAgentSessionClosed`；122 个测试套件、累计 269 项测试全部通过 |
| 2026-09-02 | `npm run check` | 通过 | 会话路由集成测试新增跨供应商切换流程 1 项：同供应商切换 400、`persistAgentSessionSwitch` 同事务落库 provider/revision 与边界消息、orchestrator 采纳快照并撤销旧凭据、切换后回合由新供应商执行且 handoff 前缀一次性投递；执行器契约新增 `handoffContext`；122 个测试套件、累计 270 项测试全部通过 |
| 2026-09-02 | `npm run check` | 通过 | 新增账本与分析只读 REST 路由集成测试 1 项：16 条 `/v1/ledger/*`、`/v1/analytics/*` 路由复用 Tool Registry 执行路径，覆盖认证 401、显式范围强制、空账本权威语境、查询参数 schema 收窄与未知账户拒绝；123 个测试套件、累计 271 项测试全部通过 |
| 2026-09-02 | `npm run check` | 通过 | 新增导入业务路由集成测试 1 项：上传解析预览、状态读取、确认提交产生账本版本与交易、错误令牌与重放冲突 409、重复文件幂等、取消收口；新增 `import-job-repository` 与 `ImportService` 进程内编排并接入 `main`（新增 `LYRA_IMPORT_STAGING_DIR` 配置）；124 个测试套件、累计 272 项测试全部通过 |
| 2026-09-02 | `npm run check` | 通过 | 新增 Gmail 连接业务路由集成测试 1 项（注入假 Google HTTP 边界）：授权事务创建/完成、state 重放拒绝、连接与映射公开投影、显式改绑 CAS、断开撤销清空密文；新增 `gmail-connection-repository`、`GmailService` 与 `registerGmailRoutes` 并接入 `main`（新增 `LYRA_SECRET_MASTER_KEY` 与 Google OAuth 配置项）；125 个测试套件、累计 273 项测试全部通过 |
| 2026-09-02 | `npm run check` | 通过 | 新增 Gmail 同步编排器集成测试 1 项：首次有界扫描建立 active 连接并推进游标、增量 history 拉取导入新交易并推进游标、空批次保持游标；新增 `gmail-message-repository`（单消息原子提交复用标准交易/FIFO 写入形状）、`GmailSyncService` 与同步触发路由；修复 Gmail 连接乐观锁因 DuckDB 微秒时间戳经 Date 归一化丢失精度导致的 CAS 失配；126 个测试套件、累计 274 项测试全部通过 |
| 2026-09-02 | `npm run check` | 通过 | 新增设备配对与令牌签发路由集成测试 1 项：配对码创建/一次性兑换（Ed25519 SPKI 公钥）、配对签发的访问令牌通过业务路由门禁、刷新轮换、旧刷新凭据重放触发同族全代次撤销；新增 `device-pairing-repository`、`DevicePairingService` 与 `registerDeviceRoutes` 并接入 `main`（补齐业务路由凭据的合法签发来源，访问令牌记录随配对与轮换持久化）；127 个测试套件、累计 275 项测试全部通过 |
| 2026-09-02 | `flutter analyze` / `flutter test` | 通过 | 新增配对→汇总全流程 widget 测试（假 HTTP 适配器、内存凭据存储）与 summary 模型解析测试 2 项；`flutter analyze` 无问题、4 项 Flutter 测试全部通过 |
| 2026-09-02 | `LYRA_PORT=3144 LYRA_DATABASE_PATH=<临时文件> LYRA_AGENT_*=<隔离目录与路径> npm start` + loopback smoke | 通过 | 单服务入口完成 v1–v11 迁移并携带完整 Agent 装配监听本机端口；`GET /v1/agents`、`POST /v1/agent/sessions`、`DELETE /v1/agent/sessions/:id` 在缺失与无效设备令牌下均返回 401 稳定 `DEVICE_CREDENTIAL_INVALID`；SIGTERM 后进程干净退出 |
| 2026-09-05 | `npm run check` | 通过 | 账本只读路由表新增 `/v1/ledger/accounts` 并扩展集成测试断言（空账本权威语境与空行集合）；TypeScript strict 编译成功；127 个测试套件、累计 275 项测试全部通过 |
| 2026-09-05 | `flutter analyze` / `flutter test` | 通过 | 静态分析无问题；7 项 Flutter 测试全部通过：导入仓储账户列表读取与归档账户过滤、`ACCOUNT_MAPPING_REQUIRED` 错误模型保留候选 details、显式 `accountId` 预览与提交解析、导入面板 409→账户选择→重新预览→提交全流程、未配对提示；既有导航与会话流程测试无回归 |
| 2026-09-05 | `npm run check` | 通过 | 导入提交幂等键接线：新增 `mutation-idempotency-repository`、`ImportService.commitImport` 幂等编排与 `Idempotency-Key` 路由头；新增集成测试 1 项覆盖非法键、事务内失败释放、重放、键复用冲突与处理中冲突；TypeScript strict 编译成功；127 个测试套件、累计 276 项测试全部通过 |
| 2026-09-05 | `flutter analyze` / `flutter test` | 通过 | 静态分析无问题；7 项 Flutter 测试全部通过：提交请求携带 `commit-<importId>` 幂等键头（假适配器断言）、既有导入全流程与导航/会话测试无回归 |
| 2026-09-05 | `flutter analyze` / `flutter test` | 通过 | 静态分析无问题；10 项 Flutter 测试全部通过：Agent 数据层解析 `GET /v1/agents` 可用性（status/version/reason 闭域）、会话创建与快照投影、回合事件序与 payload、取消终态；Agent 页创建→提问→助手回复渲染全流程；未配对入口阻断；既有导航/会话/导入测试无回归 |
| 2026-09-05 | `flutter analyze` / `flutter test` | 通过 | 静态分析无问题；20 项 Flutter 测试全部通过：新增 9 项（帧解析五类归类、delta 累积/重复忽略/缺口与 snapshot_required 刷新/terminal 刷新、switch 解析与 409、双容器重启恢复与 404 清除）加 1 项切换 widget 全流程；既有 10 项无回归 |
| 2026-09-05 | `npm run check` / `flutter analyze` / `flutter test` | 通过 | 服务端 127 套件 277 项全部通过（新增工具调用公开列表路由集成测试）；App 静态分析无问题、21 项全部通过（新增 tool-calls 三态解析与工具调用卡片渲染 widget 测试） |
| 2026-09-05 | `flutter analyze` / `flutter test` | 通过 | 静态分析无问题；24 项 Flutter 测试全部通过：新增 3 项重连退避测试（意外断开自动重连带最新游标、close/dispose 静默、退避封顶与 subscribed 重置），既有 21 项无回归 |
| 2026-09-05 | `npm run check`（5 连跑） | 通过 | 服务端 277 项测试在 `--test-concurrency=2` 下 5 次连续运行全部通过；测试进程并发从默认全量降为 2，消除原生 DuckDB 模块在多测试进程并行加载时的偶发 SIGSEGV（旧提交 bcc0c00 同样复现，与业务代码无关），总耗时 8.2s 基本不变 |
| 2026-09-05 | `flutter analyze` / `flutter test` | 通过 | 静态分析无问题；26 项 Flutter 测试在服务端运行与停止两种环境下均全部通过：新增配对响应六字段与刷新响应四字段（真实服务端形状）解析回归 2 项；`AgentLiveEvents` ready 失败收口修复后，干净 HEAD 上即失败的既有 4 项编排测试恢复通过 |
| 2026-09-05 | Medium_Phone 模拟器 401 刷新链路复验 | 通过 | 访问令牌过期后 `GET /v1/ledger/summary` 401 → `POST /v1/devices/tokens` 200 → 原请求重放 200,App 总览保持已同步;修复前该路径因刷新响应解析崩溃中断重放 |
| 2026-09-05 | Medium_Phone 模拟器 Agent 会话创建 | 通过 | 空账本下 `POST /v1/agent/sessions` 创建 Claude 会话成功,会话页渲染空闲状态与账本版本 0 权威语境;未发送真实回合（按既有纪律不启动模型会话） |
| 2026-09-05 | `flutter analyze` / `flutter test` / `flutter build apk --debug` | 通过 | 白色主题重构:静态分析无问题,26 项测试全部通过,debug 构建成功;模拟器目视验收总览、持仓、Agent 会话三页(截图 `theme-20260905-*.png`) |
| 2026-09-05 | `flutter analyze` / `flutter test` / `flutter build apk --debug` | 通过 | Coinbase 风格 token 落地:静态分析无问题,26 项测试全部通过,debug 构建成功;模拟器目视验收总览与 Agent 会话页(截图 `theme-20260905-04/05-*.png`) |
| 2026-09-05 | `npm run check` | 通过 | 配对码消费顺序修复：`consumePairingChallenge` 哈希比较前置并新增回归测试；127 个测试套件、累计 278 项测试全部通过 |
| 2026-09-05 | `GRADLE_USER_HOME=/private/tmp/lyra-gradle-home-20260831 flutter build apk --debug` | 通过 | `file_selector_android` 钉回 0.5.1+9 后 Android 构建恢复（52.6s）；修复配对协议头与刷新解析后再次构建成功 |
| 2026-09-05 | Medium_Phone 模拟器端到端 smoke | 通过 | `com.lyrafinance.lyra_mobile` 安装启动无崩溃；配对真实服务端（127.0.0.1:3141 经 adb reverse）成功并显示空账本权威语境（账本版本 0、已同步）；导入面板经系统文件选择器上传 Schwab CSV，服务端正确返回 `ACCOUNT_MAPPING_REQUIRED` 且 App 显示稳定错误码；Agent 页三供应商入口渲染正常；401 刷新路径暴露的响应解析缺陷已修复并复验配对成功 |
| 2026-09-01 | `LYRA_PORT=3143 LYRA_DATABASE_PATH=<临时文件> npm start` + loopback smoke | 通过 | 单服务入口完成 v1–v11 迁移并监听本机端口；健康端点返回 200 与 schema 11；`GET /mcp` 明确 405；无凭据与未知凭据的 `tools/list` 均返回 401 稳定 JSON-RPC 错误；SIGTERM 后写队列关闭、进程干净退出 |

## 当前环境事项

| 事项 | 状态 | 后续动作 |
| --- | --- | --- |
| Node.js 22 | 可用 | 用于 TypeScript 服务端 |
| Flutter SDK | 可用 | Flutter 3.47.2 stable、Dart 3.13.2；Android 授权已接受 |
| Android 工具链 | 可用 | Android CLI 1.0.15498356、Android SDK 36.1.0、JDK 21；Medium_Phone 设备 smoke 已通过 |
| 外部网络代理 | 可用 | `127.0.0.1:7890` 监听正常；默认及显式代理 GitHub 探测均为 HTTP 200；受限沙箱内的 loopback 失败不代表代理离线 |
| 测试进程并发 | 已收敛 | 原生 DuckDB Node 模块在 `node --test` 默认全量并发下偶发 SIGSEGV（agent-events-ws 集成测试套件，旧提交同样复现）；`npm test` 已固定 `--test-concurrency=2`，5 连跑稳定 |
| file_selector_android 构建兼容性 | 已钉版、待上游 | 0.5.2+10 的 Kotlin DSL 构建脚本在 Flutter 3.47.2（AGP 9.1.0 / Gradle 9.3.1）下 `kotlin{}`/`android{}` 访问器无法解析,全新模板项目同样复现,与项目配置无关;`pubspec.yaml` dependency_overrides 钉回 0.5.1+9(groovy 构建,平台接口不变),上游发布修复后移除钉版 |
| Flutter 3.47.2 模板 Gradle 标志 | 保持默认 | `android.newDsl=false` 与 `android.builtInKotlin=false` 必须同时保留:单独开启 builtInKotlin 会让 `:app` 的 Kotlin DSL 访问器失效,开启 newDsl 会让 flutter-gradle-plugin 转型 `AbstractAppExtension` 失败 |
| Gradle 全局镜像 | 需隔离 | `/Users/Gin/.gradle/init.d/aliyun-mirror.gradle.kts` 会清空 Flutter 官方 Maven 仓库；Flutter 构建使用独立 `GRADLE_USER_HOME`，不修改用户全局配置 |
| 服务端外部依赖 | TypeScript、Node 类型、Decimal、Fastify、Zod、DuckDB Node API、csv-parse、ExcelJS、mailparser 与 genkit / @genkit-ai/compat-oai / google-auth-library 已安装 | MCP SDK、Claude/Pi/OpenCode CLI 相关依赖已随本次迁移移除；Pi 扩展 SDK 不再计划引入，可按后续功能点逐项评估并验证 |
| ExcelJS 上游审计项 | 已评估、待上游修复 | uuid advisory 不命中 Lyra 的 XLSX 只读路径；保留 ExcelJS 4.4.0 精确锁定并跟踪升级，不用 `npm audit fix --force` 降级主依赖 |
