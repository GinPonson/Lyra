"""交易标准化与业务哈希。

镜像 TypeScript 版 ``transactions.ts``。**业务哈希是导入幂等与跨快照去重的
唯一依据**，所以这里的每个细节（字段顺序、分隔符、空值哨兵、十进制文本格式）
都必须与现有实现逐字节一致；任何偏差都会让同一笔真实交易在切换后被判为
两条，或让本应新增的交易被判为已存在。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal

from ..numeric import decimal_text
from .errors import IngestError
from .parsers import parse_date_field, parse_money, parse_option_symbol
from .types import (
    AssetType,
    LedgerSourceType,
    OptionType,
    ParsedTransaction,
    PositionAction,
    RawTransactionFields,
    TransactionAction,
)

# 美股标准期权一张合约对应 100 股。
OPTION_MULTIPLIER = 100
# 与 TypeScript 版本一致的金额勾稽最大误差，单位为美元。
AMOUNT_TOLERANCE = Decimal("0.011")

# 买入类交易净额必须为负数，并包含费用支出。
BUY_ACTIONS: frozenset[str] = frozenset({"Buy", "Buy to Open", "Buy to Close"})

# 参与 FIFO 重放的动作；集合同时用于把未知字符串收窄为领域类型。
POSITION_ACTIONS: frozenset[str] = frozenset({
    "Buy", "Sell", "Buy to Open", "Sell to Open",
    "Buy to Close", "Sell to Close", "Expired", "Assigned",
})

# 只影响资金流水、不直接改变持仓的动作。
CASH_ACTIONS: frozenset[str] = frozenset({
    "MoneyLink Transfer", "Credit Interest", "Qualified Dividend",
    "Non-Qualified Div", "Pr Yr Non-Qual Div", "NRA Tax Adj",
    "Pr Yr NRA Tax", "Foreign Tax Paid",
})

# 当前解析器明确接受的全部 Schwab 动作。
KNOWN_ACTIONS: frozenset[str] = POSITION_ACTIONS | CASH_ACTIONS

# 空值在哈希中的哨兵；必须与数值或文本 ``0`` 显式区分。
_NULL_SENTINEL = "<NULL>"


@dataclass(frozen=True)
class ParseTransactionContext:
    """解析单行所需的账本上下文。"""

    account: str
    source_hash: str
    source_type: LedgerSourceType
    source_reference: str
    sequence: int
    where: str


def parse_transaction_row(
    fields: RawTransactionFields,
    context: ParseTransactionContext,
) -> ParsedTransaction:
    """把一行 Schwab 原始字段转换为标准交易。

    完成动作白名单、日期、资产类型、期权字段与金额勾稽。任一规则失败都抛出
    :class:`IngestError`，上层必须中止整个文件，不能跳过当前行。

    @param fields: CSV 或 Excel 适配器提供的八个原始字段。
    @param context: 账户、来源哈希、物理顺序与错误位置。
    @returns occurrence 初始为 1 的标准交易；批次标准化器会重新编号。
    @raises IngestError: 动作、字段、期权格式或成交金额不符合规则。
    """
    action_text = fields.action.strip()
    if action_text not in KNOWN_ACTIONS:
        raise IngestError(
            f"{context.where}: 未知 Action {_quote(action_text)},请扩展 KNOWN_ACTIONS 后再导入"
        )
    action: TransactionAction = action_text  # type: ignore[assignment]

    parsed_date = parse_date_field(fields.date)
    raw_symbol = _empty_to_null(fields.symbol)
    description = _empty_to_null(fields.description)
    quantity = parse_money(fields.quantity)
    price = parse_money(fields.price)
    fees = parse_money(fields.fees)
    amount = parse_money(fields.amount)

    if action in CASH_ACTIONS:
        asset_type: AssetType = "cash"
        underlying: str | None = None
        expiry: str | None = None
        strike: Decimal | None = None
        option_type: OptionType | None = None
    else:
        if quantity is None:
            raise IngestError(
                f"{context.where}: 持仓类交易缺少数量 action={_quote(action)}"
            )

        option = parse_option_symbol(raw_symbol or "")
        if option is not None:
            asset_type = "option"
            underlying = option.underlying
            expiry = option.expiry
            strike = option.strike
            option_type = option.option_type
        else:
            requires_option_symbol = " to " in action or action in ("Expired", "Assigned")
            if requires_option_symbol:
                raise IngestError(
                    f"{context.where}: 期权 Symbol 格式无法解析: {_quote(raw_symbol)}"
                )
            asset_type = "stock"
            underlying = raw_symbol
            expiry = None
            strike = None
            option_type = None

        # 到期与指派在现有 Schwab 导出中没有价格与金额，因此只校验数量与合约。
        if action not in ("Expired", "Assigned"):
            if price is None or amount is None:
                raise IngestError(
                    f"{context.where}: 交易行缺少价格或金额 action={_quote(action)}"
                )
            _reconcile_amount(
                action=action,
                quantity=quantity,
                price=price,
                fees=fees if fees is not None else Decimal(0),
                amount=amount,
                multiplier=OPTION_MULTIPLIER if asset_type == "option" else 1,
                where=context.where,
            )

    business_hash = create_business_hash(
        account=context.account,
        record_date=parsed_date.record_date,
        transaction_date=parsed_date.transaction_date,
        action=action,
        raw_symbol=raw_symbol,
        description=description,
        quantity_text=decimal_hash_text(fields.quantity),
        price_text=decimal_hash_text(fields.price),
        fees_text=decimal_hash_text(fields.fees),
        amount_text=decimal_hash_text(fields.amount),
    )

    return ParsedTransaction(
        account=context.account,
        source_type=context.source_type,
        source_reference=_require_source_reference(context.source_reference, context.where),
        record_date=parsed_date.record_date,
        transaction_date=parsed_date.transaction_date,
        action=action,
        raw_symbol=raw_symbol,
        description=description,
        asset_type=asset_type,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        quantity=quantity,
        price=price,
        fees=fees,
        amount=amount,
        source_hash=context.source_hash,
        sequence=context.sequence,
        occurrence=1,
        business_hash=business_hash,
        transaction_hash=hash_text(f"{business_hash}|1"),
    )


def with_occurrence(transaction: ParsedTransaction, occurrence: int) -> ParsedTransaction:
    """为同一文件内业务字段完全相同的交易设置稳定出现次数。

    @param transaction: 已标准化的交易。
    @param occurrence: 从 1 开始的相同业务交易出现次数。
    @returns 更新 occurrence 与交易哈希后的新对象。
    @raises ValueError: occurrence 不是正整数。
    """
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise ValueError("occurrence 必须是从 1 开始的整数")
    return replace(transaction, occurrence=occurrence, transaction_hash=hash_text(
        f"{transaction.business_hash}|{occurrence}"
    ))


def decimal_hash_text(raw: str) -> str | None:
    """生成参与业务哈希的十进制文本。

    小数位数取自**清洗后的源文本**，这样 ``$5.00`` 不会被规范化为 ``5``：
    哈希必须对同一次 Schwab 导出的原始书写形式保持稳定。

    与 TypeScript 版的两点刻意对齐：

    1. ``-0.00`` 归一到 ``0.00``（decimal.js 不保留负零）；
    2. 科学计数法分支**不做猜测性对齐**——它不是已确认的 Schwab 格式，
       TypeScript 侧同样未做跨语言验证，此处直接给出 Python 的规范文本。

    @param raw: 原始单元格文本。
    @returns 哈希文本；空字段返回 ``None``。
    """
    normalized = raw.strip().replace("$", "").replace(",", "")
    if normalized == "":
        return None

    value = Decimal(normalized)
    if "e" in normalized or "E" in normalized:
        return str(value)

    dot_index = normalized.find(".")
    scale = 0 if dot_index < 0 else len(normalized) - dot_index - 1
    quantized = value.quantize(Decimal(1).scaleb(-scale))
    if quantized.is_zero():
        # 与 decimal.js 一致：去掉负零的符号，否则 -0.00 与 0.00 会产出不同哈希。
        quantized = quantized.copy_abs()
    return format(quantized, "f")


def create_business_hash(
    *,
    account: str,
    record_date: str,
    transaction_date: str,
    action: str,
    raw_symbol: str | None,
    description: str | None,
    quantity_text: str | None,
    price_text: str | None,
    fees_text: str | None,
    amount_text: str | None,
) -> str:
    """构造不依赖文件名与物理行号的业务指纹。

    @returns 64 位小写十六进制 SHA-256。
    """
    parts = [
        account,
        record_date,
        transaction_date,
        action,
        _hash_value(raw_symbol),
        _hash_value(description),
        _hash_value(quantity_text),
        _hash_value(price_text),
        _hash_value(fees_text),
        _hash_value(amount_text),
    ]
    return hash_text("|".join(parts))


def hash_text(value: str) -> str:
    """使用 SHA-256 生成小写十六进制文本。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reconcile_amount(
    *,
    action: PositionAction,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    amount: Decimal,
    multiplier: int,
    where: str,
) -> None:
    """完成持仓交易净额与数量、价格、乘数、费用之间的勾稽。

    @raises IngestError: 净额与期望值之差超过 ``AMOUNT_TOLERANCE``。
    """
    gross = quantity * price * multiplier
    expected = -(gross + fees) if action in BUY_ACTIONS else gross - fees
    if abs(amount - expected) > AMOUNT_TOLERANCE:
        raise IngestError(
            f"{where}: 金额勾稽不符 action={_quote(action)} "
            f"qty={decimal_text(quantity)} price={decimal_text(price)} "
            f"fees={decimal_text(fees)} amount={decimal_text(amount)} "
            f"(期望 {decimal_text(expected)})"
        )


def _require_source_reference(value: str, where: str) -> str:
    """来源引用用于审计与查询，禁止空值进入标准交易。"""
    normalized = value.strip()
    if normalized == "":
        raise IngestError(f"{where}: 来源引用不能为空")
    return normalized


def _empty_to_null(raw: str) -> str | None:
    """去除字段两端空白，并把空文本统一为账本 ``None``。"""
    value = raw.strip()
    return None if value == "" else value


def _hash_value(value: str | None) -> str:
    """显式区分空值与数值或文本 ``0``，保持哈希语义。"""
    return _NULL_SENTINEL if value is None else value


def _quote(value: str | None) -> str:
    """生成接近 Python ``repr`` 的简洁字段显示，仅用于不含敏感信息的导入错误。"""
    if value is None:
        return "None"
    return "'" + value.replace("'", "\\'") + "'"
