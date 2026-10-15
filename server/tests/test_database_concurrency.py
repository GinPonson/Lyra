"""共享 DuckDB 连接的并发回归测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Thread

import pytest
from fastapi.testclient import TestClient

from lyra.application.device_pairing_service import DevicePairingService
from lyra.application.write_coordinator import WriteCoordinator
from lyra.config import Settings
from lyra.db.connection import open_lyra_database
from lyra.db.serialized_connection import SerializedDuckDBConnection
from lyra.infrastructure.auth.login_provider import LoginIdentity
from lyra.infrastructure.db.device_repository import DuckDbDeviceRepository
from lyra.main import create_app
from lyra.protocol import CLIENT_PROTOCOL_HEADERS

_DEVICE_KEYS = json.loads(
    (Path(__file__).parent / "fixtures" / "device-keys.json").read_text(encoding="utf-8")
)
_PUBLIC_KEY_PEM = _DEVICE_KEYS["cases"][0]["inputPem"]
_IDENTITY = LoginIdentity(
    provider="github", subject="concurrency-test", email=None, display_name="Concurrency Test"
)


class _FailingTerminalConnection:
    """可控模拟事务终结或底层关闭失败的 DuckDB 连接。"""

    description = None

    def __init__(self, terminal_statement: str, close_error: BaseException | None) -> None:
        """保存需要触发的故障，以便验证包装器的锁释放与异常链。"""
        self.terminal_statement = terminal_statement
        self.terminal_error = RuntimeError(f"{terminal_statement} failed")
        self.close_error = close_error
        self.close_calls = 0

    def execute(self, query: str, *args: object, **kwargs: object) -> _FailingTerminalConnection:
        """除指定事务终结语句外接受 SQL，并对目标语句抛出稳定异常。"""
        if query.strip().rstrip(";").upper() == self.terminal_statement:
            raise self.terminal_error
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        """为测试查询返回空结果。"""
        return []

    def close(self) -> None:
        """记录关闭尝试，并按测试场景可选地模拟底层关闭失败。"""
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture()
def authenticated_client(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """在隔离临时数据库上创建应用与可复用的有效设备访问令牌。"""
    database_path = tmp_path / "concurrency.duckdb"
    database = open_lyra_database(str(database_path))
    settings = Settings(
        LYRA_DATABASE_PATH=str(database_path),
        LYRA_IMPORT_STAGING_DIR=str(tmp_path / "staging"),
        LYRA_SECRET_MASTER_KEY="0" * 64,
    )
    application = create_app(settings, database)
    pairing = DevicePairingService(
        DuckDbDeviceRepository(database.connection), WriteCoordinator()
    )
    issued = asyncio.run(
        pairing.authorize_owner_device(
            identity=_IDENTITY,
            display_name="Concurrency Test Device",
            public_key_pem=_PUBLIC_KEY_PEM,
        )
    )
    try:
        with TestClient(application, headers=CLIENT_PROTOCOL_HEADERS) as client:
            yield client, issued.access_token
    finally:
        database.close()


def test_query_results_do_not_cross_between_threads() -> None:
    """两个线程交错读取时，每个 execute 返回的结果仍属于原查询。"""
    database = open_lyra_database(":memory:")
    connection = database.connection
    both_executed = Barrier(2)

    def read_value(value: str) -> str:
        """执行查询后等待另一线程也执行，再读取本线程的独立结果。"""
        result = connection.execute("SELECT ?::VARCHAR", [value])
        both_executed.wait(timeout=5)
        row = result.fetchone()
        assert row is not None
        return str(row[0])

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(read_value, "first")
            second = executor.submit(read_value, "second")
            assert {first.result(timeout=5), second.result(timeout=5)} == {"first", "second"}

        rows = connection.execute("SELECT * FROM range(3)")
        assert rows.fetchone() == (0,)
        assert rows.fetchmany(1) == [(1,)]
        assert rows.fetchall() == [(2,)]
        assert rows.fetchone() is None
    finally:
        database.close()


def test_rollback_cannot_include_another_threads_write() -> None:
    """一个事务回滚时，另一个请求的写入必须等事务结束后独立提交。"""
    database = open_lyra_database(":memory:")
    connection = database.connection
    connection.execute("CREATE TABLE concurrency_markers (label VARCHAR PRIMARY KEY)")
    transaction_started = Event()
    other_write_attempted = Event()
    allow_rollback = Event()
    other_write_finished = Event()

    def rollback_transaction() -> None:
        """在显式事务中写入一行，等待并发写入发起后回滚。"""
        connection.execute("BEGIN TRANSACTION")
        connection.execute("INSERT INTO concurrency_markers VALUES (?)", ["rolled-back"])
        transaction_started.set()
        if not allow_rollback.wait(timeout=5):
            raise TimeoutError("测试未允许事务回滚")
        connection.execute("ROLLBACK")

    def write_from_other_request() -> None:
        """尝试写入另一行，并在 SQL 完成后报告。"""
        if not transaction_started.wait(timeout=5):
            raise TimeoutError("事务没有开始")
        other_write_attempted.set()
        connection.execute("INSERT INTO concurrency_markers VALUES (?)", ["committed"])
        other_write_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            rollback_future = executor.submit(rollback_transaction)
            write_future = executor.submit(write_from_other_request)
            assert other_write_attempted.wait(timeout=5)
            was_blocked_by_transaction = not other_write_finished.wait(timeout=0.1)
            allow_rollback.set()
            rollback_future.result(timeout=5)
            write_future.result(timeout=5)

        rows = connection.execute(
            "SELECT label FROM concurrency_markers ORDER BY label"
        ).fetchall()
        assert was_blocked_by_transaction
        assert rows == [("committed",)]
    finally:
        allow_rollback.set()
        database.close()


def test_concurrent_authenticated_fastapi_reads_succeed(authenticated_client) -> None:
    """真实 FastAPI 认证与账本读取并发执行时均返回成功。"""
    client, access_token = authenticated_client
    headers = {"Authorization": f"Bearer {access_token}"}

    # 私有账本端点要求客户端明确选择全部账户范围。
    with ThreadPoolExecutor(max_workers=16) as executor:
        responses = list(
            executor.map(
                lambda _: client.get(
                    "/v1/ledger/summary", headers=headers, params={"scope": "all"}
                ),
                range(30),
            )
        )

    assert [response.status_code for response in responses] == [200] * 30, [
        response.text for response in responses if response.status_code != 200
    ]


@pytest.mark.parametrize("statement", ["COMMIT", "ROLLBACK"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_failed_transaction_end_releases_waiters_and_preserves_errors(
    statement: str,
    close_fails: bool,
) -> None:
    """事务终结失败应使共享连接明确失效，并让跨线程查询与关闭尽快退出。"""
    close_error = RuntimeError("raw close failed") if close_fails else None
    raw_connection = _FailingTerminalConnection(statement, close_error)
    connection = SerializedDuckDBConnection(raw_connection)  # type: ignore[arg-type]
    transaction_started = Event()
    allow_terminal_failure = Event()
    fetch_attempted = Event()
    close_attempted = Event()
    transaction_result: Queue[BaseException | None] = Queue()
    fetch_result: Queue[BaseException | None] = Queue()
    close_result: Queue[BaseException | None] = Queue()

    def fail_transaction() -> None:
        """持有事务锁直到主线程确认并发调用已经发起。"""
        try:
            connection.execute("BEGIN TRANSACTION")
            transaction_started.set()
            if not allow_terminal_failure.wait(timeout=5):
                raise TimeoutError("测试未触发事务终结故障")
            connection.execute(statement)
        except BaseException as error:
            transaction_result.put(error)
        else:
            transaction_result.put(None)

    def fetch_during_failed_transaction() -> None:
        """尝试读取共享连接，并记录失效状态异常而不阻塞测试线程。"""
        fetch_attempted.set()
        try:
            connection.execute("SELECT 1")
        except BaseException as error:
            fetch_result.put(error)
        else:
            fetch_result.put(None)

    def close_during_failed_transaction() -> None:
        """尝试关闭共享连接，并记录显式失败。"""
        close_attempted.set()
        try:
            connection.close()
        except BaseException as error:
            close_result.put(error)
        else:
            close_result.put(None)

    # 使用守护线程并对 join 设限：如果锁错误地永久保留，测试本身仍可明确失败并退出。
    transaction_thread = Thread(target=fail_transaction, daemon=True)
    fetch_thread = Thread(target=fetch_during_failed_transaction, daemon=True)
    close_thread = Thread(target=close_during_failed_transaction, daemon=True)
    transaction_thread.start()
    assert transaction_started.wait(timeout=5)
    fetch_thread.start()
    close_thread.start()
    assert fetch_attempted.wait(timeout=5)
    assert close_attempted.wait(timeout=5)
    allow_terminal_failure.set()

    for thread in (transaction_thread, fetch_thread, close_thread):
        thread.join(timeout=5)
        assert not thread.is_alive(), f"{thread.name} 未因连接失效而退出"

    terminal_error = transaction_result.get_nowait()
    assert terminal_error is raw_connection.terminal_error
    if close_fails:
        assert terminal_error.__cause__ is close_error
        assert any("raw close failed" in note for note in terminal_error.__notes__)
    else:
        assert terminal_error.__cause__ is None

    fetch_error = fetch_result.get_nowait()
    close_thread_error = close_result.get_nowait()
    assert isinstance(fetch_error, RuntimeError)
    assert "必须重新打开数据库连接" in str(fetch_error)
    assert isinstance(close_thread_error, RuntimeError)
    assert "必须重新打开数据库连接" in str(close_thread_error)
    assert raw_connection.close_calls == 1
