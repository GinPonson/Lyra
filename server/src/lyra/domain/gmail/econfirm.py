"""Schwab eConfirm 邮件的识别。

镜像 TypeScript 版 ``domain/gmail/econfirm.ts`` 的常量与消息类型部分。

**发件人模式刻意写得严格**：邮件地址必须位于开头、``<`` 之后或空白之后，且以
``>``、空白或结尾结束。宽松的 ``包含即匹配`` 会让 ``evil@mail.schwab.com.attacker.net``
这种伪造地址通过——**它包含目标字符串但不是同一个地址**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from ..ledger.errors import IngestError
from ..ledger.transactions import create_business_hash, decimal_hash_text
from ..ledger.types import ParsedOptionSymbol, ParsedTransaction, RawTransactionFields

SCHWAB_ECONFIRM_SENDER = "donotreply@mail.schwab.com"
SCHWAB_ECONFIRM_SUBJECT = "Schwab eConfirms"

# 目标地址必须是**整个地址**，不能只是被包含。
SCHWAB_SENDER_PATTERN = re.compile(
    r"(?:^|[<\s])donotreply@mail\.schwab\.com(?:>|\s|$)", re.IGNORECASE
)


@dataclass(frozen=True)
class GmailMessage:
    """一封已解析的 Gmail 邮件。"""

    message_id: str
    sender: str
    subject: str
    body: str
    email_timestamp: str | None
    body_candidates: tuple[str, ...] = ()


class UnresolvedCashOptionSaleError(IngestError):
    """期权 Sale/Cash 缺少唯一、精确的既有成交事实关联。"""

    def __init__(self, message: str, *, reason: str = "unresolved") -> None:
        """保留供同封 MIME 桥接判断的失败类别，不暴露交易内容。

        @param message: 不含成交数值的安全诊断。
        @param reason: ``no_match``、``ambiguous``、``mixed_account``、桥接不匹配等内部原因。
        """
        super().__init__(message)
        self.reason = reason


def is_schwab_sender(sender: str) -> bool:
    """判断发件人是否来自 Schwab 通知地址。

    @param sender: ``From`` 头文本（可能是 ``Name <addr>`` 形式）。
    @returns 是否匹配。
    """
    return SCHWAB_SENDER_PATTERN.search(sender) is not None


SubjectKind = Literal["econfirm", "other"]


def classify_subject(subject: str) -> SubjectKind:
    """判断主题是否为 eConfirms 通知。

    **用包含判断而不是相等**：Schwab 会在主题后追加批次信息。

    @param subject: 主题文本。
    @returns ``econfirm`` 或 ``other``。
    """
    return "econfirm" if SCHWAB_ECONFIRM_SUBJECT in subject else "other"


@dataclass(frozen=True)
class EmailTrade:
    """从 Schwab eConfirm 确定性提取的一笔成交。"""

    trade_date: str
    action: str
    symbol: str
    description: str
    quantity: str
    price: str
    fees: str
    amount: str
    # 保留表格中的本金，以便 MIME 候选一致性校验不忽略容差内的金额差异。
    principal: str | None = None
    # 这是导入控制元数据，不是交易字段；匹配既有事实的成交不得再次入账。
    existing_only: bool = False


class _CashOptionSaleBridgeContext:
    """仅供同一封邮件的候选重跑使用的既有成交桥接快照。

    每个成功批次中的既有现金期权卖出只能被一个失败候选行消费，防止新成交借用
    其它行的已验证结果。该对象只在一次 ``parse_econfirm`` 调用内创建。
    """

    def __init__(self, source_trades: tuple[EmailTrade, ...]) -> None:
        """创建只含严格匹配既有事实的候选桥接快照。

        @param source_trades: 唯一成功批次中的成交；仅保留 ``existing_only`` 行。
        """
        self.trades = tuple(trade for trade in source_trades if trade.existing_only)
        self._used_indexes: set[int] = set()

    @property
    def fully_consumed(self) -> bool:
        """判断失败候选是否逐行对应了全部既有成交桥接行。"""
        return len(self._used_indexes) == len(self.trades)

    def reconcile(
        self,
        trade: EmailTrade,
        option: ParsedOptionSymbol,
        strict_match: EmailTrade | None,
        where: str,
    ) -> EmailTrade:
        """将失败候选的 Cash 行与一条唯一已验证事实对齐。

        除数量与价格允许仅有小数尾随零精度差异外，日期、原始符号、规范描述、费用、
        净额和期权完整身份必须完全相同；若严格解析已得到动作，也必须与桥接动作一致。

        @param trade: 按 Sell 方向解析并已完成金额勾稽的候选行。
        @param option: 候选行完整期权身份。
        @param strict_match: 原业务哈希解析结果；未匹配时为 ``None``。
        @param where: 安全的错误定位文本。
        @returns 仅替换数量/价格精度并保留 ``existing_only=True`` 的成交。
        @raises UnresolvedCashOptionSaleError: 未找到唯一兼容桥接行或动作冲突。
        """
        matches: list[tuple[int, EmailTrade]] = []
        for index, bridge in enumerate(self.trades):
            if index in self._used_indexes or not _cash_sale_bridge_matches(
                trade, option, bridge
            ):
                continue
            if strict_match is not None and strict_match.action != bridge.action:
                continue
            matches.append((index, bridge))

        if not matches:
            raise UnresolvedCashOptionSaleError(
                f"{where}: 期权 Sale/Cash 与同封已验证成交不一致",
                reason="bridge_mismatch",
            )

        # 重复填单可能有相同内容；只有最终语义完全一致时按顺序消费一个副本。
        fingerprints = {trades_fingerprint((bridge,)) for _, bridge in matches}
        if len(fingerprints) != 1:
            raise UnresolvedCashOptionSaleError(
                f"{where}: 期权 Sale/Cash 对应多个不同的已验证成交",
                reason="ambiguous_bridge",
            )

        index, bridge = matches[0]
        self._used_indexes.add(index)
        return replace(
            trade,
            action=bridge.action,
            quantity=bridge.quantity,
            price=bridge.price,
            existing_only=True,
        )


def email_trade_to_raw_fields(trade: EmailTrade) -> RawTransactionFields:
    """把一笔邮件成交转换为与 CSV 导入相同的领域字段对象。

    **必须与 CSV 解析器产出的字段逐字符一致**，否则同一笔交易经邮件导入与经 CSV
    导入会得到不同的交易哈希，跨来源去重失效——用户会看到同一笔交易出现两次。

    三处格式约定：

    - ``Date`` 用 ``MM/DD/YYYY``（**零填充**，与 Schwab 的展示格式一致）；
    - ``Fees & Comm`` **为零时留空**，而不是 ``$0``；
    - ``Amount`` 的**符号位在 ``$`` 之前**（``-$12.34``），不是 ``$-12.34``。

    @param trade: 邮件中提取的成交。
    @returns 领域层的原始字段对象（**与 CSV 适配器产出同一类型**）。
    @raises ValueError: 成交日期不是 ``YYYY-MM-DD``。

    ``existing_only`` 是解析控制标记，不属于账本行字段，因此不会随此转换泄漏。
    """
    parts = trade.trade_date.split("-")
    if len(parts) != 3:
        raise ValueError(f"邮件成交日期无法格式化: {trade.trade_date}")
    year, month, day = parts
    return RawTransactionFields(
        date=f"{month}/{day}/{year}",
        action=trade.action,
        symbol=trade.symbol,
        description=trade.description,
        quantity=_plain_decimal(trade.quantity),
        price=f"${_plain_decimal(trade.price)}",
        # 零费用留空：写 $0 会改变行哈希，破坏跨来源一致性。
        fees="" if _is_zero(trade.fees) else f"${_plain_decimal(trade.fees)}",
        amount=_signed_usd(trade.amount),
    )


def _plain_decimal(value: object) -> str:
    """渲染为无指数的十进制文本（``toFixed()`` 的等价物）。"""
    from decimal import Decimal

    try:
        return format(Decimal(value), "f")
    except Exception:  # noqa: BLE001 - 非法输入不构成可用数值
        return str(value)


def _plain_decimal_text(value: str) -> str:
    """渲染为无指数的十进制文本。

    无法解析的文本原样返回——**不猜**。
    """
    from decimal import Decimal, InvalidOperation

    try:
        return format(Decimal(value), "f")
    except (InvalidOperation, ValueError):
        return value


def _is_zero(value: str) -> bool:
    """判断十进制文本是否为零。"""
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(value) == 0
    except (InvalidOperation, ValueError):
        return False


def _signed_usd(value: str) -> str:
    """输出符号位位于美元符号之前的金额文本。"""
    from decimal import Decimal, InvalidOperation

    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError):
        return value
    # 符号位在 $ 之前：`-$12.34` 而不是 `$-12.34`。
    prefix = "-" if decimal_value < 0 else ""
    return f"{prefix}${_plain_decimal(str(abs(decimal_value)))}"


# ---------------------------------------------------------------------------
# eConfirm 解析：入口层
# ---------------------------------------------------------------------------

# "Account ending in 276" / "Account ending: 276" 两种写法。
ACCOUNT_SUFFIX_PATTERN = re.compile(
    r"Account\s+ending(?:\s+in\s+|\s*:\s*)([A-Za-z0-9]+)", re.IGNORECASE
)

# 期权动作由「买卖」与「保证金/做空」两个维度共同决定。
OPTION_ACTIONS: dict[str, str] = {
    "Purchase|Margin": "Buy to Open",
    "Sale|Margin": "Sell to Close",
    "Sale|Short": "Sell to Open",
    "Purchase|Short": "Buy to Close",
}

# **股票不区分 Open/Close**，仅由买卖方向决定动作。
STOCK_ACTIONS: dict[str, str] = {
    "Purchase": "Buy",
    "Sale": "Sell",
}


def candidate_bodies(message: GmailMessage) -> tuple[str, ...]:
    """按顺序返回去重后的正文候选。

    顺序是「先纯文本、后 HTML」——那是供应商给出的原文，不经转换。

    @param message: 已解析的邮件。
    @returns 非空且去重的候选正文。
    """
    unique: list[str] = []
    for candidate in (message.body, *message.body_candidates):
        if candidate.strip() and candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def extract_account_suffix(message: GmailMessage) -> str:
    """从主题与全部正文候选提取唯一的账户尾号。

    **0 个或 2 个以上不同尾号都报错。**

    多个尾号意味着主题与正文对"这笔交易属于哪个账户"给出了矛盾的说法；**猜一个会把
    交易写进错误的账本**，而更正它意味着删除已入账的交易并重算 FIFO。

    @param message: 已解析的邮件。
    @returns 唯一的账户尾号。
    @raises IngestError: 没有尾号，或不同来源给出冲突的尾号。
    """
    from ..ledger.errors import IngestError

    suffixes: set[str] = set()
    for source in (message.subject, *candidate_bodies(message)):
        suffixes.update(
            match.group(1) for match in ACCOUNT_SUFFIX_PATTERN.finditer(source)
        )
    if not suffixes:
        raise IngestError(f"邮件 {message.message_id}: 缺少 Account ending")
    if len(suffixes) != 1:
        raise IngestError(
            f"邮件 {message.message_id}: Subject 与正文中的账户尾号不一致: "
            f"{sorted(suffixes)}"
        )
    return next(iter(suffixes))


def trades_fingerprint(trades: tuple[EmailTrade, ...]) -> str:
    """对一批成交生成稳定指纹。

    用于判断同一封邮件的纯文本与 HTML 版本是否解析出**同一批交易**。

    @param trades: 成交序列。
    @returns JSON 指纹。
    """
    import json

    return json.dumps(
        [
            {
                "tradeDate": trade.trade_date, "action": trade.action,
                "symbol": trade.symbol, "description": trade.description,
                "quantity": trade.quantity, "price": trade.price,
                "principal": trade.principal,
                "fees": trade.fees, "amount": trade.amount,
                "existingOnly": trade.existing_only,
            }
            for trade in trades
        ],
        separators=(",", ":"), ensure_ascii=False,
    )


def _trim_decimal_fraction_zeros(value: str) -> str:
    """仅移除十进制小数部分末尾的零，用于比较展示精度差异。

    @param value: 已由邮件解析器规范化的十进制文本。
    @returns 去掉小数末尾零后的文本；整数部分与其他字符保持不变。
    """
    if "." not in value:
        return value
    integer, fraction = value.split(".", 1)
    fraction = fraction.rstrip("0")
    return integer if not fraction else f"{integer}.{fraction}"


def _cash_sale_bridge_matches(
    trade: EmailTrade,
    option: ParsedOptionSymbol,
    bridge: EmailTrade,
) -> bool:
    """检查 Cash 行是否只存在数量或价格的小数尾零展示差异。

    @param trade: 当前 MIME 候选中按现金卖出方向勾稽的行。
    @param option: 当前候选的完整期权身份。
    @param bridge: 唯一成功候选中已严格命中既有事实的行。
    @returns 所有字段相同且数量/价格至多有尾随零精度差异时为真。
    """
    from ..ledger.parsers import parse_option_symbol

    bridge_option = parse_option_symbol(bridge.symbol)
    if (
        not bridge.existing_only
        or bridge.action not in ("Sell to Open", "Sell to Close")
        or trade.action != "Sell"
        or trade.trade_date != bridge.trade_date
        or trade.symbol != bridge.symbol
        or trade.description != bridge.description
        or trade.principal != bridge.principal
        or bridge_option is None
        or option.underlying != bridge_option.underlying
        or option.expiry != bridge_option.expiry
        or option.strike != bridge_option.strike
        or option.option_type != bridge_option.option_type
        or trade.fees != bridge.fees
        or trade.amount != bridge.amount
    ):
        return False

    try:
        quantity_equal = Decimal(trade.quantity) == Decimal(bridge.quantity)
        price_equal = Decimal(trade.price) == Decimal(bridge.price)
    except InvalidOperation:
        return False
    return (
        quantity_equal
        and price_equal
        and _trim_decimal_fraction_zeros(trade.quantity)
        == _trim_decimal_fraction_zeros(bridge.quantity)
        and _trim_decimal_fraction_zeros(trade.price)
        == _trim_decimal_fraction_zeros(bridge.price)
    )


def _cash_option_sale_blocks(body: str) -> tuple[str, ...]:
    """提取候选正文中所有需要专门证明的期权 Sale/Cash 块。

    @param body: 单个 MIME 正文候选。
    @returns 原顺序的 Cash 交易块片段，不含 ``Symbol:`` 分隔行。
    """
    from ..ledger.parsers import parse_option_symbol

    parts = SYMBOL_SPLIT_PATTERN.split(body)
    cash_blocks: list[str] = []
    for raw_block in parts[1:]:
        block = raw_block.split(ADDITIONAL_INFORMATION_MARKER, 1)[0]
        try:
            action = field(block, "Action", "候选扫描")
            position_type = field(block, "Type", "候选扫描")
        except IngestError:
            continue
        first_line = next(
            (line.strip() for line in block.replace("\r", "").split("\n") if line.strip()),
            "",
        )
        if (
            action == "Sale"
            and position_type == "Cash"
            and parse_option_symbol(unwrap_symbol(first_line)) is not None
        ):
            cash_blocks.append(raw_block)
    return tuple(cash_blocks)


def parse_econfirm(
    message: GmailMessage,
    *,
    existing_option_sales: tuple[ParsedTransaction, ...] = (),
) -> tuple[EmailTrade, ...]:
    """从全部正文候选中确定性提取成交。

    **两个候选必须解析出同一批交易。** 纯文本与 HTML 是同一封邮件的两种表示，
    若结果不同说明至少有一方解析错了——**此时不能挑一个"看起来更完整"的用**，
    那会在用户毫无察觉的情况下写入错误数据。

    @param message: 已解析的邮件。
    @param existing_option_sales: 同一账户的既有期权卖出快照，仅用于解析 Sale/Cash。
    @returns 成交序列。
    @raises UnresolvedCashOptionSaleError: Sale/Cash 无法唯一匹配既有成交。
    @raises IngestError: 全部候选失败，或候选之间结果不一致。
    """
    successes: dict[str, tuple[EmailTrade, ...]] = {}
    failures: list[tuple[int, str, IngestError]] = []
    errors: list[str] = []
    for index, body in enumerate(candidate_bodies(message)):
        try:
            trades = parse_econfirm_body(
                message.message_id,
                body,
                existing_option_sales=existing_option_sales,
            )
        except IngestError as error:
            # 单个候选失败不致命：另一个候选可能解析得出来。
            failures.append((index, body, error))
            errors.append(f"候选{index + 1}: {error}")
            continue
        successes[trades_fingerprint(trades)] = trades

    if len(successes) > 1:
        raise IngestError(
            f"邮件 {message.message_id}: plain 与 HTML 解析出的交易不一致"
        )

    if not successes:
        unresolved_errors = [
            error for _, _, error in failures
            if isinstance(error, UnresolvedCashOptionSaleError)
        ]
        if unresolved_errors and len(failures) == 1:
            raise unresolved_errors[0]
        if unresolved_errors:
            # 保留未知动作的专用类型，同时把其他候选错误一并呈现，避免只报其中一方。
            raise UnresolvedCashOptionSaleError(
                f"邮件 {message.message_id}: 正文候选包含未解决的 Sale/Cash 或解析失败: "
                f"{'；'.join(errors)}"
            )
        raise IngestError(
            f"邮件 {message.message_id}: 所有正文候选解析失败: {'；'.join(errors)}"
        )

    canonical = next(iter(successes.values()))
    bridge_rows = tuple(trade for trade in canonical if trade.existing_only)
    for index, body, original_error in failures:
        cash_blocks = _cash_option_sale_blocks(body)
        if not cash_blocks:
            if isinstance(original_error, UnresolvedCashOptionSaleError):
                raise original_error
            # 维持既有候选行为：与交易动作无关的单候选解析失败可由另一 MIME 候选兜底。
            continue

        if not bridge_rows:
            raise UnresolvedCashOptionSaleError(
                f"邮件 {message.message_id}: plain 与 HTML 候选无法一致验证 Sale/Cash "
                f"(候选{index + 1}含未验证的期权 Sale/Cash)"
            )

        # 先独立校验整封候选中所有 Cash 行，避免普通块的早期格式错误掩盖后续新 Cash 行。
        cash_only_body = "Symbol:\n" + "\nSymbol:\n".join(cash_blocks)
        verification_context = _CashOptionSaleBridgeContext(canonical)
        try:
            parse_econfirm_body(
                message.message_id,
                cash_only_body,
                existing_option_sales=existing_option_sales,
                _bridge_context=verification_context,
            )
        except UnresolvedCashOptionSaleError:
            raise
        except IngestError as error:
            raise UnresolvedCashOptionSaleError(
                f"邮件 {message.message_id}: 候选{index + 1}的 Sale/Cash 无法全部验证"
            ) from error
        if not verification_context.fully_consumed:
            raise UnresolvedCashOptionSaleError(
                f"邮件 {message.message_id}: 候选{index + 1}没有验证全部既有 Sale/Cash"
            )

        bridge_context = _CashOptionSaleBridgeContext(canonical)
        try:
            retried = parse_econfirm_body(
                message.message_id,
                body,
                existing_option_sales=existing_option_sales,
                _bridge_context=bridge_context,
            )
        except UnresolvedCashOptionSaleError:
            raise
        except IngestError:
            # Cash 行已独立全部验证；普通块仍有结构错误时，按旧规则使用成功候选。
            continue

        if not bridge_context.fully_consumed:
            raise UnresolvedCashOptionSaleError(
                f"邮件 {message.message_id}: 候选{index + 1}没有验证全部既有 Sale/Cash"
            )
        if trades_fingerprint(retried) != trades_fingerprint(canonical):
            raise IngestError(
                f"邮件 {message.message_id}: plain 与 HTML 解析出的交易不一致"
            )

    return canonical


# ---------------------------------------------------------------------------
# eConfirm 解析：字段与格式
# ---------------------------------------------------------------------------

# 交易块的分隔：一行 `Symbol:` 后换行。第一段是前言，不是交易块。
SYMBOL_SPLIT_PATTERN = re.compile(r"(?:^|\n)\s*Symbol:\s*\n", re.IGNORECASE)

# Markdown 链接形式的符号：`[INTC](https://...)`。
_MARKDOWN_SYMBOL = re.compile(r"^\[([^\]]+)]\([^\n]+\)")

# 期权描述里附加的过期/行权价后缀，重建规范描述时要剥掉。
_OPTION_DESCRIPTION_SUFFIX = re.compile(
    r"\s+\d{2}/\d{2}/\d{4}\s+\$[\d.]+\s+(?:Call|Put)$", re.IGNORECASE
)


def field(block: str, label: str, where: str) -> str:
    """从交易块读取一个单行字段。

    模式要求 ``标签:`` 独占一行，值在下一行——这正是 eConfirm 的排版。
    用"标签后紧跟冒号"匹配会在正文里出现同名普通词时误命中。

    @param block: 交易块文本。
    @param label: 字段名。
    @param where: 错误定位说明。
    @returns 去空白后的字段值。
    @raises IngestError: 字段缺失。
    """
    from ..ledger.errors import IngestError

    pattern = re.compile(
        rf"(?:^|\n)\s*{re.escape(label)}:\s*\n+\s*([^\n]+)", re.IGNORECASE
    )
    match = pattern.search(block)
    if match is None:
        raise IngestError(f"{where}: 缺少字段 '{label}'")
    return match.group(1).strip()


def unwrap_symbol(raw: str) -> str:
    """取出符号原文。

    邮件正文里符号可能被渲染成 Markdown 链接（``[INTC](https://…)``）；
    直接把这个整体当成符号会让下游解析出错误的代码。

    @param raw: 首行原文。
    @returns 符号。
    """
    trimmed = raw.strip()
    match = _MARKDOWN_SYMBOL.match(trimmed)
    return (match.group(1) if match is not None else trimmed).strip()


def parse_trade_date(raw: str, where: str) -> str:
    """把 ``M/D/YYYY`` 转为 ISO 日期。

    **两位数年份按 20xx 解释**（Schwab 的展示格式），并**逐字段回代校验**——
    ``2/30/2026`` 这种日期格式合法但不存在，回代校验能拦住它。

    @param raw: 日期原文。
    @param where: 错误定位说明。
    @returns ``YYYY-MM-DD``。
    @raises IngestError: 无法解析或日期不存在。
    """
    import datetime as _datetime

    from ..ledger.errors import IngestError

    parts = raw.strip().split("/")
    if len(parts) != 3:
        raise IngestError(f"{where}: 无法解析交易日期 '{raw}'")
    try:
        month, day, year_value = (int(part) for part in parts)
    except ValueError as error:
        raise IngestError(f"{where}: 无法解析交易日期 '{raw}'") from error
    year = year_value + 2000 if year_value < 100 else year_value
    try:
        parsed = _datetime.date(year, month, day)
    except ValueError as error:
        # 回代校验：date() 本身就会拒绝不存在的日期。
        raise IngestError(f"{where}: 无法解析交易日期 '{raw}'") from error
    return f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"


def parse_email_decimal(raw: str, where: str) -> Any:
    """把邮件里的数值文本转为 Decimal。

    剥掉货币符号与千分位逗号；**不剥负号**——负号是金额语义的一部分。

    @param raw: 数值原文。
    @param where: 错误定位说明。
    @returns ``Decimal``。
    @raises IngestError: 无法解析。
    """
    from decimal import Decimal, InvalidOperation

    from ..ledger.errors import IngestError

    text = raw.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise IngestError(f"{where}: 无法解析数值 '{raw}'") from error


def normalize_description(symbol: str, security_description: str) -> str:
    """规范化证券描述。

    **正股原样保留；期权重建为规范形式**：``CALL|PUT {公司} ${行权价} EXP MM/DD/YY``。

    重建是必要的：Schwab 的描述格式在不同批次间会变，而描述会进入交易哈希——
    不规范化会让同一笔交易在两次导入中算出不同的哈希，跨来源去重随之失效。

    @param symbol: 已解析的符号。
    @param security_description: ``Security Description`` 字段。
    @returns 规范化描述。
    @raises IngestError: 期权到期日无法格式化。
    """
    from ..ledger.errors import IngestError
    from ..ledger.parsers import parse_option_symbol

    option = parse_option_symbol(symbol)
    if option is None:
        return security_description.strip()
    company = _OPTION_DESCRIPTION_SUFFIX.sub("", security_description.strip())
    kind = "CALL" if option.option_type == "C" else "PUT"
    parts = option.expiry.split("-")
    if len(parts) != 3:
        raise IngestError(f"期权到期日无法格式化: {option.expiry}")
    year, month, day = parts
    strike = _plain_decimal(option.strike)
    return f"{kind} {company} ${strike} EXP {month}/{day}/{year[2:]}"


# 勾稽容差：Schwab 的金额按分显示，`数量 × 价格 × 乘数` 与本金之间会有舍入差。
# 0.011 是参考实现取的阈值——小于一分半，足以吸收舍入而拦得住真实错误。
RECONCILIATION_TOLERANCE = Decimal("0.011")

BUY_ACTIONS = ("Buy", "Buy to Open", "Buy to Close")


def build_trade(
    *,
    trade_date: str,
    action: str,
    symbol: str,
    description: str,
    multiplier: int,
    quantity: str,
    price: str,
    principal: str,
    fees: Any,
    amount: str,
    where: str,
) -> EmailTrade:
    """构造一笔成交，并做两处勾稽校验。

    **校验一：本金 = 数量 × 价格 × 乘数。**

    **校验二：净额 = 本金 ± 费用**（买入加、卖出减）。

    两处都用 0.011 的容差吸收显示舍入；**超出容差即报错而不是修正**——修正会让账本
    记下一个从未发生过的数字，而用户看到的是"金额对上了"。

    **买入的 ``amount`` 取负**：现金流出为负是账本的符号约定；不取负会让买入看起来
    像收入，现金流与持仓方向同时反了。

    @param trade_date: 已解析的交易日期。
    @param action: 已映射的动作。
    @param symbol: 符号。
    @param description: 规范化描述。
    @param multiplier: 1（正股）或 100（期权）。
    @param quantity: 数量原文。
    @param price: 价格原文。
    @param principal: 本金原文。
    @param fees: 已解析的费用。
    @param amount: 成交净额原文。
    @param where: 错误定位说明。
    @returns 一笔成交。
    @raises IngestError: 两处勾稽任一不符。
    """
    from ..ledger.errors import IngestError

    parsed_quantity = parse_email_decimal(quantity, where)
    parsed_price = parse_email_decimal(price, where)
    parsed_principal = parse_email_decimal(principal, where)
    net_amount = parse_email_decimal(amount, where)

    expected_principal = parsed_quantity * parsed_price * Decimal(multiplier)
    if abs(parsed_principal - expected_principal) > RECONCILIATION_TOLERANCE:
        raise IngestError(
            f"{where}: Principal 勾稽不符，实际 {parsed_principal}，期望 {expected_principal}"
        )

    is_buy = action in BUY_ACTIONS
    expected_net = (
        parsed_principal + fees if is_buy else parsed_principal - fees
    )
    if abs(net_amount - expected_net) > RECONCILIATION_TOLERANCE:
        raise IngestError(
            f"{where}: Total Amount 勾稽不符，实际 {net_amount}，期望 {expected_net}"
        )

    return EmailTrade(
        trade_date=trade_date,
        action=action,
        symbol=symbol,
        description=description,
        quantity=_plain_decimal(parsed_quantity),
        price=_plain_decimal(parsed_price),
        principal=_plain_decimal(parsed_principal),
        fees=_plain_decimal(fees),
        # 买入为负：现金流出。
        amount=_plain_decimal(-net_amount if is_buy else net_amount),
    )


# eConfirm 支持的美元文本：可带负号、千分位与小数。
MONEY_PATTERN = re.compile(r"^-?\$[\d,]+(?:\.\d+)?$")


def is_money(value: object) -> bool:
    """判断字段是否为 eConfirm 支持的美元文本。

    **必须带 ``$``**：不带货币符号的数字在正文里随处可见（日期、数量、行号），
    把它们当金额会让解析器在错误的字段上对齐。
    """
    return isinstance(value, str) and MONEY_PATTERN.fullmatch(value) is not None


def parse_labeled_fees(values: tuple[str, ...], where: str) -> tuple[Decimal, str]:
    """解析费用区域，返回 ``(费用, 成交净额)``。

    识别三种模板：

    1. **零费用行**：``["n/a:", money, "n/a", money]``——股票多价格成交的 plain 模板
       在每条腿后保留 N/A 标签但不给 Commission / Industry Fee 标签；
    2. **只有 Industry Fee**：``industry fee:`` 开头，佣金按 0 计；
    3. **完整模板**：``commission:`` + ``industry fee:`` + ``total:``。

    **分项费用必须等于总费用**（``commission + industry_fee == total``）。不等时报错
    而不是采信某一方——采信哪一边都是在猜，而费用会进入交易哈希与已实现损益。

    **尾部不得有多余字段**（``index != len(values)`` 即报错）：模板若新增了字段，
    静默忽略会让解析结果悄悄漏掉信息。

    @param values: 费用区域的各行。
    @param where: 错误定位说明。
    @returns ``(费用, 成交净额原文)``。
    @raises IngestError: 结构不符或勾稽失败。
    """
    from ..ledger.errors import IngestError

    # 模板一：零费用行。
    if (
        len(values) == 4
        and values[0].lower() == "n/a:"
        and values[2].lower() == "n/a"
        and is_money(values[1])
        and is_money(values[3])
    ):
        fees = parse_email_decimal(values[1], where)
        if fees != 0:
            raise IngestError(f"{where}: N/A 费用必须为 0，实际 {fees}")
        return fees, values[3]

    index = 0
    industry_only = values[0].lower() == "industry fee:"
    if industry_only:
        # 只有 Industry Fee 时佣金按 0 计——这是模板的约定，不是猜测。
        commission = Decimal(0)
    elif values[0].lower() == "commission:":
        index += 1
        raw_commission = values[index] if index < len(values) else None
        if not is_money(raw_commission):
            raise IngestError(f"{where}: Commission 后缺少金额")
        commission = parse_email_decimal(raw_commission, where)  # type: ignore[arg-type]
        index += 1
        if index < len(values) and values[index].lower() == "commission":
            # 完整模板会在金额后再给一个裸标签。
            index += 1
    elif is_money(values[0]):
        commission = parse_email_decimal(values[0], where)
        index += 1
    else:
        raise IngestError(f"{where}: 无法识别 Commission 字段: {list(values)}")

    if index >= len(values) or values[index].lower() != "industry fee:":
        raise IngestError(f"{where}: 缺少 Industry Fee 标签")
    index += 1
    raw_industry = values[index] if index < len(values) else None
    if not is_money(raw_industry):
        raise IngestError(f"{where}: Industry Fee 后缺少金额")
    industry_fee = parse_email_decimal(raw_industry, where)  # type: ignore[arg-type]
    index += 1

    if industry_only:
        if index < len(values) and values[index].lower() == "industry fee":
            index += 1
        amount = values[index] if index < len(values) else None
        if not is_money(amount):
            raise IngestError(f"{where}: Industry Fee 后缺少成交净额")
        index += 1
        if index != len(values):
            raise IngestError(
                f"{where}: 费用表包含未知字段: {list(values[index:])}"
            )
        return industry_fee, amount  # type: ignore[return-value]

    if index >= len(values) or values[index].lower() != "total:":
        raise IngestError(f"{where}: 缺少费用 Total 标签")
    index += 1
    raw_total = values[index] if index < len(values) else None
    if not is_money(raw_total):
        raise IngestError(f"{where}: Total 后缺少费用金额")
    total_fees = parse_email_decimal(raw_total, where)  # type: ignore[arg-type]
    index += 1
    amount = values[index] if index < len(values) else None
    if not is_money(amount):
        raise IngestError(f"{where}: 费用表后缺少成交净额")
    index += 1
    if index != len(values):
        # **模板新增字段时静默忽略会让解析结果悄悄漏掉信息。**
        raise IngestError(f"{where}: 费用表包含未知字段: {list(values[index:])}")

    fees = commission + industry_fee
    if fees != total_fees:
        # 采信哪一边都是在猜；费用会进入交易哈希与已实现损益。
        raise IngestError(f"{where}: 分项费用与 Total 不一致")
    return fees, amount  # type: ignore[return-value]


# 多价格模板用它分隔成交腿；有 Totals 时它也是腿分隔符，无 Totals 时只是免责声明结尾。
LEG_SEPARATOR_PREFIX = "for the above:"


def parse_value_rows(
    *,
    values: tuple[str, ...],
    where: str,
    trade_date: str,
    action: str,
    symbol: str,
    description: str,
    multiplier: int,
    has_charge_interest_column: bool = False,
) -> tuple[EmailTrade, ...]:
    """解析成交金额表，返回该符号块下的全部成交。

    **一个符号块可以产出多笔交易**（多价格成交）。

    Totals 分支做**三处勾稽**：逐腿费用之和、数量之和、金额绝对值之和都必须等于
    Totals 给出的值。任一处不符即报错——**不平均分摊、不按比例缩放**：
    分摊出来的数字在账本里看起来完全正常，但它对应的是从未真实发生过的成交。

    @param values: 金额表的各行（已 trim 且去除空行）。
    @param where: 错误定位说明。
    @param trade_date: 交易日期。
    @param action: 已映射的动作。
    @param symbol: 符号。
    @param description: 规范化描述。
    @param multiplier: 1 或 100。
    @param has_charge_interest_column: 表头是否明确包含 Charge and/or Interest 列。
    @returns 成交序列。
    @raises IngestError: 结构不符或任一勾稽失败。
    """
    from ..ledger.errors import IngestError

    remaining = list(values)
    disclosure = next(
        (i for i, value in enumerate(remaining)
         if value.lower().startswith(LEG_SEPARATOR_PREFIX)),
        -1,
    )
    # **只在没有 Totals 时截断。** 有 Totals 的多价格模板用同一文本分隔成交腿；
    # 在这里截断会把 Totals 一并切掉，于是退回单笔解析并**静默丢掉后续腿**。
    if disclosure >= 0 and "Totals" not in remaining:
        remaining = remaining[:disclosure]

    if "Totals" in remaining:
        return _parse_totals_rows(
            remaining=remaining, where=where, trade_date=trade_date, action=action,
            symbol=symbol, description=description, multiplier=multiplier,
            has_charge_interest_column=has_charge_interest_column,
        )

    if len(remaining) < 4:
        raise IngestError(f"{where}: 无法识别成交金额结构: {list(remaining)}")
    quantity, price, principal, *tail = remaining

    fees, amount = _single_row_fees(tail, where)
    return (build_trade(
        trade_date=trade_date, action=action, symbol=symbol, description=description,
        multiplier=multiplier, quantity=quantity, price=price, principal=principal,
        fees=fees, amount=amount, where=where,
    ),)


def _single_row_fees(tail: list[str], where: str) -> tuple[Decimal, str]:
    """解析单笔成交的费用尾部。

    三种紧凑形态先试，都不符才落到带标签的费用表。

    @returns ``(费用, 净额原文)``。
    @raises IngestError: 无法识别。
    """
    from ..ledger.errors import IngestError

    if len(tail) == 1 and is_money(tail[0]):
        return Decimal(0), tail[0]
    if (
        len(tail) == 2
        and tail[0].rstrip(":").lower() == "n/a"
        and is_money(tail[1])
    ):
        return Decimal(0), tail[1]
    if (
        len(tail) == 4
        and tail[0].lower() == "n/a:"
        and tail[2].lower() == "n/a"
        and is_money(tail[1])
        and is_money(tail[3])
    ):
        fees = parse_email_decimal(tail[1], where)
        if fees != 0:
            raise IngestError(f"{where}: N/A 费用必须为 0，实际 {fees}")
        return fees, tail[3]
    return parse_labeled_fees(tuple(tail), where)


def _charge_interest_row_fees(
    values: list[str], start: int, where: str,
) -> tuple[Decimal, str, int]:
    """解析五列成交行的费用单元格及其净额，并返回消耗的字段数。

    五列 HTML 被规范化为扁平文本；费用单元格可以展开为多个标签/金额行。此处按
    明确支持的标签语法确定边界，再交给既有单笔费用解析器校验，避免把后续腿误当
    当前费用或把未标注金额猜成某种费用。

    @param values: 当前成交行费用单元格开始处的扁平字段序列。
    @param start: 费用单元格在 ``values`` 中的起始索引。
    @param where: 错误定位说明。
    @returns ``(费用, 净额原文, 消耗字段数)``，消耗数包含净额字段。
    @raises IngestError: 费用标签未知、字段缺失或费用表勾稽失败。
    """
    from ..ledger.errors import IngestError

    tail = values[start:]
    if not tail:
        raise IngestError(f"{where}: Charge and/or Interest 字段缺失")

    label = tail[0].strip().lower()
    if label in ("n/a", "n/a:"):
        # 少数版本把 N/A 的零费用说明展开为四项；普通五列单元格只有 N/A 一项。
        count = (
            4
            if label == "n/a:"
            and len(tail) >= 4
            and is_money(tail[1])
            and tail[2].strip().lower() == "n/a"
            and is_money(tail[3])
            else 2
        )
    elif label == "industry fee:":
        # Industry Fee-only 模板可能在费用金额后重复裸标签。
        count = 4 if len(tail) >= 4 and tail[2].strip().lower() == "industry fee" else 3
    elif label == "commission:":
        # Commission 模板必须包含 Total；其后的两个金额分别是费用总额和成交净额。
        total_index = next(
            (index for index, value in enumerate(tail) if value.strip().lower() == "total:"),
            -1,
        )
        if total_index < 0:
            raise IngestError(f"{where}: Commission 费用缺少 Total 标签")
        count = total_index + 3
    else:
        raise IngestError(
            f"{where}: Charge and/or Interest 只接受 N/A 或明确标注的 Commission/Industry Fee"
        )

    if count > len(tail):
        raise IngestError(f"{where}: Charge and/or Interest 成交行字段不完整")
    fee_values = tail[:count]
    fees, amount = _single_row_fees(fee_values, where)
    return fees, amount, count


def _is_nonfinancial_leg_note(value: str) -> bool:
    """识别 eConfirm 已知的费用或平均单价说明脚注。

    五列 HTML 可在 ``for the above:`` 后插入已确认的费用、平均单价或代理身份说明。只接受
    这两类说明前缀和一条完整代理说明，且拒绝其中含任何数字或美元符号的文本。

    @param value: 分隔符后的单个规范化文本字段。
    @returns 是否以已确认的说明脚注前缀开头且不含数字/金额。
    """
    note = value.strip()
    if not note or re.search(r"[$\d]", note):
        return False
    known_leg_note_prefixes = re.compile(
        r"^\s*-\s*(?:Industry Fee:\s*For certain securities transactions\b|"
        r"The unit price shown is an average price\.)",
        re.IGNORECASE,
    )
    known_agent_note = "- Schwab acted as your agent."
    return (
        known_leg_note_prefixes.match(note) is not None
        or note.strip() == known_agent_note
    )


def _parse_totals_rows(
    *, remaining: list[str], where: str, trade_date: str, action: str,
    symbol: str, description: str, multiplier: int,
    has_charge_interest_column: bool,
) -> tuple[EmailTrade, ...]:
    """解析带 Totals 的多价格成交表。

    @param remaining: 含各腿和 Totals 的行。
    @param where: 错误定位说明。
    @param trade_date: 交易日期。
    @param action: 已映射动作。
    @param symbol: 证券代码。
    @param description: 规范化描述。
    @param multiplier: 1 或 100。
    @param has_charge_interest_column: 表头明确声明第五列模板时为真。
    @returns 逐腿成交。
    @raises IngestError: 行结构或 Totals 勾稽不符。
    """
    from ..ledger.errors import IngestError

    totals_index = remaining.index("Totals")
    fill_values = remaining[:totals_index]
    totals = remaining[totals_index + 1:]
    if len(totals) < 4:
        raise IngestError(f"{where}: 无法识别多价格成交结构")
    total_fees_raw = totals[-2]
    total_quantity_raw = totals[0]
    total_amount_raw = totals[-1]

    total_fees = parse_email_decimal(total_fees_raw, where)

    # 新版模板在最后一腿与 Totals 之间也保留分隔标记；它结束前一腿，不代表空腿。
    if fill_values and fill_values[-1].lower().startswith(LEG_SEPARATOR_PREFIX):
        fill_values = fill_values[:-1]

    separators = [
        index for index, value in enumerate(fill_values)
        if value.lower().startswith(LEG_SEPARATOR_PREFIX)
    ]
    rows: list[EmailTrade] = []
    if not fill_values:
        raise IngestError(f"{where}: 多价格成交缺少逐腿数据")
    if separators and not has_charge_interest_column:
        # 逐腿拆分：每腿前四项是数量、价格、本金…（其余交给费用区域解析）。
        bounds: list[tuple[int, int]] = []
        start = 0
        for separator in separators:
            bounds.append((start, separator))
            start = separator + 1
        bounds.append((start, len(fill_values)))
        for start_index, end_index in bounds:
            leg = fill_values[start_index:end_index]
            if len(leg) < 4:
                raise IngestError(f"{where}: 多价格成交单腿字段不足: {leg}")
            quantity, price, principal, *fees_tail = leg
            fees, amount = parse_labeled_fees(tuple(fees_tail), where)
            rows.append(build_trade(
                trade_date=trade_date, action=action, symbol=symbol,
                description=description, multiplier=multiplier,
                quantity=quantity, price=price, principal=principal,
                fees=fees, amount=amount, where=where,
            ))
    elif has_charge_interest_column:
        # HTML 转文本会把费用单元格里的标签和值展开，因此行宽不是固定五个文本字段。
        # 逐行从三个固定数值列开始，费用尾部只接受可由现有语法完整解释的结构。
        offset = 0
        while offset < len(fill_values):
            if fill_values[offset].lower().startswith(LEG_SEPARATOR_PREFIX):
                # 分隔符后的少数 HTML 模板注入一条普通说明；只跳过经严格词法检查的
                # 非金融文本，金额、数字或财务字段名都不能在此处被吞掉。
                offset += 1
                if offset < len(fill_values) and not re.fullmatch(
                    r"-?[\d,]+(?:\.\d+)?", fill_values[offset].strip()
                ):
                    if not _is_nonfinancial_leg_note(fill_values[offset]):
                        raise IngestError(
                            f"{where}: 成交腿分隔符后含未识别的字段"
                        )
                    offset += 1
                continue
            if len(fill_values) - offset < 5:
                raise IngestError(f"{where}: Charge and/or Interest 多价格结构列数不完整")
            quantity, price, principal = fill_values[offset:offset + 3]
            fees, amount, fee_tail_count = _charge_interest_row_fees(
                fill_values, offset + 3, where,
            )
            rows.append(build_trade(
                trade_date=trade_date, action=action, symbol=symbol,
                description=description, multiplier=multiplier,
                quantity=quantity, price=price, principal=principal,
                fees=fees, amount=amount, where=where,
            ))
            offset += 3 + fee_tail_count
        if not rows:
            raise IngestError(f"{where}: 多价格成交缺少逐腿数据")
    else:
        if len(fill_values) % 4 != 0:
            raise IngestError(f"{where}: 无法识别多价格成交结构")
        for offset in range(0, len(fill_values), 4):
            quantity, price, principal, amount = fill_values[offset:offset + 4]
            rows.append(build_trade(
                trade_date=trade_date, action=action, symbol=symbol,
                description=description, multiplier=multiplier,
                quantity=quantity, price=price, principal=principal,
                # 该形态没有费用信息，模板约定为零。
                fees=Decimal(0), amount=amount, where=where,
            ))

    _reconcile(totals, rows, where, total_fees, total_quantity_raw, total_amount_raw)
    return tuple(rows)


def _reconcile(
    totals: list[str], rows: list[EmailTrade], where: str,
    total_fees: Decimal, total_quantity_raw: str, total_amount_raw: str,
) -> None:
    """把逐腿结果与 Totals 勾稽。

    **三处都必须相符。** 不符时报错而不是分摊——分摊出来的数字在账本里看起来完全
    正常，但它对应的是**从未真实发生过的成交**。

    @raises IngestError: 任一处不符。
    """
    from ..ledger.errors import IngestError

    parsed_fees = sum((Decimal(row.fees) for row in rows), Decimal(0))
    if parsed_fees != total_fees:
        raise IngestError(
            f"{where}: 多价格成交费用无法可靠分摊: "
            f"逐腿合计 {parsed_fees} 与 Totals {total_fees} 不一致"
        )
    quantity_total = sum((Decimal(row.quantity) for row in rows), Decimal(0))
    if quantity_total != parse_email_decimal(total_quantity_raw, where):
        raise IngestError(f"{where}: 多价格成交数量与 Totals 不一致")
    # 金额取绝对值相加：买入为负、卖出为正，Totals 给的是规模而不是净额。
    amount_total = sum((abs(Decimal(row.amount)) for row in rows), Decimal(0))
    if amount_total != parse_email_decimal(total_amount_raw, where):
        raise IngestError(f"{where}: 多价格成交金额与 Totals 不一致")
    del totals


# 每个交易块里金额表的起点。
TOTAL_AMOUNT_MARKER = re.compile(r"Total Amount\s*\n", re.IGNORECASE)

# 该标记之后的内容与成交无关（免责与附加说明）。
ADDITIONAL_INFORMATION_MARKER = "Additional information for this security:"


def _resolve_existing_cash_option_sale(
    trade: EmailTrade,
    option: ParsedOptionSymbol,
    existing_option_sales: tuple[ParsedTransaction, ...],
    where: str,
) -> EmailTrade:
    """仅在同账户既有期权卖出中唯一重建出业务哈希时补全 Sale/Cash 动作。

    @param trade: 已按明确卖出方向完成金额勾稽的期权成交。
    @param option: 从邮件原始 Symbol 解析出的完整期权身份。
    @param existing_option_sales: 调用方绑定账户的既有期权卖出快照。
    @param where: 错误定位文本。
    @returns 复制既有 STO/STC 动作并标记为仅关联既有交易的成交。
    @raises UnresolvedCashOptionSaleError: 快照混账户、没有精确匹配或匹配到多个业务哈希。
    """
    if not existing_option_sales:
        raise UnresolvedCashOptionSaleError(
            f"{where}: 期权 Sale/Cash 缺少既有成交上下文，无法确定开仓或平仓",
            reason="missing_context",
        )

    accounts = {transaction.account for transaction in existing_option_sales}
    if len(accounts) != 1:
        raise UnresolvedCashOptionSaleError(
            f"{where}: 期权 Sale/Cash 的既有成交上下文不属于唯一账户",
            reason="mixed_account",
        )

    parsed_numbers = {
        "quantity": Decimal(trade.quantity),
        "price": Decimal(trade.price),
        "fees": Decimal(trade.fees),
        "amount": Decimal(trade.amount),
    }
    raw_fields = email_trade_to_raw_fields(trade)
    matches: dict[str, ParsedTransaction] = {}
    for existing in existing_option_sales:
        if (
            existing.account not in accounts
            or existing.asset_type != "option"
            or existing.action not in ("Sell to Open", "Sell to Close")
            or existing.transaction_date != trade.trade_date
            or existing.raw_symbol != trade.symbol
            or existing.underlying != option.underlying
            or existing.expiry != option.expiry
            or existing.strike != option.strike
            or existing.option_type != option.option_type
        ):
            continue

        # Decimal 相等校验拦住数值不同；重建业务哈希还会保留 CSV 数值文本精度，
        # 防止 1.0 与 1.00 这类显示差异被当作同一来源行。
        existing_numbers = {
            "quantity": existing.quantity,
            "price": existing.price,
            "fees": existing.fees if existing.fees is not None else Decimal(0),
            "amount": existing.amount,
        }
        if any(
            existing_numbers[name] is None
            or existing_numbers[name] != expected
            for name, expected in parsed_numbers.items()
        ):
            continue

        expected_hash = create_business_hash(
            account=existing.account,
            record_date=existing.record_date,
            transaction_date=trade.trade_date,
            action=existing.action,
            raw_symbol=trade.symbol,
            # eConfirm 与 CSV 对公司名/描述的呈现可能不同；用候选原交易的描述与记录日
            # 重建它自己的业务哈希，仍由原始合约身份和精确金额限定这笔匹配。
            description=existing.description,
            quantity_text=decimal_hash_text(raw_fields.quantity),
            price_text=decimal_hash_text(raw_fields.price),
            fees_text=decimal_hash_text(raw_fields.fees),
            amount_text=decimal_hash_text(raw_fields.amount),
        )
        if expected_hash == existing.business_hash:
            matches.setdefault(existing.business_hash, existing)

    if len(matches) != 1:
        raise UnresolvedCashOptionSaleError(
            f"{where}: 期权 Sale/Cash 无法唯一关联到既有成交",
            reason="no_match" if not matches else "ambiguous",
        )

    matched = next(iter(matches.values()))
    return replace(trade, action=matched.action, existing_only=True)


def parse_econfirm_body(
    message_id: str,
    body: str,
    *,
    existing_option_sales: tuple[ParsedTransaction, ...] = (),
    _bridge_context: _CashOptionSaleBridgeContext | None = None,
) -> tuple[EmailTrade, ...]:
    """从一段正文解析出全部成交。

    正文被 ``Symbol:`` 分成若干块（第一段是前言），**每块对应一个证券**；一个块内的
    金额表又可以含多笔成交（多价格成交），因此返回的是全部成交的拼接。

    @param message_id: 消息 ID，用于错误定位。
    @param body: 正文文本。
    @param existing_option_sales: 同一账户的既有期权卖出快照，仅用于 Sale/Cash。
    @param _bridge_context: 仅由同封 MIME 候选重试传入的已严格验证成交快照。
    @returns 成交序列。
    @raises UnresolvedCashOptionSaleError: Sale/Cash 无法唯一匹配既有成交。
    @raises IngestError: 没有交易块、字段缺失，或动作无法映射。
    """
    from ..ledger.parsers import parse_option_symbol

    parts = SYMBOL_SPLIT_PATTERN.split(body)
    if len(parts) < 2:
        raise IngestError("正文中没有交易块")

    trades: list[EmailTrade] = []
    for index, raw_block in enumerate(parts[1:]):
        # 附加说明与成交无关；留着它会让后面的行被当成金额表的一部分。
        block = raw_block.split(ADDITIONAL_INFORMATION_MARKER, 1)[0]
        where = f"邮件 {message_id} 第{index + 1}笔"

        first_line = next(
            (line.strip() for line in block.replace("\r", "").split("\n") if line.strip()),
            "",
        )
        symbol = unwrap_symbol(first_line)
        security_description = field(block, "Security Description", where)
        reported_action = field(block, "Action", where)
        position_type = field(block, "Type", where)
        trade_date = parse_trade_date(field(block, "Trade Date", where), where)

        option = parse_option_symbol(symbol)
        # 只对未知的期权 Sale/Cash 暂用明确的卖出现金方向完成勾稽；之后必须关联
        # 既有事实，不能把该临时动作写入返回值或扩大全局动作映射。
        unresolved_cash_sale = (
            option is not None
            and reported_action == "Sale"
            and position_type == "Cash"
        )
        action = (
            "Sell"
            if unresolved_cash_sale
            else (
                STOCK_ACTIONS.get(reported_action)
                if option is None
                else OPTION_ACTIONS.get(f"{reported_action}|{position_type}")
            )
        )
        if action is None:
            raise IngestError(
                f"{where}: 无法映射 Action/Type: "
                f"'{reported_action}'/'{position_type}'"
            )

        marker = TOTAL_AMOUNT_MARKER.search(block)
        if marker is None:
            raise IngestError(f"{where}: 缺少成交金额表")
        # 列宽本身有歧义，只有正文在 Total Amount 前明确声明该列时才启用五列解析。
        has_charge_interest_column = _has_charge_interest_column(block, marker.start())
        values = tuple(
            line.strip()
            for line in block[marker.end():].replace("\r", "").split("\n")
            if line.strip()
        )
        parsed_trades = parse_value_rows(
            values=values, where=where, trade_date=trade_date, action=action,
            symbol=symbol,
            description=normalize_description(symbol, security_description),
            multiplier=1 if option is None else 100,
            has_charge_interest_column=has_charge_interest_column,
        )
        if unresolved_cash_sale:
            assert option is not None
            for trade in parsed_trades:
                try:
                    strict_match = _resolve_existing_cash_option_sale(
                        trade, option, existing_option_sales, where,
                    )
                except UnresolvedCashOptionSaleError as error:
                    if (
                        _bridge_context is None
                        or error.reason != "no_match"
                    ):
                        raise
                    trades.append(
                        _bridge_context.reconcile(
                            trade, option, strict_match=None, where=where,
                        )
                    )
                else:
                    if _bridge_context is None:
                        trades.append(strict_match)
                    else:
                        trades.append(
                            _bridge_context.reconcile(
                                trade,
                                option,
                                strict_match=strict_match,
                                where=where,
                            )
                        )
        else:
            trades.extend(parsed_trades)
    return tuple(trades)


def _has_charge_interest_column(block: str, marker_start: int) -> bool:
    """判断 Total Amount 前是否明确声明五列逐腿格式。

    @param block: 完整交易块文本。
    @param marker_start: ``Total Amount`` 匹配的起始位置。
    @returns 表头以 Quantity、Price、Principal、Charge and/or Interest 顺序结束时为真。
    """
    header_lines = [
        line.strip()
        for line in block[:marker_start].replace("\r", "").split("\n")
        if line.strip()
    ]
    expected = tuple(
        label.casefold()
        for label in ("Quantity", "Price", "Principal", "Charge and/or Interest")
    )
    actual = tuple(line.casefold() for line in header_lines[-len(expected):])
    return actual == expected
