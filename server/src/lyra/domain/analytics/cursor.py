"""查询游标：编码、解码与分页切片。

镜像 TypeScript 版 ``domain/analytics/queries.ts`` 的游标部分。

**游标不是"页码"**，而是一个指向"上次看到的那一行"的定位符，因此它必须带上
**筛选条件指纹**：

```json
{"version": 1, "transactionHash": "...", "filterFingerprint": "..."}
```

三道校验，各自防一种静默错误：

| 校验 | 不做会怎样 |
| --- | --- |
| **筛选条件指纹** | 把查询 A 的游标用到查询 B 上，会从"另一个结果集里的某一行"继续——**返回的行错位且不报错** |
| **定位哈希必须在当前快照里找得到** | 账本已变化（新导入改了顺序）时，游标指向的位置已无意义 |
| **解码失败不回退到第一页** | 客户端会把"游标坏了"当成"数据到头了"，**静默停在第一页** |

另：**多取一条**判断是否还有下一页，而不是先数总数——数总数需要对整个结果集遍历，
而多取一条只需一次切片。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..ledger.errors import IngestError

CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 1_024


@dataclass(frozen=True)
class TradeCursor:
    """交易流水的游标载荷。"""

    transaction_hash: str
    filter_fingerprint: str


@dataclass(frozen=True)
class Page:
    """一页结果。"""

    rows: tuple[Any, ...]
    next_cursor: str | None


def encode_trade_cursor(cursor: TradeCursor) -> str:
    """编码游标为 base64url（无填充）。

    @param cursor: 游标载荷。
    @returns base64url 文本。
    """
    payload = json.dumps(
        {
            "version": CURSOR_VERSION,
            "transactionHash": cursor.transaction_hash,
            "filterFingerprint": cursor.filter_fingerprint,
        },
        separators=(",", ":"), ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_trade_cursor(value: str) -> TradeCursor:
    """严格解码游标。

    **解析失败一律抛错，绝不回退到第一页。** 回退会让客户端把"游标坏了"当成"数据
    到头了"——它会以为已经看完全部数据，而实际上只看了第一页。

    @param value: base64url 文本。
    @returns 游标载荷。
    @raises IngestError: 长度非法、不是 base64url、不是 JSON、版本不符或字段缺失。
    """
    if value == "" or len(value) > MAX_CURSOR_LENGTH:
        raise IngestError("交易流水游标无效")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise IngestError("交易流水游标无效") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IngestError("交易流水游标无效") from error
    if not isinstance(parsed, dict):
        raise IngestError("交易流水游标无效")
    if parsed.get("version") != CURSOR_VERSION:
        raise IngestError("交易流水游标版本不符")
    transaction_hash = parsed.get("transactionHash")
    fingerprint = parsed.get("filterFingerprint")
    if not isinstance(transaction_hash, str) or transaction_hash == "":
        raise IngestError("交易流水游标无效")
    if not isinstance(fingerprint, str) or fingerprint == "":
        raise IngestError("交易流水游标无效")
    return TradeCursor(
        transaction_hash=transaction_hash, filter_fingerprint=fingerprint
    )


def trade_filter_fingerprint(
    account_scope: Any,
    *,
    symbol: str | None = None,
    action: str | None = None,
    source_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """对账户范围与筛选条件生成稳定指纹。

    **顺序与空值处理必须与参考实现一致**：条件不同却得到同一指纹，会让游标被误用到
    另一组筛选上；条件相同却得到不同指纹，会让合法的翻页请求被拒。

    @param account_scope: 账户范围。
    @param symbol: 标的筛选。
    @param action: 动作筛选。
    @param source_type: 来源筛选。
    @param from_date: 起始日期。
    @param to_date: 结束日期。
    @returns SHA-256 十六进制指纹。
    """
    scope = (
        {"kind": "all"}
        if getattr(account_scope, "kind", None) == "all"
        else {"kind": "account", "account": account_scope.account}
    )
    canonical = json.dumps(
        {
            "accountScope": scope,
            "symbol": symbol if symbol is not None else None,
            "action": action if action is not None else None,
            "sourceType": source_type if source_type is not None else None,
            "fromDate": from_date if from_date is not None else None,
            "toDate": to_date if to_date is not None else None,
        },
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def paginate(
    rows: list[Any],
    *,
    limit: int,
    cursor: str | None,
    fingerprint: str,
    hash_of: Any,
) -> Page:
    """按定位游标切出一页。

    **多取一条**来判断是否还有下一页。

    @param rows: 已筛选并排好序的全部行。
    @param limit: 每页条数。
    @param cursor: 客户端游标；``None`` 表示从第一页开始。
    @param fingerprint: 本次查询的筛选指纹。
    @param hash_of: 从一行取出定位哈希的可调用对象。
    @returns 该页的行与下一页游标（没有下一页时为 ``None``）。
    @raises ValueError: ``limit`` 非正。
    @raises IngestError: 游标无效、指纹不符，或定位哈希不在当前快照里。
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("分页条数必须是正安全整数")

    page_start = 0
    if cursor is not None:
        decoded = decode_trade_cursor(cursor)
        if decoded.filter_fingerprint != fingerprint:
            # 游标来自另一组筛选条件。继续用它会从"另一个结果集里的某一行"往下取，
            # **返回的行错位而不报错**。
            raise IngestError("交易流水游标不属于当前查询条件")
        index = next(
            (i for i, row in enumerate(rows) if hash_of(row) == decoded.transaction_hash),
            None,
        )
        if index is None:
            # 账本已变化（例如新导入改了顺序），游标指向的位置已无意义。
            raise IngestError("交易流水游标已不属于当前账本快照")
        page_start = index + 1

    candidates = rows[page_start : page_start + limit + 1]
    page_rows = candidates[:limit]
    next_cursor = (
        encode_trade_cursor(TradeCursor(
            transaction_hash=hash_of(page_rows[-1]), filter_fingerprint=fingerprint
        ))
        if len(candidates) > limit and page_rows
        else None
    )
    return Page(rows=tuple(page_rows), next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# 明细族游标（realized / cashflow）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetailCursor:
    """明细查询的游标载荷。

    **比交易流水族多一个 ``query`` 判别字段**，且该字段在解码时被校验：

    - 载荷里的 ``query`` —— 把 realized 的游标用在 cashflow 上会在此被拒；
    - 指纹里的 ``query`` —— 第二道独立防线。

    两处独立校验是刻意的冗余：游标是客户端可回传的输入，**两道校验比一道可靠**——
    万一日后有人给某一族换了指纹算法而忘了同步，另一道仍能拦住跨查询误用。
    """

    query: str
    row_key: str
    filter_fingerprint: str


DETAIL_QUERIES = ("realized", "cashflow")


def encode_detail_cursor(cursor: DetailCursor) -> str:
    """编码明细游标。

    @param cursor: 载荷。
    @returns base64url（无填充）。
    """
    payload = json.dumps(
        {
            "version": CURSOR_VERSION,
            "query": cursor.query,
            "rowKey": cursor.row_key,
            "filterFingerprint": cursor.filter_fingerprint,
        },
        separators=(",", ":"), ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_detail_cursor(value: str, query: str) -> DetailCursor:
    """严格解码明细游标，并校验它属于 ``query`` 这一族。

    @param value: base64url 文本。
    @param query: 期望的查询名（``realized`` 或 ``cashflow``）。
    @returns 载荷。
    @raises IngestError: 长度非法、不是 base64url、不是 JSON、版本不符、字段缺失，
        或**游标属于另一族查询**。
    """
    if value == "" or len(value) > MAX_CURSOR_LENGTH:
        raise IngestError(f"{query} 游标无效")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        parsed = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IngestError(f"{query} 游标无效") from error
    if not isinstance(parsed, dict) or parsed.get("version") != CURSOR_VERSION:
        raise IngestError(f"{query} 游标无效")
    if parsed.get("query") != query:
        # **判别字段不符即拒**：一个 realized 的游标用在 cashflow 上会走到这里，
        # 而不是靠指纹是否恰好不同。
        raise IngestError(f"{query} 游标不属于当前查询")
    row_key = parsed.get("rowKey")
    fingerprint = parsed.get("filterFingerprint")
    if not isinstance(row_key, str) or row_key == "":
        raise IngestError(f"{query} 游标无效")
    if not isinstance(fingerprint, str) or fingerprint == "":
        raise IngestError(f"{query} 游标无效")
    return DetailCursor(query=query, row_key=row_key, filter_fingerprint=fingerprint)


def detail_filter_fingerprint(
    query: str, account_scope: Any, **filters: Any
) -> str:
    """明细查询的筛选指纹。

    **与交易流水族的指纹有一处结构差异：这里包含 ``query``。** 因此即使某条命令的
    筛选条件与另一条完全相同，两族指纹也不会相同。

    @param query: 查询名（``realized`` 或 ``cashflow``）。
    @param account_scope: 账户范围。
    @param filters: 该查询的筛选条件。
    @returns SHA-256 十六进制指纹。
    @raises ValueError: 查询名不在 ``DETAIL_QUERIES`` 内。
    """
    if query not in DETAIL_QUERIES:
        raise ValueError(f"未注册的明细查询: {query}")
    scope = (
        {"kind": "all"}
        if getattr(account_scope, "kind", None) == "all"
        else {"kind": "account", "account": account_scope.account}
    )
    payload: dict[str, Any] = {"query": query, "accountScope": scope}
    # **键序由排序保证**：JSON 的键序若不固定，同一组条件会得到不同指纹，
    # 于是合法的翻页请求会被拒。
    for key in sorted(filters):
        value = filters[key]
        payload[key] = value if value is not None else None
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def paginate_detail(
    rows: list[Any],
    *,
    limit: int,
    cursor: str | None,
    query: str,
    fingerprint: str,
    row_key_of: Any,
) -> Page:
    """按明细游标切出一页。

    @param rows: 已筛选并排好序的全部行。
    @param limit: 每页条数。
    @param cursor: 客户端游标；``None`` 表示第一页。
    @param query: 查询名。
    @param fingerprint: 本次查询的筛选指纹。
    @param row_key_of: 从一行取出行键的可调用对象。
    @returns 该页与下一页游标。
    @raises ValueError: ``limit`` 非正。
    @raises IngestError: 游标无效、不属于本查询、指纹不符，或行键不在当前快照里。
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("分页条数必须是正安全整数")

    page_start = 0
    if cursor is not None:
        decoded = decode_detail_cursor(cursor, query)
        if decoded.filter_fingerprint != fingerprint:
            raise IngestError(f"{query} 游标不属于当前查询条件")
        index = next(
            (i for i, row in enumerate(rows) if row_key_of(row) == decoded.row_key),
            None,
        )
        if index is None:
            raise IngestError(f"{query} 游标已不属于当前账本快照")
        page_start = index + 1

    candidates = rows[page_start : page_start + limit + 1]
    page_rows = candidates[:limit]
    next_cursor = (
        encode_detail_cursor(DetailCursor(
            query=query, row_key=row_key_of(page_rows[-1]), filter_fingerprint=fingerprint
        ))
        if len(candidates) > limit and page_rows
        else None
    )
    return Page(rows=tuple(page_rows), next_cursor=next_cursor)


def realized_row_key(record: Any) -> str:
    """已实现明细的行键。

    **复合哈希而非单一字段**：同一笔交易可能同时是"开"与"闭"（部分平仓），
    因此单看开仓或闭仓交易哈希都无法唯一定位一行。

    @param record: 已实现明细。
    @returns SHA-256 十六进制。
    """
    canonical = json.dumps(
        {
            "account": record.account,
            "openTransaction": record.open_transaction,
            "closeTransaction": record.close_transaction,
        },
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cashflow_row_key(row: Any) -> str:
    """现金流水行键——直接用交易哈希（与参考实现一致）。

    **入参是标准交易 ``ParsedTransaction``，不是投影后的 ``CashflowRow``**：
    分页发生在投影之前，参考实现同样取 ``transaction.transactionHash``。

    @param row: 现金流水对应的标准交易。
    @returns 交易哈希。
    """
    return row.transaction_hash
