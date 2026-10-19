"""为 DuckDB 单连接提供线程安全的执行与事务边界。

DuckDB Python 连接会把查询结果保存在连接自身上，因此仅串行化 ``execute`` 仍会让另一个
线程覆盖尚未读取的结果。这里在锁内复制结果，并在显式事务期间一直持有可重入锁。
"""

from __future__ import annotations

from collections.abc import Iterator
from threading import RLock, get_ident
from typing import Any

import duckdb


class SerializedQueryResult:
    """保存一次查询的独立结果，提供仓储当前使用的 DB-API 读取语义。

    @param rows: 查询执行时在连接锁内取出的行。
    @param description: DuckDB 返回的列描述；无列结果时为 ``None``。
    """

    def __init__(
        self,
        rows: list[tuple[Any, ...]],
        description: list[tuple[Any, ...]] | None,
    ) -> None:
        self._rows = rows
        self._index = 0
        self.description = None if description is None else tuple(description)

    def fetchone(self) -> tuple[Any, ...] | None:
        """返回下一行；结果耗尽时返回 ``None``。

        @returns 下一行，或表示结果耗尽的 ``None``。
        """
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[tuple[Any, ...]]:
        """返回尚未读取的所有行，并将结果游标推进到末尾。

        @returns 尚未读取的行；再次调用时返回空列表。
        """
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        """返回最多 ``size`` 行；未指定时按 DB-API 默认值读取一行。

        @param size: 要读取的最大行数，必须是非负整数。
        @returns 尚未读取的行。
        @raises ValueError: ``size`` 为负数。
        """
        count = 1 if size is None else size
        if count < 0:
            raise ValueError("fetchmany 的行数不能为负数")
        rows = self._rows[self._index : self._index + count]
        self._index += len(rows)
        return rows

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        """按顺序迭代尚未读取的行。"""
        return self

    def __next__(self) -> tuple[Any, ...]:
        """返回下一行；结果耗尽时遵循迭代器协议抛出 ``StopIteration``。"""
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class SerializedDuckDBConnection:
    """串行化共享 DuckDB 连接，并隔离每次执行产生的结果集。

    @param connection: 已打开并完成迁移的原生 DuckDB 连接。
    """

    _BEGIN_STATEMENTS = {"BEGIN", "BEGIN TRANSACTION", "START TRANSACTION"}
    _END_STATEMENTS = {
        "COMMIT",
        "COMMIT TRANSACTION",
        "END",
        "END TRANSACTION",
        "ROLLBACK",
        "ROLLBACK TRANSACTION",
    }

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._connection = connection
        self._lock = RLock()
        self._closed = False
        self._failure: BaseException | None = None
        self._transaction_owner: int | None = None

    def execute(self, query: str, *args: object, **kwargs: object) -> SerializedQueryResult:
        """在线程锁内执行 SQL、复制结果，并维持显式事务的整体互斥。

        @param query: 单条 SQL 语句。
        @param args: 传给 DuckDB ``execute`` 的位置参数，例如参数绑定值。
        @param kwargs: 传给 DuckDB ``execute`` 的关键字参数。
        @returns 与连接结果集独立的读取对象。
        @raises Exception: 原样抛出 DuckDB 执行或读取错误。
        @raises RuntimeError: 连接已关闭，或事务终结失败后连接状态不可信。
        """
        statement = _normalize_transaction_statement(query)
        begins_transaction = statement in self._BEGIN_STATEMENTS
        ends_transaction = statement in self._END_STATEMENTS
        thread_id = get_ident()

        # 先取一个会跨越多次 execute 的锁计数；RLock 允许本线程在事务内继续执行 SQL。
        if begins_transaction:
            self._lock.acquire()
        self._lock.acquire()

        transaction_started = False
        transaction_ended = False
        try:
            self._ensure_available()

            try:
                self._connection.execute(query, *args, **kwargs)
            except BaseException as transaction_error:
                if ends_transaction and self._transaction_owner == thread_id:
                    # COMMIT/ROLLBACK 执行失败后，原生事务状态已不可判定。先标记连接不可用，
                    # 再尝试关闭底层句柄；finally 会释放本线程预留的事务锁计数。
                    self._transaction_owner = None
                    transaction_ended = True
                    self._failure = transaction_error
                    self._closed = True
                    try:
                        self._connection.close()
                    except BaseException as close_error:
                        transaction_error.add_note(
                            f"事务失败后的底层连接关闭也失败，必须重新打开连接：{close_error!r}"
                        )
                        # 保留事务执行异常作为向上抛出的异常对象，关闭异常在因果链中可见。
                        raise transaction_error from close_error
                raise

            if begins_transaction:
                self._transaction_owner = thread_id
                transaction_started = True
            elif ends_transaction and self._transaction_owner == thread_id:
                # COMMIT/ROLLBACK 已由原生连接成功执行，事务边界现在可以释放。
                self._transaction_owner = None
                transaction_ended = True

            # DuckDB 将游标状态放在连接对象本身；在释放互斥锁前复制所有行，避免后续
            # execute 覆盖结果，也让调用者在锁外使用 fetchone/fetchall。
            description = self._connection.description
            rows = self._connection.fetchall()
            return SerializedQueryResult(rows, description)
        finally:
            self._lock.release()
            if transaction_ended:
                self._lock.release()
            elif begins_transaction and not transaction_started:
                # BEGIN 失败时撤销为事务预留的锁计数，保留原始数据库异常。
                self._lock.release()

    def close(self) -> None:
        """在线程锁内关闭底层连接；重复调用保持幂等。

        若当前事务所有者关闭连接，也释放该事务持有的锁计数；其他线程的关闭会等待
        事务正常提交或回滚后再执行。
        """
        thread_id = get_ident()
        self._lock.acquire()
        release_transaction_lock = False
        try:
            if self._failure is not None:
                self._ensure_available()
            if self._closed:
                return
            try:
                self._connection.close()
            except BaseException as close_error:
                # 底层 close 失败时不再允许复用连接；若调用者就是事务所有者，
                # 也必须撤销其锁保留，否则其它线程会永久等待一个无法恢复的事务。
                self._failure = close_error
                self._closed = True
                if self._transaction_owner == thread_id:
                    self._transaction_owner = None
                    release_transaction_lock = True
                raise

            self._closed = True
            if self._transaction_owner == thread_id:
                self._transaction_owner = None
                release_transaction_lock = True
        finally:
            self._lock.release()
            if release_transaction_lock:
                self._lock.release()

    def _ensure_available(self) -> None:
        """拒绝使用已关闭或事务状态不可信的连接，并保留导致失效的异常链。"""
        if self._failure is not None:
            raise RuntimeError("DuckDB 连接状态不可信，必须重新打开数据库连接") from self._failure
        if self._closed:
            raise RuntimeError("DuckDB 连接已关闭")


def _normalize_transaction_statement(query: str) -> str:
    """归一化事务控制语句，以识别会跨越多条 SQL 的显式事务边界。

    @param query: 待识别的 SQL。
    @returns 去除外围空白与末尾分号后的大写语句。
    """
    return query.strip().rstrip(";").strip().upper()
