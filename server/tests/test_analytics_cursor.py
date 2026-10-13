"""查询游标测试。

三道校验，各自防一种静默错误：

| 校验 | 不做会怎样 |
| --- | --- |
| **筛选条件指纹** | 把查询 A 的游标用到查询 B 上，会从"另一个结果集里的某一行"继续——**返回的行错位且不报错** |
| **定位哈希必须在当前快照里找得到** | 账本已变化时，游标指向的位置已无意义 |
| **解码失败不回退到第一页** | 客户端会把"游标坏了"当成"数据到头了"，**静默停在第一页** |
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lyra.domain.analytics.cursor import (
    CURSOR_VERSION,
    TradeCursor,
    decode_trade_cursor,
    encode_trade_cursor,
    paginate,
    trade_filter_fingerprint,
)
from lyra.domain.ledger.errors import IngestError

ALL_SCOPE = SimpleNamespace(kind="all", account=None)
ACCOUNT_SCOPE = SimpleNamespace(kind="account", account="TST001")


def _rows(count: int) -> list[str]:
    """构造定位哈希序列。"""
    return [f"h{i}" for i in range(count)]


def _page(rows, *, limit, cursor=None, fingerprint="fp", hash_of=lambda r: r):
    """跑一次分页。"""
    return paginate(rows, limit=limit, cursor=cursor, fingerprint=fingerprint, hash_of=hash_of)


# --------------------------------------------------------------------------
# 编解码
# --------------------------------------------------------------------------


def test_cursor_round_trips() -> None:
    """编解码往返一致。"""
    cursor = TradeCursor(transaction_hash="h7", filter_fingerprint="abc")

    decoded = decode_trade_cursor(encode_trade_cursor(cursor))

    assert decoded == cursor


def test_cursor_is_base64url_without_padding() -> None:
    """游标是 base64url 且无填充。"""
    encoded = encode_trade_cursor(TradeCursor(transaction_hash="h", filter_fingerprint="f"))

    assert "=" not in encoded
    assert all(c.isalnum() or c in "-_" for c in encoded)


def test_cursor_carries_the_version() -> None:
    """游标里带版本号——**将来换格式时才有拒绝旧游标的依据**。"""
    import base64, json

    encoded = encode_trade_cursor(TradeCursor(transaction_hash="h", filter_fingerprint="f"))
    payload = json.loads(base64.urlsafe_b64decode(encoded + "==").decode())

    assert payload["version"] == CURSOR_VERSION == 1


@pytest.mark.parametrize(
    "value",
    [
        "",                          # 空
        "not base64!",               # 字符集不符
        "a" * 1025,                  # 超长
        "e30",                       # {} —— 缺字段
        "eyJ2ZXJzaW9uIjoyfQ",        # version 2 —— 版本不符
    ],
)
def test_invalid_cursors_are_rejected(value: str) -> None:
    """非法游标一律报错，**不回退到第一页**。"""
    with pytest.raises(IngestError):
        decode_trade_cursor(value)


def test_cursor_with_empty_fields_is_rejected() -> None:
    """字段为空同样拒绝——空定位哈希无法定位任何一行。"""
    import base64, json

    payload = json.dumps(
        {"version": 1, "transactionHash": "", "filterFingerprint": "f"}
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()

    with pytest.raises(IngestError):
        decode_trade_cursor(encoded)


# --------------------------------------------------------------------------
# 筛选指纹
# --------------------------------------------------------------------------


def test_fingerprint_is_stable() -> None:
    """相同条件得到相同指纹。"""
    assert trade_filter_fingerprint(ALL_SCOPE) == trade_filter_fingerprint(ALL_SCOPE)


def test_fingerprint_distinguishes_filters() -> None:
    """**不同筛选条件必须得到不同指纹。**

    否则查询 A 的游标能用查询 B 上，从"另一个结果集里的某一行"继续——**返回的行
    错位且不报错**。
    """
    base = trade_filter_fingerprint(ALL_SCOPE)

    assert trade_filter_fingerprint(ALL_SCOPE, symbol="INTC") != base
    assert trade_filter_fingerprint(ALL_SCOPE, action="Buy") != base
    assert trade_filter_fingerprint(ALL_SCOPE, source_type="csv") != base
    assert trade_filter_fingerprint(ALL_SCOPE, from_date="2026-01-05") != base
    assert trade_filter_fingerprint(ALL_SCOPE, to_date="2026-01-05") != base


def test_fingerprint_distinguishes_account_scopes() -> None:
    """账户范围不同则指纹不同。"""
    assert trade_filter_fingerprint(ALL_SCOPE) != trade_filter_fingerprint(ACCOUNT_SCOPE)


def test_fingerprint_treats_absent_and_none_alike() -> None:
    """未提供与显式 ``None`` 等价——两者都表示"不筛选"。"""
    assert trade_filter_fingerprint(ALL_SCOPE) == trade_filter_fingerprint(ALL_SCOPE, symbol=None)


# --------------------------------------------------------------------------
# 分页
# --------------------------------------------------------------------------


def test_first_page_takes_exactly_the_limit() -> None:
    """第一页取满 limit 条。"""
    page = _page(_rows(10), limit=4)

    assert page.rows == ("h0", "h1", "h2", "h3")
    assert page.next_cursor is not None


def test_pages_are_contiguous_without_gaps_or_duplicates() -> None:
    """**逐页取完不重复不遗漏。**

    这是分页唯一真正要保证的事，因此用完整遍历来验证，而不是只看单页。
    """
    rows = _rows(10)
    collected: list[str] = []
    cursor = None
    for _ in range(10):
        page = _page(rows, limit=3, cursor=cursor)
        collected.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert collected == rows


def test_last_page_has_no_cursor() -> None:
    """最后一页的 ``next_cursor`` 为 ``None``。

    **多取一条**判断是否还有下一页：若按"取满 limit 就还有下一页"判断，
    恰好整除时最后一页会给出一个指向空的游标。
    """
    page = _page(_rows(6), limit=3)

    assert page.rows == ("h0", "h1", "h2")
    assert page.next_cursor is not None

    last = _page(_rows(6), limit=3, cursor=page.next_cursor)
    assert last.rows == ("h3", "h4", "h5")
    assert last.next_cursor is None


def test_exact_multiple_does_not_produce_an_empty_page() -> None:
    """**恰好整除时不产出一个空页。**

    这是"多取一条"的价值：`len == limit` 时没有下一页，因此不给游标。
    """
    rows = _rows(3)

    page = _page(rows, limit=3)

    assert page.rows == ("h0", "h1", "h2")
    assert page.next_cursor is None


def test_empty_result_yields_no_cursor() -> None:
    """空结果没有游标。"""
    page = _page([], limit=3)

    assert page.rows == ()
    assert page.next_cursor is None


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_invalid_limits_are_rejected(limit: object) -> None:
    """条数必须是正整数。"""
    with pytest.raises(ValueError, match="正安全整数"):
        _page(_rows(5), limit=limit)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 跨查询误用与快照变化
# --------------------------------------------------------------------------


def test_a_cursor_from_another_filter_is_rejected() -> None:
    """**把查询 A 的游标用到查询 B 上必须报错。**

    继续用它会从"另一个结果集里的某一行"往下取，**返回的行错位而不报错**。
    """
    page = _page(_rows(10), limit=4, fingerprint="fp-a")

    with pytest.raises(IngestError, match="不属于当前查询条件"):
        _page(_rows(10), limit=4, cursor=page.next_cursor, fingerprint="fp-b")


def test_a_cursor_whose_row_is_gone_is_rejected() -> None:
    """**账本已变化时游标失效。**

    定位哈希在当前快照里找不到（例如新导入改了顺序），继续用它会从一个无意义的
    位置切下去。
    """
    page = _page(_rows(10), limit=4)

    with pytest.raises(IngestError, match="不属于当前账本快照"):
        _page(["other1", "other2"], limit=4, cursor=page.next_cursor)


def test_a_cursor_pointing_at_the_last_row_yields_an_empty_page() -> None:
    """游标指向最后一行时返回空页且没有下一页——不是错误。"""
    page = _page(_rows(4), limit=10)
    assert page.next_cursor is None

    # 手工构造一个指向最后一行的游标。
    cursor = encode_trade_cursor(TradeCursor(transaction_hash="h3", filter_fingerprint="fp"))
    tail = _page(_rows(4), limit=10, cursor=cursor)

    assert tail.rows == ()
    assert tail.next_cursor is None


# --------------------------------------------------------------------------
# 明细族游标（realized / cashflow）
# --------------------------------------------------------------------------


def _detail(rows, *, limit, cursor=None, query="realized", fingerprint="fp"):
    """跑一次明细分页。"""
    from lyra.domain.analytics.cursor import paginate_detail

    return paginate_detail(
        rows, limit=limit, cursor=cursor, query=query,
        fingerprint=fingerprint, row_key_of=lambda r: r,
    )


def test_detail_cursor_round_trips() -> None:
    """明细游标编解码往返一致。"""
    from lyra.domain.analytics.cursor import DetailCursor, decode_detail_cursor, encode_detail_cursor

    cursor = DetailCursor(query="realized", row_key="rk", filter_fingerprint="f")

    assert decode_detail_cursor(encode_detail_cursor(cursor), "realized") == cursor


def test_detail_cursor_carries_the_query_discriminator() -> None:
    """载荷里有 ``query`` 判别字段——**这是与交易流水族的格式差异**。"""
    import base64, json

    from lyra.domain.analytics.cursor import DetailCursor, encode_detail_cursor

    encoded = encode_detail_cursor(
        DetailCursor(query="realized", row_key="rk", filter_fingerprint="f")
    )
    payload = json.loads(base64.urlsafe_b64decode(encoded + "==").decode())

    assert payload["query"] == "realized"
    assert payload["version"] == 1


def test_a_cursor_from_the_other_detail_family_is_rejected() -> None:
    """**把 realized 的游标用在 cashflow 上必须在游标层被拒。**

    这一条不依赖指纹是否恰好不同——载荷里的判别字段就直接拦住了。
    """
    from lyra.domain.analytics.cursor import decode_detail_cursor

    page = _detail(["k0", "k1", "k2", "k3"], limit=2)

    with pytest.raises(IngestError, match="不属于当前查询"):
        decode_detail_cursor(page.next_cursor, "cashflow")


def test_a_trade_cursor_cannot_be_used_as_a_detail_cursor() -> None:
    """**交易流水族的游标不能被当作明细游标使用。**

    两族载荷形状不同（前者有 ``transactionHash``，后者有 ``query`` + ``rowKey``），
    因此解析即失败。
    """
    trade_cursor = encode_trade_cursor(
        TradeCursor(transaction_hash="h1", filter_fingerprint="f")
    )

    with pytest.raises(IngestError):
        _detail(["k0", "k1", "k2"], limit=2, cursor=trade_cursor)


def test_detail_pages_are_contiguous() -> None:
    """**明细逐页取完不重复不遗漏。**"""
    rows = [f"k{i}" for i in range(8)]
    collected: list[str] = []
    cursor = None
    for _ in range(10):
        page = _detail(rows, limit=3, cursor=cursor)
        collected.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert collected == rows


def test_detail_fingerprint_includes_the_query_name() -> None:
    """**明细指纹包含查询名**——与交易流水族的指纹结构不同。

    因此即使两条命令的筛选条件完全相同，两族指纹也不会相同。
    """
    from lyra.domain.analytics.cursor import detail_filter_fingerprint

    realized = detail_filter_fingerprint("realized", ALL_SCOPE)
    cashflow = detail_filter_fingerprint("cashflow", ALL_SCOPE)

    assert realized != cashflow


def test_detail_fingerprint_is_stable_regardless_of_key_order() -> None:
    """**键序不影响指纹。**

    条件相同却得到不同指纹，会让合法的翻页请求被拒。
    """
    from lyra.domain.analytics.cursor import detail_filter_fingerprint

    a = detail_filter_fingerprint("realized", ALL_SCOPE, symbol="INTC", from_date="2026-01-01")
    b = detail_filter_fingerprint("realized", ALL_SCOPE, from_date="2026-01-01", symbol="INTC")

    assert a == b


def test_an_unregistered_detail_query_is_rejected() -> None:
    """未注册的查询名报错。"""
    from lyra.domain.analytics.cursor import detail_filter_fingerprint

    with pytest.raises(ValueError, match="未注册的明细查询"):
        detail_filter_fingerprint("nope", ALL_SCOPE)


def test_detail_fingerprint_mismatch_is_rejected() -> None:
    """指纹不符即拒——第二道防线。"""
    page = _detail(["k0", "k1", "k2"], limit=2, fingerprint="fp-a")

    with pytest.raises(IngestError, match="不属于当前查询条件"):
        _detail(["k0", "k1", "k2"], limit=2, cursor=page.next_cursor, fingerprint="fp-b")


def test_detail_cursor_whose_row_is_gone_is_rejected() -> None:
    """行键不在当前快照里即拒。"""
    page = _detail(["k0", "k1", "k2"], limit=2)

    with pytest.raises(IngestError, match="不属于当前账本快照"):
        _detail(["other"], limit=2, cursor=page.next_cursor)
