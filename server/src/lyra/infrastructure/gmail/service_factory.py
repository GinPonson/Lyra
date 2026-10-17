"""从数据库连接与环境中装配 Gmail 服务。

把仓储、加密器、OAuth 客户端、Gmail 客户端与同步编排接到一起，供 ``main.py` 使用。

**未配置 OAuth 时返回一个 `configured == False` 的服务而不是 `None`**：路由需要它来
返回 503，而"没有服务"与"服务存在但未配置"对客户端是同一件事。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import duckdb

from ...application.gmail_account_mapping import (
    AccountRecord,
    plan_gmail_account_mapping_update,
    resolve_gmail_account_mapping,
)
from ...application.gmail_service import (
    GmailService,
    load_gmail_google_oauth_config,
)
from ...application.gmail_sync_service import (
    GmailSyncDependencies,
)
from ...application.gmail_user_scope import assert_gmail_user_active
from ...infrastructure.db.gmail_connection_repository import (
    create_gmail_account_mapping_row,
    create_gmail_connection_row,
    load_gmail_account_mapping,
    load_gmail_account_mappings,
    load_gmail_connection,
    load_gmail_connections,
    save_gmail_connection_row,
    update_gmail_account_mapping_row,
)
from ...security.secret_cipher import SecretCipher
from .full_message import parse_gmail_full_input
from .google_gmail_client import (
    GMAIL_PROFILE_ENDPOINT,
    fetch_incremental_gmail_full_records,
    fetch_incremental_gmail_message_ids,
)
from .google_oauth_client import (
    exchange_google_authorization_code,
    revoke_gmail_connection_authorization,
)
from .http_transport import build_form_getter, build_json_getter

# 单次同步的有界扫描/增量上限。
#
# 值必须存在且为正：Gmail 查询不带 ``maxResults`` 会把邮箱全部历史拉下来（既慢又贵），
# 而 ``search_query`` 会拒绝非正数。当前没有对应的配置字段（``config.py`` 不在本次
# 改造范围），因此先固定为模块常量；需要按部署调整时应由主代理在 ``config.py`` 增加
# 设置项并从这里读取，而不是把数字散落到调用点。
DEFAULT_GMAIL_MAXIMUM_MESSAGES = 500


def build_gmail_service(
    *, connection: duckdb.DuckDBPyConnection, environment: object, cipher: SecretCipher
) -> GmailService:
    """装配 Gmail 服务。

    **返回值是未绑定用户的服务**：路由必须经 ``service.for_user(user_id)`` 取得绑定
    当前用户的视图后再调用。未绑定视图在真实依赖上调用业务方法会因缺少 ``user_id``
    直接报错，不会退回全库语义。

    @param connection: 数据库连接。
    @param environment: 环境变量映射。
    @param cipher: 凭据加密器。
    @returns 服务实例。
    """
    post_form = build_form_getter()
    get_json = build_json_getter()
    now = lambda: datetime.now(UTC)

    deps = _Dependencies(
        connection=connection, cipher=cipher, now=now,
        post_form=post_form, get_json=get_json,
    )
    return GmailService(
        dependencies=deps, oauth=load_gmail_google_oauth_config(environment)
    )


class _Dependencies:
    """服务所需外部动作的真实实现。

    **每个与数据库相关的方法首参都是 ``user_id``**，由
    :class:`lyra.application.gmail_service._UserScopedGmailDependencies` 注入；
    这样仓储调用不可能漏掉用户范围。
    """

    def __init__(self, *, connection, cipher, now, post_form, get_json) -> None:
        self._connection = connection
        self.cipher = cipher
        self._now = now
        self._post_form = post_form
        self._get_json = get_json

    def now(self) -> datetime:
        """服务端可信时间。"""
        return self._now()

    def new_id(self) -> str:
        """新连接 ID。"""
        return str(uuid.uuid4())

    def exchange_code(self, **kwargs) -> dict:
        """用授权码换取凭据与身份。"""
        return exchange_google_authorization_code(
            post_form=self._post_form, get_userinfo=self._get_json,
            now=self.now(), **kwargs,
        )

    def create_connection(self, user_id: str, record) -> None:
        """插入属于该用户的连接行。"""
        create_gmail_connection_row(self._connection, user_id, record)

    def list_connections(self, user_id: str) -> tuple:
        """列出该用户的连接。"""
        return load_gmail_connections(self._connection, user_id)

    def load_connection(self, user_id: str, connection_id: str):
        """读取一条属于该用户的连接。"""
        return load_gmail_connection(self._connection, user_id, connection_id)

    def save_connection(
        self, user_id: str, record, *, expected_updated_at: str, **overrides
    ) -> None:
        """保存该用户的连接状态（带 CAS）。"""
        save_gmail_connection_row(
            self._connection, user_id, _with_overrides(record, overrides),
            expected_updated_at=expected_updated_at,
        )

    def list_mappings(self, user_id: str, connection_id: str) -> tuple:
        """列出该用户某连接的映射。"""
        return load_gmail_account_mappings(self._connection, user_id, connection_id)

    def load_mapping(self, user_id: str, mapping_id: str):
        """读取一条属于该用户的映射。"""
        return load_gmail_account_mapping(self._connection, user_id, mapping_id)

    def list_accounts(self, user_id: str) -> tuple:
        """列出该用户的账本账户。"""
        rows = self._connection.execute(
            """
            SELECT CAST(id AS VARCHAR), legacy_account_key, masked_suffix, status
            FROM accounts WHERE user_id = CAST(? AS UUID)
            ORDER BY masked_suffix, id
            """,
            [user_id],
        ).fetchall()
        return tuple(
            AccountRecord(
                id=row[0], legacy_account_key=row[1], masked_suffix=row[2],
                status=(row[3] or "active"),
            )
            for row in rows
        )

    def plan_mapping_update(self, **kwargs):
        """规划一次映射改绑。"""
        return plan_gmail_account_mapping_update(**kwargs)

    def update_mapping(
        self, user_id: str, *, mapping_id, expected_account_id, next_account_id, at
    ) -> None:
        """执行改绑（旧账户 ID 做 CAS），并确认目标账户属于该用户。"""
        update_gmail_account_mapping_row(
            self._connection, user_id, mapping_id=mapping_id,
            expected_account_id=expected_account_id,
            next_account_id=next_account_id, at=at,
        )

    def revoke(self, connection) -> None:
        """向 Google 撤销授权。"""
        revoke_gmail_connection_authorization(
            connection, self.cipher, post_form=self._post_form
        )

    def build_sync_dependencies(self, user_id: str) -> GmailSyncDependencies:
        """构造绑定该用户的同步依赖集合。

        同步依赖里的每一次数据库读写都带 ``user_id``：连接读取、账户候选、映射读写、
        账本版本与消息归档。因此"同步携带用户"不是靠某个参数传到最深处，而是**依赖
        集合本身属于一个用户**。

        @param user_id: 当前用户的 UUID 文本。
        @returns 同步编排所需的动作集合。
        """
        return build_gmail_sync_dependencies(
            connection=self._connection, cipher=self.cipher,
            maximum_messages=DEFAULT_GMAIL_MAXIMUM_MESSAGES, user_id=user_id,
        )


def _with_overrides(record, overrides: dict):
    """按需覆盖连接记录字段。"""
    from dataclasses import replace

    if not overrides:
        return record
    return replace(record, **overrides)


class _SyncDependencies:
    """同步编排所需外部动作的真实实现。

    **有界扫描必须先取 profile。** profile 给出本次扫描起点的 history 基线；
    不先取它，扫描期间新增的消息就没有游标可以覆盖，会永久丢失。

    **整个依赖集合属于一个用户**（``user_id``）：连接、账户、映射、账本版本与消息
    归档的每一次读写都带用户范围，"同步携带 user_id"因此不依赖调用点自觉传参。

    @param connection: 数据库连接。
    @param cipher: 凭据加密器。
    @param maximum_messages: 单次同步的消息上限。
    @param user_id: 当前用户的 UUID 文本；**必填**。
    """

    def __init__(
        self, *, connection, cipher, maximum_messages, user_id
    ) -> None:
        self._connection = connection
        self._cipher = cipher
        self._maximum_messages = maximum_messages
        self._user_id = user_id
        self._post_form = build_form_getter()
        self._get_json = build_json_getter()
        # 由 ``load_connection`` 填入：有界扫描要用它校验 profile 属于同一个邮箱。
        self._expected_email: str | None = None

    def now(self):
        """服务端可信时间。"""
        return datetime.now(UTC)

    def load_connection(self, connection_id: str):
        """读取**当前用户的**连接，并确认用户仍然活动。

        **活动状态在这里查**：``sync_gmail_connection`` 的第一件事就是读取连接，
        因此停用一旦提交，本次同步在拉取任何消息之前就被拒绝（403），既不会继续调用
        Google，也不会写回状态（计划 §4.4：排队任务实际开始执行时必须拒绝）。

        @raises ApplicationError: 用户已停用（403）。
        """
        assert_gmail_user_active(self._connection, self._user_id)
        row = load_gmail_connection(self._connection, self._user_id, connection_id)
        if row is not None:
            # 有界扫描的 profile 校验基准；读不到连接时同步层会立刻报 404，
            # 因此这里不会用空基准去跳过邮箱校验。
            self._expected_email = row.google_email
        return row

    def save_connection(self, record, *, expected_updated_at: str, **overrides) -> None:
        """保存当前用户的连接状态（带 CAS）。

        **写回前再确认一次活动状态**：长流程期间被停用后，同步不得把游标或状态写回
        （"不得继续拉取或写回消息"）。
        """
        assert_gmail_user_active(self._connection, self._user_id)
        save_gmail_connection_row(
            self._connection, self._user_id, _with_overrides(record, overrides),
            expected_updated_at=expected_updated_at,
        )

    def refresh_access_token(self, connection) -> str:
        """刷新 access token。"""
        from ...application.gmail_sync_errors import refresh_with_connection

        # 刷新令牌是同步里第一次触达 Google 的动作：停用后不得发出。
        assert_gmail_user_active(self._connection, self._user_id)
        return refresh_with_connection(
            connection, cipher=self._cipher, post_form=self._post_form, now=self.now
        )

    def fetch_incremental(self, access_token, start_history_id, maximum):
        """拉取增量消息。"""
        assert_gmail_user_active(self._connection, self._user_id)
        return fetch_incremental_gmail_full_records_for(
            access_token, start_history_id, maximum, get_json=self._get_json
        )

    def fetch_initial(self, access_token, after_date, maximum):
        """执行有界扫描。

        **profile 先于消息列表**：它给出本次扫描的 history 基线，扫描期间新增的消息
        才能被后续增量覆盖。

        **profile 必须与本次连接的邮箱一致**：``load_connection`` 已经填好基准；
        基准缺失说明调用顺序被改坏了（同步层会先读连接），此时**明确失败**而不是
        跳过校验——否则一个错配的 access token 能把别人邮箱的邮件写进本次连接。
        """
        # **路径必须是同包的 ``.search_query``**：原先是 ``...domain.gmail.search_query``，
        # 而 ``build_schwab_econfirm_search`` 一直只存在于 ``infrastructure/gmail/``。
        # 那行在 ``GmailService.sync`` 实现之前是死代码（同步根本没有生产入口），
        # 一旦首次同步可走，它就会抛 ``ModuleNotFoundError`` → 500。
        from .search_query import build_schwab_econfirm_search
        from .google_gmail_client import (
            fetch_bounded_gmail_message_ids,
            fetch_full_gmail_records,
            parse_gmail_profile,
            request_gmail_json,
        )

        assert_gmail_user_active(self._connection, self._user_id)
        if self._expected_email is None:
            raise RuntimeError("Gmail 有界扫描缺少连接邮箱基准，必须先读取连接")
        profile = parse_gmail_profile(
            request_gmail_json(
                GMAIL_PROFILE_ENDPOINT, access_token, get_json=self._get_json
            ),
            expected_email=self._expected_email,
        )
        plan = build_schwab_econfirm_search(after_date, maximum)
        bounded = fetch_bounded_gmail_message_ids(
            access_token, plan, get_json=self._get_json
        )
        if bounded.truncated:
            return _TruncatedRecords()
        return _ReadyRecords(
            records=fetch_full_gmail_records(
                bounded.message_ids, access_token, get_json=self._get_json
            ),
            completion_history_id=profile.history_id,
        )

    def process_messages(self, connection, records, explicit_account_id):
        """处理一批消息。"""
        from ...application.gmail_message_processing import (
            GmailMessageProcessingDependencies,
            process_gmail_messages,
        )

        deps = GmailMessageProcessingDependencies(
            parse_records=lambda record: parse_gmail_full_input(record, source_name="Gmail 同步"),
            # 账户候选与映射都限定在当前用户内：跨用户账户不可能被解析成映射目标，
            # 因此"跨归属映射"在解析阶段就无从产生，写入阶段还有仓储层的原子校验。
            load_accounts=self._load_accounts,
            load_mappings=lambda: load_gmail_account_mappings(
                self._connection, self._user_id, connection.id
            ),
            resolve_mapping=lambda **kwargs: resolve_gmail_account_mapping(
                connection_id=connection.id, **kwargs
            ),
            create_mapping=lambda plan: create_gmail_account_mapping_row(
                self._connection, self._user_id,
                connection_id=plan.connection_id,
                account_suffix=plan.account_suffix, account_id=plan.account_id,
                at=self.now(),
            ),
            persist_unsuccessful=self._persist_unsuccessful(connection),
            import_message=self._import_message(connection),
        )
        summary = process_gmail_messages(
            records, dependencies=deps, explicit_account_id=explicit_account_id
        )
        return summary.to_dict()

    def _load_accounts(self) -> tuple:
        """读取**当前用户的**账本账户。"""
        rows = self._connection.execute(
            """
            SELECT CAST(id AS VARCHAR), legacy_account_key, masked_suffix, status
            FROM accounts WHERE user_id = CAST(? AS UUID)
            ORDER BY masked_suffix, id
            """,
            [self._user_id],
        ).fetchall()
        return tuple(
            AccountRecord(
                id=row[0], legacy_account_key=row[1], masked_suffix=row[2],
                status=(row[3] or "active"),
            )
            for row in rows
        )

    def _persist_unsuccessful(self, connection):
        """构造失败归档动作。"""
        from ...infrastructure.gmail.message_metadata import (
            create_unsuccessful_gmail_message_metadata,
        )
        from ..db.gmail_message_repository import persist_unsuccessful_gmail_message

        def persist(message, error_code):
            metadata = create_unsuccessful_gmail_message_metadata(
                connection_id=connection.id, thread_id=None, message=message,
                account_id=None, history_id=connection.history_id,
                synced_at=self.now(), error_code=error_code,
            )
            persist_unsuccessful_gmail_message(
                self._connection, self._user_id, metadata,
                synced_at=_iso(self.now()),
            )

        return persist

    def _import_message(self, connection):
        """构造单封导入动作。

        **先读账本版本再建计划**：计划绑定读取时的账本版本，提交时用它做 CAS——
        两次同步交错时后到的一方会收到版本冲突而不是把基于旧快照的计划写进去。
        """
        from ..db.gmail_message_repository import (
            load_existing_gmail_message_state,
            persist_gmail_message_import,
        )
        from .message_import_plan import build_gmail_message_import_plan


        def import_message(message, trades, mapping):
            ledger_version = _current_ledger_version(self._connection, self._user_id)
            existing = load_existing_gmail_message_state(
                self._connection, self._user_id, connection.id, message.message_id
            )
            plan = build_gmail_message_import_plan(
                connection_id=connection.id, thread_id=None, message=message,
                trades=trades, account_id=mapping.account_id,
                ledger_account=mapping.legacy_account_key,
                history_id=connection.history_id, synced_at=self.now(),
                parser_version=_parser_version(),
                base_ledger_version=ledger_version,
                existing_message=existing,
                existing_account_transactions=_account_transactions(
                    self._connection, self._user_id, mapping.legacy_account_key
                ),
            )
            if plan.kind == "already_imported":
                return "already_imported"
            persist_gmail_message_import(
                self._connection, user_id=self._user_id, plan=plan,
                expected_ledger_version=ledger_version,
                synced_at=_iso(self.now()),
            )
            return "imported"

        return import_message


def _request_json(get_json, url: str, access_token: str) -> dict:
    """发起 Gmail JSON 请求并做统一错误映射。"""
    from .google_gmail_client import request_gmail_json

    return request_gmail_json(url, access_token, get_json=get_json)


def _iso(value: datetime) -> str:
    """渲染为与 JavaScript ``toISOString()`` 同形的文本。"""
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_gmail_sync_dependencies(
    *,
    connection: duckdb.DuckDBPyConnection,
    cipher: SecretCipher,
    maximum_messages: int,
    user_id: str,
) -> GmailSyncDependencies:
    """装配同步编排所需的动作集合。

    @param connection: 数据库连接。
    @param cipher: 凭据加密器。
    @param maximum_messages: 单次同步的消息上限。
    @param user_id: 当前用户的 UUID 文本；**必填**，所有回调都限定在该用户内。
    @returns 依赖集合。
    """
    deps = _SyncDependencies(
        connection=connection, cipher=cipher,
        maximum_messages=maximum_messages, user_id=user_id,
    )
    return GmailSyncDependencies(
        load_connection=deps.load_connection,
        save_connection=deps.save_connection,
        refresh_access_token=deps.refresh_access_token,
        fetch_incremental=deps.fetch_incremental,
        fetch_initial=deps.fetch_initial,
        process_messages=deps.process_messages,
        now=lambda: _iso(deps.now()),
        maximum_messages=maximum_messages,
    )


def fetch_incremental_gmail_full_records_for(
    access_token: str, start_history_id: str, maximum: int, *, get_json
) -> object:
    """拉取增量消息并返回完整记录。

    先按 history 拿到新增 ID，再用元数据过滤，最后拉正文——三步都在已有的客户端函数里。
    """
    bounded = fetch_incremental_gmail_message_ids(
        access_token, start_history_id=start_history_id,
        maximum_messages=maximum, get_json=get_json,
    )
    fetched = fetch_incremental_gmail_full_records(
        bounded.message_ids, access_token, get_json=get_json
    )
    return _IncrementalResult(
        records=fetched.records,
        observed_history_id=bounded.observed_history_id,
        truncated=bounded.truncated,
        ignored_message_count=fetched.ignored_message_count,
    )


class _IncrementalResult:
    """增量结果。"""

    def __init__(self, *, records, observed_history_id, truncated, ignored_message_count):
        self.records = records
        self.observed_history_id = observed_history_id
        self.truncated = truncated
        self.ignored_message_count = ignored_message_count


class _TruncatedRecords:
    """被截断的有界扫描结果。"""

    kind = "truncated"
    records: tuple = ()
    completion_history_id: str | None = None
    ignored_message_count = 0


class _ReadyRecords:
    """就绪的有界扫描结果。"""

    kind = "ready"
    ignored_message_count = 0

    def __init__(self, *, records, completion_history_id: str) -> None:
        self.records = records
        # 扫描前读到的 history 基线；扫描完成后从它开始增量。
        self.completion_history_id = completion_history_id


def _current_ledger_version(connection: duckdb.DuckDBPyConnection, user_id: str) -> int:
    """读取**当前用户**的账本版本；该用户的空账本为 0。

    契约 §3.3 把「用户当前账本版本」显式定义为 ``max(version) WHERE user_id = ?``，
    ``ledger_version_seq`` 仍是全局序列（号码不连续）。**禁止**改回全局
    ``max(version)``：那会让 B 的导入把 A 的基准版本推高，A 拿着旧版本提交时被判
    ``LEDGER_VERSION_CONFLICT``——A 的同步被 B 的写入"隔空"作废。
    """
    row = connection.execute(
        "SELECT coalesce(max(version), 0) FROM ledger_versions WHERE user_id = CAST(? AS UUID)",
        [user_id],
    ).fetchone()
    return 0 if row is None else int(row[0])


def _parser_version() -> str:
    """解析器版本。

    与 CSV 导入共用同一个常量——**规则变化后旧预览必须失效**，两条路径的版本号必须
    一起变，否则邮件导入的预览不会随规则更新而失效。
    """
    from ...domain.ledger.parsers import PARSER_VERSION

    return PARSER_VERSION


def _account_transactions(
    connection: duckdb.DuckDBPyConnection, user_id: str, legacy_key: str
) -> tuple:
    """读取目标账户的现有标准交易。

    传给导入计划做去重与 FIFO 重算的依据。

    **复用已有的账本快照读取**，而不是另写一份查询——两份查询在字段或排序上稍有差异
    就会让导入计划基于不同的交易集合做判断。注意分析层的 ``AccountScope.account``
    用的是**legacy 账户键**（不是 UUID）。

    仓储在构造时绑定 ``user_id``：账户键已改为用户内唯一，不带用户范围的快照会把
    另一个用户的同键账户交易混进本次去重与 FIFO。
    """
    from ...domain.analytics.types import AccountScope
    from ..db.ledger_repository import DuckDbLedgerRepository

    snapshot = DuckDbLedgerRepository(connection, user_id=user_id).load_snapshot(
        AccountScope(kind="account", account=legacy_key)
    )
    # 快照按范围过滤，但显式再筛一次：仓储若过滤失效，多出的交易会污染去重与 FIFO。
    return tuple(
        transaction for transaction in snapshot.transactions
        if transaction.account == legacy_key
    )
