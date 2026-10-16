"""离线初始化入口（``lyra.admin``）的行为验证。

覆盖契约 §3.10 冻结的四条语义：

1. 新安装必须显式给出首个管理员的 ``(provider, subject)``，并创建固定 ``admin`` 用户；
2. 旧库升级只把 ``--owner-identity`` 列出的身份关联到原用户，**未列出的身份保持未关联**；
3. 旧库缺显式归属时由迁移明确失败，入口不提供任何兜底；
4. 入口不打印任何 token，也不把「首个请求」当作管理员凭据。

旧库用**冻结夹具** ``fixtures/typescript-ledger-schema15.duckdb`` 的副本构造（来源见
``fixtures/README.md``），与 ``test_migrations.py`` / ``test_multi_user_schema.py`` 共用同一
证据来源；夹具本身只读复制、绝不修改。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pytest

from lyra.admin import main
from lyra.config import Settings
from lyra.db.connection import open_lyra_database

# **冻结夹具**：迁移前由 TypeScript 服务端创建，schema 15、28 个对象、无数据。
FIXTURE_DATABASE = (
    Path(__file__).resolve().parent / "fixtures" / "typescript-ledger-schema15.duckdb"
)

OWNER_ACCOUNT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OWNER_DEVICE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

MASTER_KEY = "a" * 64

requires_fixture = pytest.mark.skipif(not FIXTURE_DATABASE.is_file(), reason="缺少冻结的既有账本夹具")


def _connect(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))


def _query(path: Path, sql: str, parameters: list[object] | None = None) -> list[tuple[object, ...]]:
    """在一条短连接上执行查询并取回全部行。"""
    connection = _connect(path)
    try:
        return connection.execute(sql, parameters or []).fetchall()
    finally:
        connection.close()


def _execute(path: Path, sql: str, parameters: list[object] | None = None) -> None:
    connection = _connect(path)
    try:
        connection.execute(sql, parameters or [])
    finally:
        connection.close()


def _table_exists(path: Path, table_name: str) -> bool:
    rows = _query(
        path,
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    )
    return int(rows[0][0]) > 0


def _schema_version(path: Path) -> int:
    rows = _query(path, "SELECT max(version) FROM schema_migrations")
    return int(rows[0][0])


def _copy_schema15(destination: Path) -> None:
    """复制冻结的 schema 15 夹具；必须先复制再打开，避免并行测试争抢文件锁。"""
    shutil.copy2(FIXTURE_DATABASE, destination)


def _seed_legacy_ledger(destination: Path, *, device: bool = False) -> None:
    """把旧库变成「非空」：一行账户，以及可选的既有设备。

    账户是迁移判定「库非空」的表之一；设备决定迁移上下文是否必须指定设备身份来源。
    """
    _execute(
        destination,
        """
        INSERT INTO accounts (id, display_name, legacy_account_key, masked_suffix)
        VALUES (CAST(? AS UUID), 'Main', 'ACC-1', '1234')
        """,
        [OWNER_ACCOUNT_ID],
    )
    if device:
        _execute(
            destination,
            """
            INSERT INTO paired_devices
                (id, display_name, public_key_pem, public_key_fingerprint, key_algorithm,
                 status, revoked_at)
            VALUES (CAST(? AS UUID), 'Pixel 9', 'pem', repeat('f', 64), 'ed25519',
                    'revoked', current_timestamp)
            """,
            [OWNER_DEVICE_ID],
        )


def _record_legacy_owner_identities(destination: Path, *identities: tuple[str, str]) -> None:
    """写入旧运行时的「owner 曾用身份」记录（schema 15 的 server_owner_identity）。"""
    connection = _connect(destination)
    try:
        connection.executemany(
            """
            INSERT INTO server_owner_identity
                (singleton_key, provider, subject, created_at, updated_at)
            VALUES (1, ?, ?, current_timestamp, current_timestamp)
            """,
            [list(identity) for identity in identities],
        )
    finally:
        connection.close()


def _admin_environment(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> None:
    """构造足以实例化 ``Settings`` 的环境，并清掉其它 ``LYRA_*`` 以免宿主机污染断言。"""
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, str):
            monkeypatch.delenv(alias, raising=False)
    monkeypatch.setenv("LYRA_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("LYRA_IMPORT_STAGING_DIR", str(database_path.parent / "staging"))
    monkeypatch.setenv("LYRA_SECRET_MASTER_KEY", MASTER_KEY)


# =====================================================================
# 新安装
# =====================================================================


def test_new_install_creates_the_first_admin(tmp_path: Path) -> None:
    """新安装：显式身份 → 恰好一个固定 ``role='admin'`` 的用户与一条身份。"""
    database = tmp_path / "fresh.duckdb"

    exit_code = main(
        [
            "--database",
            str(database),
            "--provider",
            "google",
            "--subject",
            "112233",
            "--display-name",
            "Owner",
        ]
    )

    assert exit_code == 0
    users = _query(database, "SELECT display_name, status, role FROM users")
    assert users == [("Owner", "active", "admin")]
    identities = _query(
        database, "SELECT provider, subject FROM user_identities"
    )
    assert identities == [("google", "112233")]
    assert _schema_version(database) == 16


def test_new_install_requires_provider_and_subject(tmp_path: Path) -> None:
    """缺少首个管理员身份时必须明确失败，且不得在磁盘上留下半初始化的库。"""
    database = tmp_path / "missing-identity.duckdb"

    exit_code = main(["--database", str(database)])

    assert exit_code == 1
    assert not database.exists(), "参数不合法时不得创建目标库"


def test_partial_provider_subject_pair_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--provider`` 与 ``--subject`` 必须成对给出，只给一个不得开始建库。"""
    database = tmp_path / "partial.duckdb"

    exit_code = main(["--database", str(database), "--provider", "google"])

    assert exit_code == 1
    assert "--subject" in capsys.readouterr().err
    assert not database.exists()


def test_new_install_rejects_unknown_provider(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """来源不在已知登录来源内时必须明确失败，而不是等到写入时才撞数据库约束。"""
    database = tmp_path / "unknown-provider.duckdb"

    exit_code = main(
        ["--database", str(database), "--provider", "wechat", "--subject", "112233"]
    )

    assert exit_code == 1
    assert "wechat" in capsys.readouterr().err
    assert not database.exists()


def test_new_install_does_not_downgrade_an_existing_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """同一身份再次初始化必须明确失败，绝不静默返回既有用户冒充初始化成功。"""
    database = tmp_path / "repeat.duckdb"
    arguments = [
        "--database",
        str(database),
        "--provider",
        "google",
        "--subject",
        "112233",
    ]
    assert main(arguments) == 0
    capsys.readouterr()

    assert main(arguments) == 1
    assert "该登录身份已存在用户" in capsys.readouterr().err
    assert _query(database, "SELECT count(*) FROM users") == [(1,)]
    assert _query(database, "SELECT role FROM users") == [("admin",)]


@requires_fixture
def test_new_install_refuses_a_non_empty_legacy_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """非空旧库走新安装路径时必须由迁移明确失败，旧库原封不动。"""
    database = tmp_path / "legacy-without-owner.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database)

    exit_code = main(
        ["--database", str(database), "--provider", "google", "--subject", "112233"]
    )

    assert exit_code == 1
    assert "必须显式提供原 owner 归属确认" in capsys.readouterr().err
    assert _schema_version(database) == 15
    assert _query(database, "SELECT count(*) FROM accounts") == [(1,)]
    assert not _table_exists(database, "users")


# =====================================================================
# 旧库升级
# =====================================================================


@requires_fixture
def test_legacy_upgrade_maps_only_confirmed_identities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """旧库升级：只有 ``--owner-identity`` 列出的身份关联原用户，全部资源随之归属。"""
    database = tmp_path / "legacy.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database, device=True)
    _record_legacy_owner_identities(database, ("google", "112233"), ("github", "999999"))

    exit_code = main(
        [
            "--database",
            str(database),
            "--owner-identity",
            "google:112233",
            "--provider",
            "google",
            "--subject",
            "112233",
            "--display-name",
            "原用户",
        ]
    )

    assert exit_code == 0
    owners = _query(database, "SELECT id, display_name, role, status FROM users")
    assert len(owners) == 1
    owner_id, display_name, role, status = owners[0]
    assert (display_name, role, status) == ("原用户", "admin", "active")

    # 未列出的 github:999999 不得关联原用户——它连身份行都不该有。
    identities = _query(database, "SELECT user_id, provider, subject FROM user_identities")
    assert [(str(row[0]), row[1], row[2]) for row in identities] == [
        (str(owner_id), "google", "112233")
    ]

    # 旧资源全部归给本次显式确认的原用户，既有设备挂到显式指定的设备身份上。
    assert _query(database, "SELECT user_id FROM accounts") == [(owner_id,)]
    device_identity = _query(
        database,
        """
        SELECT user_identities.provider, user_identities.subject
        FROM paired_devices
        JOIN user_identities ON user_identities.id = paired_devices.identity_id
        """,
    )
    assert device_identity == [("google", "112233")]

    # 旧 owner 身份表在升级后删除：它不该继续作为第二条可读的授权来源。
    # **删除由迁移 16 负责**，不是由本入口负责——否则直接建库的新安装路径会漏掉，
    # 于是「新库」与「升级库」的表集合不一致。
    assert not _table_exists(database, "server_owner_identity")

    # 未被确认的历史身份必须回报给操作者核对，否则原用户会登录到空账本却没人发现。
    output = capsys.readouterr().out
    assert "旧 owner 身份表已由迁移 16 删除" in output
    assert "github:999999" in output
    assert "未被确认" in output


@requires_fixture
def test_legacy_upgrade_requires_device_identity_when_devices_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """旧库有设备却没指定设备身份来源时，迁移必须明确失败（入口不替它挑一条）。"""
    database = tmp_path / "no-device-identity.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database, device=True)

    exit_code = main(
        ["--database", str(database), "--owner-identity", "google:112233"]
    )

    assert exit_code == 1
    assert "只能指定一条 is_device_identity" in capsys.readouterr().err
    assert _schema_version(database) == 15
    assert not _table_exists(database, "users")


@requires_fixture
def test_device_identity_must_be_one_of_the_confirmed_identities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """设备身份来源若未列在 ``--owner-identity`` 中，必须在打开库之前失败。"""
    database = tmp_path / "unconfirmed-device.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database, device=True)

    exit_code = main(
        [
            "--database",
            str(database),
            "--owner-identity",
            "google:112233",
            "--provider",
            "github",
            "--subject",
            "999999",
        ]
    )

    assert exit_code == 1
    assert "未出现在 --owner-identity" in capsys.readouterr().err
    assert _schema_version(database) == 15


@requires_fixture
def test_legacy_upgrade_rejects_duplicate_confirmed_identities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """重复身份由迁移自己的校验拒绝，入口不替它去重或猜测归属。"""
    database = tmp_path / "duplicate.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database)

    exit_code = main(
        [
            "--database",
            str(database),
            "--owner-identity",
            "google:112233",
            "--owner-identity",
            "google:112233",
        ]
    )

    assert exit_code == 1
    assert "重复身份" in capsys.readouterr().err
    assert _schema_version(database) == 15


@requires_fixture
def test_legacy_upgrade_is_not_silently_repeated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """升级过的库再次执行旧库升级必须明确失败，而不是报一个什么都没做的成功。"""
    database = tmp_path / "already-upgraded.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database)
    arguments = [
        "--database",
        str(database),
        "--owner-identity",
        "google:112233",
        "--display-name",
        "原用户",
    ]
    assert main(arguments) == 0
    capsys.readouterr()

    assert main(arguments) == 1
    assert "本入口只执行一次升级" in capsys.readouterr().err
    assert _query(database, "SELECT count(*) FROM users") == [(1,)]
    assert _query(database, "SELECT count(*) FROM user_identities") == [(1,)]


@requires_fixture
def test_legacy_upgrade_rolls_back_and_can_be_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """迁移中途失败必须整体回滚到旧库；清理故障后同一条命令必须能重试成功。"""
    database = tmp_path / "fault-injection.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database)
    # 故障注入：v16 的第一条 CTAS 会撞上同名表，迁移必须整体失败并回滚。
    _execute(database, "CREATE TABLE _stage_accounts (id INTEGER)")

    assert main(["--database", str(database), "--owner-identity", "google:112233"]) == 1
    capsys.readouterr()
    assert _schema_version(database) == 15
    assert _query(database, "SELECT count(*) FROM accounts") == [(1,)]
    assert not _table_exists(database, "users")

    _execute(database, "DROP TABLE _stage_accounts")
    assert main(["--database", str(database), "--owner-identity", "google:112233"]) == 0
    assert _query(database, "SELECT count(*) FROM accounts") == [(1,)]
    assert _query(database, "SELECT role FROM users") == [("admin",)]


@requires_fixture
def test_upgraded_database_can_be_opened_by_the_server_path(tmp_path: Path) -> None:
    """升级后的库必须能被服务端启动路径打开，且不会重复迁移或新建用户。"""
    database = tmp_path / "restart.duckdb"
    _copy_schema15(database)
    _seed_legacy_ledger(database)
    assert main(["--database", str(database), "--owner-identity", "google:112233"]) == 0
    users_before = _query(database, "SELECT id, role FROM users")

    server = open_lyra_database(str(database))
    try:
        assert server.read_health()["schemaVersion"] == 16
    finally:
        server.close()

    assert _query(database, "SELECT id, role FROM users") == users_before
    assert _query(database, "SELECT count(*) FROM user_identities") == [(1,)]


def test_legacy_upgrade_requires_an_existing_database_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """路径写错时必须立刻失败：静默新建空库会把「升级成功」伪装出来。"""
    database = tmp_path / "typo.duckdb"

    exit_code = main(["--database", str(database), "--owner-identity", "google:112233"])

    assert exit_code == 1
    assert "旧库升级要求目标库已存在" in capsys.readouterr().err
    assert not database.exists()


@pytest.mark.parametrize("raw_identity", ["google", "google:", ":112233", "wechat:112233"])
def test_malformed_owner_identity_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], raw_identity: str
) -> None:
    """``--owner-identity`` 必须是 ``provider:subject``，且来源已知。"""
    database = tmp_path / "malformed.duckdb"

    exit_code = main(["--database", str(database), "--owner-identity", raw_identity])

    assert exit_code == 1
    assert "--owner-identity" in capsys.readouterr().err
    assert not database.exists()


# =====================================================================
# 默认路径与输出内容
# =====================================================================


def test_database_path_defaults_to_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--database`` 缺省时取既有 ``Settings.lyra_database_path``。"""
    database = tmp_path / "from-settings.duckdb"
    _admin_environment(monkeypatch, database)

    exit_code = main(["--provider", "google", "--subject", "112233"])

    assert exit_code == 0
    assert _query(database, "SELECT role FROM users") == [("admin",)]


def test_report_never_prints_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功报告只含用户与身份标识：不得出现任何 token / 密钥字样。"""
    database = tmp_path / "report.duckdb"

    assert main(["--database", str(database), "--provider", "google", "--subject", "112233"]) == 0

    captured = capsys.readouterr()
    output = (captured.out + captured.err).lower()
    for forbidden in ("token", "secret", "bearer", "eyj", "password"):
        assert forbidden not in output
    owner_id = str(_query(database, "SELECT id FROM users")[0][0])
    assert owner_id in captured.out
