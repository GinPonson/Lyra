"""Schwab eConfirm 邮件的识别。

镜像 TypeScript 版 ``domain/gmail/econfirm.ts`` 的常量与消息类型部分。

**发件人模式刻意写得严格**：邮件地址必须位于开头、``<`` 之后或空白之后，且以
``>``、空白或结尾结束。宽松的 ``包含即匹配`` 会让 ``evil@mail.schwab.com.attacker.net``
这种伪造地址通过——**它包含目标字符串但不是同一个地址**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from ..ledger.types import RawTransactionFields

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
                "fees": trade.fees, "amount": trade.amount,
            }
            for trade in trades
        ],
        separators=(",", ":"), ensure_ascii=False,
    )


def parse_econfirm(message: GmailMessage) -> tuple[EmailTrade, ...]:
    """从全部正文候选中确定性提取成交。

    **两个候选必须解析出同一批交易。** 纯文本与 HTML 是同一封邮件的两种表示，
    若结果不同说明至少有一方解析错了——**此时不能挑一个"看起来更完整"的用**，
    那会在用户毫无察觉的情况下写入错误数据。

    @param message: 已解析的邮件。
    @returns 成交序列。
    @raises IngestError: 全部候选失败，或候选之间结果不一致。
    """
    from ..ledger.errors import IngestError

    successes: dict[str, tuple[EmailTrade, ...]] = {}
    errors: list[str] = []
    for index, body in enumerate(candidate_bodies(message)):
        try:
            trades = parse_econfirm_body(message.message_id, body)
        except IngestError as error:
            # 单个候选失败不致命：另一个候选可能解析得出来。
            errors.append(f"候选{index + 1}: {error}")
            continue
        successes[trades_fingerprint(trades)] = trades

    if not successes:
        raise IngestError(
            f"邮件 {message.message_id}: 所有正文候选解析失败: {'；'.join(errors)}"
        )
    if len(successes) != 1:
        raise IngestError(
            f"邮件 {message.message_id}: plain 与 HTML 解析出的交易不一致"
        )
    return next(iter(successes.values()))


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


def _parse_totals_rows(
    *, remaining: list[str], where: str, trade_date: str, action: str,
    symbol: str, description: str, multiplier: int,
) -> tuple[EmailTrade, ...]:
    """解析带 Totals 的多价格成交表。"""
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
    if separators:
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


def parse_econfirm_body(message_id: str, body: str) -> tuple[EmailTrade, ...]:
    """从一段正文解析出全部成交。

    正文被 ``Symbol:`` 分成若干块（第一段是前言），**每块对应一个证券**；一个块内的
    金额表又可以含多笔成交（多价格成交），因此返回的是全部成交的拼接。

    @param message_id: 消息 ID，用于错误定位。
    @param body: 正文文本。
    @returns 成交序列。
    @raises IngestError: 没有交易块、字段缺失，或动作无法映射。
    """
    from ..ledger.errors import IngestError
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
        # 期权看「买卖 + 保证金/做空」，正股只看买卖方向。
        action = (
            STOCK_ACTIONS.get(reported_action)
            if option is None
            else OPTION_ACTIONS.get(f"{reported_action}|{position_type}")
        )
        if action is None:
            raise IngestError(
                f"{where}: 无法映射 Action/Type: "
                f"'{reported_action}'/'{position_type}'"
            )

        marker = TOTAL_AMOUNT_MARKER.search(block)
        if marker is None:
            raise IngestError(f"{where}: 缺少成交金额表")
        values = tuple(
            line.strip()
            for line in block[marker.end():].replace("\r", "").split("\n")
            if line.strip()
        )
        trades.extend(parse_value_rows(
            values=values, where=where, trade_date=trade_date, action=action,
            symbol=symbol,
            description=normalize_description(symbol, security_description),
            multiplier=1 if option is None else 100,
        ))
    return tuple(trades)
