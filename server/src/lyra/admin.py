"""离线初始化与旧库归属升级入口（P1b）。

本模块是**一次性离线操作**的命令行入口，不是运行时配置：

* 新安装：显式给出首个管理员的 ``(provider, subject)``，创建固定 ``role='admin'`` 的用户；
* 旧库升级：显式确认哪些历史登录身份属于原 owner，把 schema 15 的全部资源归给该用户。

**为什么用命令行参数而不是环境变量**（契约 §3.10）：环境变量会让一个只该用一次的授权
长期驻留在部署配置里，容易被误认成运行时鉴权开关，也会让「谁是管理员」变成启动时可变的
输入。``config.py`` 因此没有新增任何字段。

**两条硬约束**（计划 §9 末段）：不打印任何 token；不把「首个请求」当作管理员凭据——管理员
身份只能来自本次命令行的显式参数。

**运行前提：服务必须已停止。** DuckDB 是单进程文件锁，服务在跑时本入口会以
``Could not set lock on file`` 失败。容器部署的正确姿势是
``compose stop`` → ``compose run --rm --no-deps lyra python -m lyra.admin …`` →
``compose start``（见 ``docs/deployment.md``）。

用法::

    # 新部署第一步：签发邀请码（不需要管理员已存在）
    python -m lyra.admin --database /data/lyra.duckdb --create-invite-code
    # 首个注册用户提升为管理员
    python -m lyra.admin --database /data/lyra.duckdb --promote-user <userId>
    # 或者离线直接登记一个管理员身份
    python -m lyra.admin --database /data/lyra.duckdb --provider google --subject 112233
    python -m lyra.admin --database /data/lyra.duckdb \
        --owner-identity google:112233 --provider google --subject 112233

退出码：``0`` 成功，``1`` 输入或目标库状态不允许继续（原因写到 stderr）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
from pydantic import ValidationError

from .application.app_error import ApplicationError
from .config import Settings
from .db.connection import LyraDatabase, _configure_temp_directory, open_lyra_database
from .db.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationContext,
    MigrationError,
    MigrationOwnerIdentity,
    apply_migrations,
)
from .infrastructure.auth.login_provider import LOGIN_PROVIDER_IDS, LoginIdentity
from .infrastructure.db.invite_code_repository import InviteCodeRepository
from .infrastructure.db.user_repository import UserRepository

# 旧运行时记录「部署唯一 owner 曾用哪些身份登录过」的表。schema 16 起归属落在
# users / user_identities 上，它只剩核对价值（见 _unconfirmed_legacy_identities）。
_LEGACY_OWNER_IDENTITY_TABLE = "server_owner_identity"


class AdminError(RuntimeError):
    """离线入口的输入或目标库状态不允许继续执行。"""


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：解析参数、执行初始化或升级，并返回进程退出码。

    @param argv: 命令行参数；``None`` 时取 ``sys.argv[1:]``。
    @returns 成功为 ``0``，明确失败为 ``1``。
    """
    options = _parse_arguments(argv)
    try:
        report = _run(
            database=options.database,
            provider=options.provider,
            subject=options.subject,
            owner_identities=tuple(options.owner_identity),
            display_name=options.display_name,
            create_invite_code=options.create_invite_code,
            invite_label=options.invite_label,
            promote_user=options.promote_user,
        )
    except AdminError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    except MigrationError as error:
        # 迁移失败（含「旧库缺显式归属」）本来就是操作者可读的明确失败，不该以 traceback 呈现。
        print(f"迁移失败: {error}", file=sys.stderr)
        return 1
    except ApplicationError as error:
        print(f"错误: [{error.code}] {error.message}", file=sys.stderr)
        if error.details:
            print(f"详情: {error.details}", file=sys.stderr)
        return 1
    except duckdb.Error as error:
        # 迁移自身的异常已经由 apply_migrations 回滚并释放连接，这里只是把底层数据库错误
        # 变成操作者可读的失败（离线升级常在大库上跑，traceback 会淹没真正的原因）。
        print(f"数据库错误: {error}", file=sys.stderr)
        return 1
    print(report)
    return 0


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析冻结的 CLI 参数集合（契约 §3.10）。

    ``--provider`` / ``--subject`` 不在 argparse 层做 ``required``：它们在旧库升级时可以
    缺省（那时代替为「既有设备的身份来源」），必填性由 :func:`_run` 按路径判定。

    @param argv: 命令行参数；``None`` 时取 ``sys.argv[1:]``。
    @returns 解析结果。
    """
    parser = argparse.ArgumentParser(
        prog="python -m lyra.admin",
        description="Lyra 离线初始化与旧库归属升级入口（一次性操作，不是运行时配置）",
    )
    parser.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help="目标 DuckDB 路径；缺省取 LYRA_DATABASE_PATH（Settings.lyra_database_path）",
    )
    parser.add_argument(
        "--provider",
        default=None,
        metavar="PROVIDER",
        help="首个管理员的登录来源：google / linuxdo / github；旧库升级时表示既有设备的身份来源",
    )
    parser.add_argument(
        "--subject",
        default=None,
        metavar="SUBJECT",
        help="首个管理员在该来源下已验证的不可变主体标识",
    )
    parser.add_argument(
        "--owner-identity",
        action="append",
        default=[],
        metavar="PROVIDER:SUBJECT",
        help="可重复；旧库升级时显式确认归属于原 owner 的身份。未列出的历史身份不会关联原用户",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        metavar="NAME",
        help="可选显示名；旧库升级时作为原用户的显示名",
    )
    parser.add_argument(
        "--create-invite-code",
        action="store_true",
        help=(
            "签发一枚邀请码并打印（只打印一次）。不需要任何管理员已存在——"
            "这正是新部署的第一步：先拿码注册，再由 --promote-user 提升为管理员"
        ),
    )
    parser.add_argument(
        "--invite-label",
        default=None,
        metavar="LABEL",
        help="邀请码备注（发给谁），仅作运维记录",
    )
    parser.add_argument(
        "--promote-user",
        default=None,
        metavar="USER_ID",
        help="把已注册用户提升为管理员（停用/恢复仍走管理 API）",
    )
    return parser.parse_args(argv)


def _run(
    *,
    database: str | None,
    provider: str | None,
    subject: str | None,
    owner_identities: tuple[str, ...],
    display_name: str | None,
    create_invite_code: bool = False,
    invite_label: str | None = None,
    promote_user: str | None = None,
) -> str:
    """按「是否给出 ``--owner-identity``」分流到旧库升级或新安装。

    @param database: ``--database``；``None`` 时取环境配置里的账本路径。
    @param provider: ``--provider``。
    @param subject: ``--subject``。
    @param owner_identities: ``--owner-identity`` 的原始取值。
    @param display_name: ``--display-name``。
    @param create_invite_code: ``--create-invite-code``。
    @param invite_label: ``--invite-label``。
    @param promote_user: ``--promote-user``。
    @returns 供操作者核对的结果摘要；**不含任何凭据**（邀请码是唯一例外：
        它必须交给操作者，且只出现这一次）。
    @raises AdminError: 参数组合非法、目标库状态不允许本次操作。
    @raises MigrationError: 迁移失败。
    @raises ApplicationError: 仓储层明确失败（如目标身份已有用户）。
    """
    database_path = _resolve_database_path(database)
    if create_invite_code:
        return _issue_invite_code(database_path, invite_label)
    if promote_user is not None:
        return _promote_user(database_path, promote_user)
    confirmed_identities = _parse_owner_identities(owner_identities)
    if confirmed_identities:
        return _upgrade_legacy_database(
            database_path, confirmed_identities, provider, subject, display_name
        )
    return _create_first_admin(database_path, provider, subject, display_name)


def _issue_invite_code(database_path: str, label: str | None) -> str:
    """签发一枚邀请码并返回给操作者。

    **这是新部署的第一步**：邀请码是新身份注册的唯一入口，而管理员只能由已存在的
    用户提升——两者互相等待就永远起不来。因此这里允许 ``created_by=None``（表示
    这枚码来自服务器终端而不是某个管理员账号），可审计且不需要先有人。

    @param database_path: 目标库路径。
    @param label: 备注。
    @returns 操作者可见的摘要，**含明文邀请码（只此一次）**。
    @raises AdminError: 目标库尚未迁移到含邀请码的版本。
    """
    database = open_lyra_database(database_path)
    try:
        invites = InviteCodeRepository(database.connection)
        try:
            record, code = invites.create(created_by=None, label=label, now=datetime.now(UTC))
        except duckdb.CatalogException as error:
            raise AdminError(
                "目标库还没有 invite_codes 表：请先用当前版本启动一次服务完成迁移"
            ) from error
        return (
            f"已签发邀请码（只显示这一次，请立即保存）：\n\n    {code}\n\n"
            f"id: {record.id}\n备注: {record.label or '（无）'}\n"
            "下一步：在 App 登录页填入它完成首次注册，然后用 --promote-user <userId> 提升为管理员。"
        )
    finally:
        database.close()


def _promote_user(database_path: str, user_id: str) -> str:
    """把已注册用户提升为管理员。

    角色只影响用户管理与邀请码签发，**不改变任何账本可见范围**——管理员也只能读
    自己的账本（契约 §4.1）。

    @param database_path: 目标库路径。
    @param user_id: 用户 UUID。
    @returns 操作者可见的摘要。
    @raises AdminError: id 非法或用户不存在。
    """
    database = open_lyra_database(database_path)
    try:
        users = UserRepository(database.connection)
        try:
            promoted = users.promote_to_admin(user_id, now=datetime.now(UTC))
        except ApplicationError as error:
            raise AdminError(f"{error.code}: {error.message}") from error
        return f"已提升为管理员：{promoted.id}（{promoted.display_name}）"
    finally:
        database.close()


def _resolve_database_path(explicit: str | None) -> str:
    """解析目标库路径：显式参数优先，否则取既有的 ``Settings.lyra_database_path``。

    ``Settings`` 只在缺省时才实例化：显式给出 ``--database`` 时不该因为部署环境缺少与之
    无关的必填变量而失败。

    @param explicit: ``--database`` 的值；``None`` 表示未提供。
    @returns 非空的目标库路径。
    @raises AdminError: 显式路径为空白，或环境里没有可用的默认路径。
    """
    if explicit is not None:
        path = explicit.strip()
        if not path:
            raise AdminError("--database 不能是空白路径")
        return path
    try:
        return Settings().lyra_database_path
    except ValidationError as error:
        # 不把 pydantic 的原始报错交给操作者：它的 model 级信息里会带上整个环境输入，
        # 其中包含主密钥等敏感值。这里只说明「缺什么、怎么补」。
        raise AdminError(
            "未提供 --database，且环境变量不足以取默认账本路径（需要 LYRA_DATABASE_PATH 等必填项）；"
            "请显式传 --database 或补全部署环境"
        ) from error


def _parse_owner_identities(raw_values: Sequence[str]) -> tuple[MigrationOwnerIdentity, ...]:
    """把 ``--owner-identity provider:subject`` 解析成迁移上下文里的身份。

    这里只做**输入格式**校验（已知来源、主体非空）：重复身份、主体长度等约束由迁移自己的
    校验负责——复制那套规则会让两处悄悄漂移，而缺少归属时本该由 ``migrations.py`` 明确失败。

    @param raw_values: ``--owner-identity`` 的原始取值，按出现顺序。
    @returns 解析后的身份；``is_device_identity`` 一律为 ``False``，由 :func:`_mark_device_identity`
        单独指定。
    @raises AdminError: 缺少分隔符、来源未知或主体为空。
    """
    identities: list[MigrationOwnerIdentity] = []
    for raw in raw_values:
        provider, separator, subject = raw.partition(":")
        provider = provider.strip()
        subject = subject.strip()
        if not separator or not provider or not subject:
            raise AdminError(f"--owner-identity 必须是 provider:subject 形式，收到 {raw!r}")
        if provider not in LOGIN_PROVIDER_IDS:
            available = " / ".join(LOGIN_PROVIDER_IDS)
            raise AdminError(f"--owner-identity 的来源未知: {provider!r}；可用来源: {available}")
        identities.append(MigrationOwnerIdentity(provider=provider, subject=subject))
    return tuple(identities)


def _require_provider_and_subject(
    provider: str | None, subject: str | None
) -> tuple[str, str]:
    """要求 ``--provider`` 与 ``--subject`` 成对给出，并校验取值。

    @param provider: ``--provider`` 的原始取值。
    @param subject: ``--subject`` 的原始取值。
    @returns 去除外围空白后的 ``(provider, subject)``。
    @raises AdminError: 只给了一个、来源未知或主体为空。
    """
    if provider is None and subject is None:
        raise AdminError("需要同时提供 --provider 与 --subject 来指定管理员身份")
    if provider is None or subject is None:
        raise AdminError("--provider 与 --subject 必须同时提供，不能只给其中一个")
    normalized_provider = provider.strip()
    normalized_subject = subject.strip()
    if normalized_provider not in LOGIN_PROVIDER_IDS:
        available = " / ".join(LOGIN_PROVIDER_IDS)
        raise AdminError(f"--provider 的来源未知: {normalized_provider!r}；可用来源: {available}")
    if not normalized_subject:
        raise AdminError("--subject 不能为空")
    return normalized_provider, normalized_subject


def _login_identity(
    provider: str, subject: str, display_name: str | None
) -> LoginIdentity:
    """构造登录身份；邮箱留空，身份键始终是 ``(provider, subject)``。

    @param provider: 已校验的来源标识。
    @param subject: 已校验的主体标识。
    @param display_name: 可选显示名；``None`` 时由仓储按 subject 回退。
    @returns 登录身份。
    """
    return LoginIdentity(
        provider=provider,  # type: ignore[arg-type] - 上游已按 LOGIN_PROVIDER_IDS 校验
        subject=subject,
        email=None,
        display_name=display_name,
    )


def _create_first_admin(
    database_path: str,
    provider: str | None,
    subject: str | None,
    display_name: str | None,
) -> str:
    """新安装路径：创建固定 ``role='admin'`` 的首个用户与身份。

    这里刻意**不**为旧库提供兜底：``open_lyra_database`` 在没有迁移上下文时会明确拒绝非空
    旧库（计划 §9.3），因此误把旧库当新安装跑不会把数据留给一个凭空造出的管理员，而是失败。

    @param database_path: 目标库路径。
    @param provider: ``--provider``。
    @param subject: ``--subject``。
    @param display_name: ``--display-name``。
    @returns 结果摘要。
    @raises AdminError: 缺少 ``--provider`` / ``--subject``。
    @raises MigrationError: 目标库是非空旧库（缺显式归属）。
    @raises ApplicationError: 该身份已有用户。
    """
    # 先校验参数再打开库：参数不合法时不得在磁盘上留下一个半创建的目标库。
    normalized_provider, normalized_subject = _require_provider_and_subject(provider, subject)
    identity = _login_identity(normalized_provider, normalized_subject, display_name)

    database = open_lyra_database(database_path)
    try:
        resolved = UserRepository(database.connection).create_admin(identity, now=_now())
    finally:
        database.close()
    return (
        f"已创建首个管理员：userId={resolved.user.id} role={resolved.user.role} "
        f"status={resolved.user.status} displayName={resolved.user.display_name} "
        f"identity={resolved.identity.provider}:{resolved.identity.subject}"
    )


def _upgrade_legacy_database(
    database_path: str,
    confirmed_identities: tuple[MigrationOwnerIdentity, ...],
    provider: str | None,
    subject: str | None,
    display_name: str | None,
) -> str:
    """旧库升级路径：把 schema 15 的全部资源归给显式确认的原 owner。

    流程（契约 §3.10、计划 §9.3）：

    1. 归属用户 id 由本入口现场生成——旧库没有用户概念，不存在可沿用的 id；
    2. ``--owner-identity`` 列出的身份写入迁移上下文，**未列出的历史身份不会关联原用户**；
    3. 既有设备的身份来源由 ``--provider/--subject`` 显式指定，且必须是上面已列出的身份之一；
    4. 迁移成功后核对归属真的落到了本次生成的用户上——已经升级过的库不会静默返回成功。

    @param database_path: 目标库路径；必须是已存在的旧库。
    @param confirmed_identities: 已显式确认归属原 owner 的身份。
    @param provider: ``--provider``；旧库升级时表示既有设备的身份来源。
    @param subject: ``--subject``。
    @param display_name: ``--display-name``。
    @returns 结果摘要。
    @raises AdminError: 目标库不存在、设备身份不在已确认身份里、迁移没有建立归属。
    @raises MigrationError: 迁移失败。
    """
    # 旧库升级的前提是「有一个旧库」。路径写错时必须立刻失败：静默新建一个空库并把
    # owner 身份写进去，会让操作者以为升级成功，而真实数据在另一个文件里。
    if not Path(database_path).is_file():
        raise AdminError(
            f"旧库升级要求目标库已存在，但 {database_path} 不是文件；"
            "若这是新安装，请去掉 --owner-identity 并给出 --provider/--subject"
        )

    identities = _mark_device_identity(confirmed_identities, provider, subject)
    owner_user_id = str(uuid4())
    context = MigrationContext(
        owner_user_id=owner_user_id,
        owner_display_name=_owner_display_name(display_name, confirmed_identities),
        identities=identities,
    )

    database, unconfirmed = _apply_migrations_with_context(database_path, context)
    try:
        repository = UserRepository(database.connection)
        owner = repository.find_user_by_id(owner_user_id)
        if owner is None:
            # 上下文没有被消费：目标库多半已经是 schema 16（v16 只在首次执行时读取上下文）。
            # 此时本次操作没有改动任何归属，必须明确失败而不是报成功。
            raise AdminError(
                f"迁移没有建立用户 {owner_user_id}：目标库可能已经是 schema "
                f"{CURRENT_SCHEMA_VERSION}，本入口只执行一次升级，本次未改动任何归属"
            )
    finally:
        database.close()

    device_identity = next(
        (f"{item.provider}:{item.subject}" for item in identities if item.is_device_identity),
        "未指定",
    )
    lines = [
        f"旧库归属升级完成：userId={owner.id} role={owner.role} status={owner.status} "
        f"displayName={owner.display_name}",
        f"已确认归属该用户的身份（{len(identities)} 条）："
        + ", ".join(f"{item.provider}:{item.subject}" for item in identities),
        f"既有设备的身份来源：{device_identity}",
    ]
    # 旧 owner 身份表由迁移 16 删除（见 multi-user-schema.sql），两条路径（新库/升级库）
    # 的结果因此一致。这里只报告，不再自己删表——否则新安装路径会漏掉。
    lines.append(f"旧 owner 身份表已由迁移 16 删除（{_LEGACY_OWNER_IDENTITY_TABLE}）")
    if unconfirmed:
        lines.append(
            "警告：旧 owner 身份表中以下身份未被确认，它们不会关联原用户，"
            "按自动注册规则将各自得到独立空账本："
            + ", ".join(f"{provider_key}:{subject_key}" for provider_key, subject_key in unconfirmed)
        )
    return "\n".join(lines)


def _mark_device_identity(
    confirmed_identities: tuple[MigrationOwnerIdentity, ...],
    provider: str | None,
    subject: str | None,
) -> tuple[MigrationOwnerIdentity, ...]:
    """把 ``--provider/--subject`` 指定的身份标记为既有设备的身份来源。

    旧库没有记录设备本次登录用的是哪个身份，迁移因此要求上下文**显式**给出唯一一条
    ``is_device_identity``（见 ``migrations.py`` 的校验）。这里用 ``--provider/--subject``
    表达这份确认，而不是从 ``--owner-identity`` 里挑一条——挑一条就是猜。

    @param confirmed_identities: ``--owner-identity`` 解析出的身份。
    @param provider: ``--provider``；``None`` 表示未指定设备身份来源。
    @param subject: ``--subject``。
    @returns 标记好设备身份来源的身份序列。
    @raises AdminError: ``--provider/--subject`` 不成对，或该身份不在已确认身份中。
    """
    if provider is None and subject is None:
        # 未指定设备身份时全部标记为 False。旧库真的有设备时，迁移会明确失败，
        # 而不是由本入口随便挑一条身份补上。
        return confirmed_identities

    device_provider, device_subject = _require_provider_and_subject(provider, subject)
    device_key = (device_provider, device_subject)
    if device_key not in {
        (item.provider, item.subject) for item in confirmed_identities
    }:
        raise AdminError(
            f"--provider/--subject 指定的设备身份 {device_provider}:{device_subject} "
            "未出现在 --owner-identity 中；未列出的身份不得关联原用户，"
            "因此它不能作为既有设备的身份来源"
        )
    return tuple(
        replace(
            item,
            is_device_identity=(item.provider, item.subject) == device_key,
        )
        for item in confirmed_identities
    )


def _owner_display_name(
    explicit: str | None, confirmed_identities: tuple[MigrationOwnerIdentity, ...]
) -> str:
    """挑选迁移上下文要求的非空显示名。

    旧库没有记录用户显示名，因此缺省回退到第一条已确认身份的 ``subject``——它是上下文里
    唯一确定非空的文本。显示名只是展示值，不参与归属判定，不会因为有回退而改变谁能进入账本。

    @param explicit: ``--display-name`` 的值。
    @param confirmed_identities: 已确认身份，至少一条。
    @returns 非空显示名。
    """
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return confirmed_identities[0].subject


def _apply_migrations_with_context(
    database_path: str, context: MigrationContext
) -> tuple[LyraDatabase, tuple[tuple[str, str], ...]]:
    """带归属上下文迁移目标库，并返回连接与「未被确认的历史身份」。

    ``open_lyra_database`` 不接受迁移上下文，因此这里先用一条短连接执行
    :func:`apply_migrations`，再用 ``open_lyra_database`` 打开最终连接——后者会把 DuckDB
    溢出目录固定到账本同级，并再次校验迁移历史。两条连接**顺序**打开，不会同时持有同一个
    文件锁（DuckDB 同一文件只允许一个进程打开）。

    **未被确认的历史身份必须在迁移前读**：迁移 16 会删除旧 owner 身份表（见
    ``multi-user-schema.sql`` 的 DROP），迁移后再读只会拿到空集，操作者就再也看不到
    「哪些历史身份没有关联上原用户」——而那正是升级后登录看到空账本的原因。

    @param database_path: 目标库路径。
    @param context: 显式归属确认。
    @returns ``(已迁移的连接, 未确认的 (provider, subject) 元组)``。
    @raises MigrationError: 迁移失败；旧库会回滚到升级前状态。
    """
    connection = duckdb.connect(database_path)
    try:
        # 复用连接模块的溢出目录规则：v16 会整库重建（CTAS），大库上会溢出，而容器以只读
        # 根文件系统运行。重写这条规则会让两处悄悄漂移，因此直接调用同一实现。
        _configure_temp_directory(connection, database_path)
        unconfirmed = _unconfirmed_legacy_identities(connection, context.identities)
        apply_migrations(connection, context)
    finally:
        connection.close()
    return open_lyra_database(database_path), unconfirmed


def _unconfirmed_legacy_identities(
    connection: duckdb.DuckDBPyConnection,
    confirmed_identities: tuple[MigrationOwnerIdentity, ...],
) -> tuple[tuple[str, str], ...]:
    """列出旧 owner 身份表里**没有**被本次确认覆盖的身份键。

    旧表只是「曾经登录过」的记录，不是原账本的永久授权（计划 §9.3）。未确认的身份按自动
    注册规则各自得到独立空账本，因此这里把差异回报给操作者核对——漏掉一条真实身份的后果，
    是原用户升级后登录看到空账本。表不存在时（已被清理）返回空集。

    @param connection: 已迁移的连接。
    @param confirmed_identities: 本次显式确认的身份。
    @returns 未确认的 ``(provider, subject)``，按来源与主体排序。
    """
    if not _legacy_owner_identity_table_exists(connection):
        return ()
    rows = connection.execute(
        f"SELECT DISTINCT provider, subject FROM {_LEGACY_OWNER_IDENTITY_TABLE} "
        "ORDER BY provider, subject"
    ).fetchall()
    confirmed_keys = {(item.provider, item.subject) for item in confirmed_identities}
    return tuple(
        (str(row[0]), str(row[1]))
        for row in rows
        if (str(row[0]), str(row[1])) not in confirmed_keys
    )


def _legacy_owner_identity_table_exists(connection: duckdb.DuckDBPyConnection) -> bool:
    """判断旧 owner 身份表是否存在。

    @param connection: 目标库连接。
    @returns 表存在时为 ``True``。
    """
    exists = connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [_LEGACY_OWNER_IDENTITY_TABLE],
    ).fetchone()
    return exists is not None and int(exists[0]) > 0


def _now() -> datetime:
    """返回本次操作时间（UTC），用于仓储写入时间戳。

    @returns 当前 UTC 时间。
    """
    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
