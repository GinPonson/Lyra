"""公开响应投影。

领域对象使用与 oracle 输出一致的 snake_case 字段名，而**客户端协议 2 使用
camelCase**。本模块是两者之间的唯一转换点。

**为什么不用通用驼峰化**：把 `net_deposits` 机械地转成 `netDeposits` 看似等价，
但一旦某个字段需要改名（例如 `account` → `accountId` 做脱敏），通用规则就会
悄悄产出旧名字。显式逐字段列出，漏字段会直接失败，而不是发一个客户端读不懂
的响应。
"""

from __future__ import annotations

from typing import Any

from ..domain.analytics.currency_groups import CurrencyGroupedResult
from ..domain.analytics.types import AccountSummary
from ..domain.numeric import decimal_text
from .app_error import ApplicationError


def project_summary(summary: AccountSummary) -> dict[str, Any]:
    """把总览投影为公开字段。

    对应 TypeScript 侧 ``projectSummary``：只做字段改名，不含计算。

    @param summary: 领域层总览。
    @returns camelCase 字段的公开字典。
    """
    return {
        "netDeposits": summary.net_deposits,
        "realizedPnlStock": summary.realized_pnl_stock,
        "realizedPnlOption": summary.realized_pnl_option,
        "realizedPnlTotal": summary.realized_pnl_total,
        "openPositions": summary.open_positions,
        "totalFees": summary.total_fees,
        "dividends": summary.dividends,
        "interest": summary.interest,
        "taxes": summary.taxes,
        "cashBalance": summary.cash_balance,
        "transactionCount": summary.txn_count,
        "dateRange": summary.date_range,
    }


def project_currency_grouped(result: CurrencyGroupedResult) -> dict[str, Any]:
    """把按币种分组的结果投影为公开信封。

    @param result: 分组结果。
    @returns ``{aggregateCurrency, groups: [{currency, accountIds, data}]}``。
    """
    return {
        "aggregateCurrency": result.aggregate_currency,
        "groups": [
            {
                "currency": group.currency,
                "accountIds": list(group.account_ids),
                "data": group.data,
            }
            for group in result.groups
        ],
    }


def account_id_map(accounts: tuple[Any, ...]) -> dict[str, str]:
    """构造「脱敏账户键 → 内部 UUID」映射，用于公开响应的账户脱敏。

    @param accounts: 快照中的账户。
    @returns 账户键到 UUID 的映射。
    """
    return {account.legacy_account_key: account.id for account in accounts}


def project_position(row: Any, account_ids: dict[str, str]) -> dict[str, Any]:
    """把持仓行投影为公开字段。

    对应 TypeScript 侧 ``projectPosition``：**账户引用改为内部 UUID**（脱敏），
    数值改为十进制字符串。

    @param row: 领域层持仓行。
    @param account_ids: 账户键到 UUID 的映射。
    @returns camelCase 字段的公开字典。
    @raises KeyError: 账户不在范围内——宁可失败也不泄漏未授权账户。
    """
    return {
        "accountId": account_ids[row.account],
        "symbolKey": row.symbol_key,
        "assetType": row.asset_type,
        "underlying": row.underlying,
        "expiry": row.expiry,
        "strike": None if row.strike is None else decimal_text(row.strike),
        "optionType": row.option_type,
        "direction": row.direction,
        "quantity": decimal_text(row.qty),
        "cost": decimal_text(row.cost),
        "firstOpenDate": row.first_open_date,
    }


def project_positions(rows: tuple[Any, ...], account_ids: dict[str, str]) -> list[dict[str, Any]]:
    """批量投影持仓行。"""
    return [project_position(row, account_ids) for row in rows]


def project_expiring(result: Any, account_ids: dict[str, str]) -> dict[str, Any]:
    """把到期持仓结果投影为公开契约。

    在公开持仓字段之上追加 ``status`` / ``daysRemaining`` / ``premiumDollars``。

    @param result: 领域层到期结果。
    @param account_ids: 账户键到 UUID 的映射。
    @returns 公开的到期响应。
    """
    return {
        "rows": [
            {
                **project_position(row, account_ids),
                "status": row.status,
                "daysRemaining": row.days_remaining,
                "premiumDollars": decimal_text(row.premium_dollars),
            }
            for row in result.rows
        ],
        "count": result.count,
        "asOf": result.as_of,
        "cutoff": result.cutoff,
        "expired": result.expired,
        "expiresToday": result.expires_today,
        "upcoming": result.upcoming,
    }


def project_drawdown(result: Any) -> dict[str, Any]:
    """把回撤结果投影为公开字段。

    对应 TypeScript 侧：``drawdown`` 命令把**领域结果直接**放进币种信封，因此
    公开字段就是领域字段的 camelCase 形式，没有额外的重命名。

    @param result: 领域层回撤结果。
    @returns camelCase 字段的公开字典。
    """
    return {
        "rows": [
            {
                "date": row.date,
                "dailyPnl": row.daily_pnl,
                "cumulativePnl": row.cumulative_pnl,
                "runningPeak": row.running_peak,
                "drawdown": row.drawdown,
            }
            for row in result.rows
        ],
        "days": result.days,
        "endingCumulativePnl": result.ending_cumulative_pnl,
        "maxDrawdown": result.max_drawdown,
        "peakDate": result.peak_date,
        "troughDate": result.trough_date,
        "recoveryDate": result.recovery_date,
        "basis": result.basis,
    }


def project_monthly(result: Any) -> dict[str, Any]:
    """月度绩效投影。"""
    return {
        "rows": [
            {
                "month": row.month,
                "closedLots": row.closed_lots,
                "closeTransactions": row.close_transactions,
                "wins": row.wins,
                "losses": row.losses,
                "grossProfit": row.gross_profit,
                "grossLoss": row.gross_loss,
                "netPnl": row.net_pnl,
                "fees": row.fees,
                "winRatePct": row.win_rate_pct,
                "profitFactor": row.profit_factor,
            }
            for row in result.rows
        ],
        "months": result.months,
        "totalPnl": result.total_pnl,
        "totalFees": result.total_fees,
        "basis": result.basis,
    }


def project_allocation(result: Any) -> dict[str, Any]:
    """历史成本配置投影。"""
    return {
        "rows": [
            {
                "category": row.category,
                "positions": row.positions,
                "longPositions": row.long_positions,
                "shortPositions": row.short_positions,
                "costBasis": row.cost_basis,
                "allocationPct": row.allocation_pct,
            }
            for row in result.rows
        ],
        "groups": result.groups,
        "totalCostBasis": result.total_cost_basis,
        "basis": result.basis,
    }


def project_attribution(result: Any) -> dict[str, Any]:
    """归因投影。"""
    return {
        "rows": [
            {
                "category": row.category,
                "closedLots": row.closed_lots,
                "closeTransactions": row.close_transactions,
                "wins": row.wins,
                "losses": row.losses,
                "netPnl": row.net_pnl,
                "fees": row.fees,
                "winRatePct": row.win_rate_pct,
                "absoluteContributionPct": row.absolute_contribution_pct,
            }
            for row in result.rows
        ],
        "groups": result.groups,
        "totalPnl": result.total_pnl,
        "basis": result.basis,
    }


def project_option_risk(result: Any) -> dict[str, Any]:
    """期权风险投影，含标的集中度。"""
    return {
        "rows": [
            {
                "bucket": row.bucket,
                "positions": row.positions,
                "contracts": row.contracts,
                "longPositions": row.long_positions,
                "shortPositions": row.short_positions,
                "longContracts": row.long_contracts,
                "shortContracts": row.short_contracts,
                "longPremiumPaid": row.long_premium_paid,
                "shortPremiumReceived": row.short_premium_received,
                "longPremiumPct": row.long_premium_pct,
            }
            for row in result.rows
        ],
        "asOf": result.as_of,
        "optionPositions": result.option_positions,
        "totalLongPremiumPaid": result.total_long_premium_paid,
        "totalShortPremiumReceived": result.total_short_premium_received,
        "topThreeLongUnderlyings": [
            {
                "underlying": item.underlying,
                "longPremiumPaid": item.long_premium_paid,
                "longPremiumPct": item.long_premium_pct,
            }
            for item in result.top_three_long_underlyings
        ],
        "topThreeLongConcentrationPct": result.top_three_long_concentration_pct,
        "basis": result.basis,
    }


def project_holding_period(result: Any) -> dict[str, Any]:
    """持有期汇总投影。"""
    return {
        "rows": [
            {
                "bucket": row.bucket,
                "closedLots": row.closed_lots,
                "closeTransactions": row.close_transactions,
                "wins": row.wins,
                "losses": row.losses,
                "averageHoldingDays": row.average_holding_days,
                "netPnl": row.net_pnl,
                "averageLotPnl": row.average_lot_pnl,
                "winRatePct": row.win_rate_pct,
            }
            for row in result.rows
        ],
        "buckets": result.buckets,
        "basis": result.basis,
    }


def project_streaks(result: Any) -> dict[str, Any]:
    """连续盈亏投影。"""
    return {
        "rows": [
            {
                "kind": row.kind,
                "count": row.count,
                "startDate": row.start_date,
                "endDate": row.end_date,
                "pnl": row.pnl,
            }
            for row in result.rows
        ],
        "closeTransactions": result.close_transactions,
        "neutralTransactions": result.neutral_transactions,
        "basis": result.basis,
    }


def project_option_stress(result: Any) -> dict[str, Any]:
    """压力测试投影。"""
    return {
        "rows": [
            {
                "underlying": row.underlying,
                "contracts": row.contracts,
                "premiumAtRisk": row.premium_at_risk,
                "nearestExpiry": row.nearest_expiry,
                "farthestExpiry": row.farthest_expiry,
                "lossIfZero": row.loss_if_zero,
                "portfolioPremiumPct": row.portfolio_premium_pct,
            }
            for row in result.rows
        ],
        "longOptionContracts": result.long_option_contracts,
        "allLongOptionsLossIfZero": result.all_long_options_loss_if_zero,
        "topThreeLossIfZero": result.top_three_loss_if_zero,
        "basis": result.basis,
    }


def project_transaction(row: Any, account_ids: dict[str, str]) -> dict[str, Any]:
    """把标准交易投影为公开字段。

    对应 TypeScript 侧 ``projectTransaction``：**刻意不展开哈希、来源引用与
    occurrence**——客户端不需要它们，暴露只会扩大泄漏面。

    @param row: 领域层交易。
    @param account_ids: 账户键到 UUID 的映射。
    @returns camelCase 字段的公开字典。
    @raises KeyError: 账户不在范围内。
    """
    return {
        "accountId": account_ids[row.account],
        "recordDate": row.record_date,
        "transactionDate": row.transaction_date,
        "action": row.action,
        "symbol": row.raw_symbol,
        "description": row.description,
        "assetType": row.asset_type,
        "underlying": row.underlying,
        "expiry": row.expiry,
        "strike": None if row.strike is None else decimal_text(row.strike),
        "optionType": row.option_type,
        "quantity": None if row.quantity is None else decimal_text(row.quantity),
        "price": None if row.price is None else decimal_text(row.price),
        "fees": None if row.fees is None else decimal_text(row.fees),
        "amount": None if row.amount is None else decimal_text(row.amount),
        "sourceType": row.source_type,
    }


def project_trade_query(
    result: Any,
    account_ids: dict[str, str],
    transactions_by_hash: dict[str, Any],
) -> dict[str, Any]:
    """投影交易流水查询结果。

    ``TradeRow`` 是 oracle 视图的**窄行**，不含 ``source_type``，因此按交易哈希
    关联回完整交易后再投影。

    @param result: 领域层查询结果。
    @param account_ids: 账户键到 UUID 的映射。
    @param transactions_by_hash: 交易哈希到完整交易的映射。
    @returns 公开的分页响应。

    ``nextCursor`` **原样透传领域层算出的值**（它是真实的"还有下一页"标记，不是
    按 ``count`` 猜的）。没有下一页时为 ``null``——客户端据此停止翻页。
    """
    return {
        "rows": [
            project_transaction(transactions_by_hash[row.txn_hash], account_ids)
            for row in result.rows
        ],
        "count": result.count,
        "nextCursor": result.next_cursor,
    }


def project_realized_query(result: Any, account_ids: dict[str, str]) -> dict[str, Any]:
    """投影已实现明细查询结果。

    合计取**过滤后的全量**，不是当前页——否则翻页时合计会随页变化。
    """
    return {
        "rows": [
            {
                "accountId": account_ids[row.account],
                "symbolKey": row.symbol_key,
                "assetType": row.asset_type,
                "underlying": row.underlying,
                "direction": row.direction,
                "openDate": row.open_date,
                "closeDate": row.close_date,
                "quantity": decimal_text(row.qty),
                "openPrice": decimal_text(row.open_price),
                "closePrice": decimal_text(row.close_price),
                "openFees": decimal_text(row.open_fees),
                "closeFees": decimal_text(row.close_fees),
                "pnl": decimal_text(row.pnl),
                "closeAction": row.close_action,
            }
            for row in result.rows
        ],
        "count": result.count,
        "totalPnl": result.total_pnl,
        # **总数是全量而非当前页**——翻页时它不变，客户端据此显示"共 N 条"。
        "totalCount": result.total_count,
        "nextCursor": result.next_cursor,
    }


def project_cashflow_query(
    result: Any,
    account_ids: dict[str, str],
    transactions_by_hash: dict[str, Any],
) -> dict[str, Any]:
    """投影现金流水查询结果，并复制分动作合计。

    与交易流水同理：``CashflowRow`` 是窄行，按哈希关联回完整交易后投影。
    """
    return {
        "rows": [
            project_transaction(transactions_by_hash[row.txn_hash], account_ids)
            for row in result.rows
        ],
        "count": result.count,
        "total": result.total,
        "byAction": dict(result.by_action),
        # 与 realized 同理：总数与合计基于全部筛选行，翻页时不随之变化。
        "totalCount": result.total_count,
        "nextCursor": result.next_cursor,
    }


def project_ledger_audit(result: Any) -> dict[str, Any]:
    """投影只读审计报告。

    对应 TypeScript 侧：``audit`` 命令把**领域结果直接**返回（不套币种信封），
    因此公开字段就是领域字段的 camelCase 形式。

    ``scopeNote`` 是审计范围说明文案；oracle 与本实现的措辞不同（"本命令" vs
    "本功能"），该差异在对照器白名单中登记。
    """
    return {
        "rows": [
            {
                "check": row.check,
                "severity": row.severity,
                "count": row.count,
                "expectation": row.expectation,
                "status": row.status,
            }
            for row in result.rows
        ],
        "ok": result.ok,
        "failed": result.failed,
        "transactionCount": result.transaction_count,
        "latestTransactionDate": result.latest_transaction_date,
        "readOnly": result.read_only,
        "scopeNote": result.scope_note,
    }


def project_extreme_trades(result: Any, account_ids: dict[str, str]) -> dict[str, Any]:
    """投影最佳/最差交易。

    对应 TypeScript 侧 ``projectExtremeTrades``：只公开业务字段与十进制文本，
    **不公开交易哈希或旧账户键**。

    @param result: 领域层结果。
    @param account_ids: 账户键到 UUID 的映射。
    @returns 公开的极值交易响应。
    @raises KeyError: 极值交易属于授权范围外账户。
    """
    return {
        "rows": [
            {
                "side": row.side,
                "rank": row.rank,
                "trade": {
                    "accountId": account_ids[row.account],
                    "closeDate": row.close_date,
                    "symbolKey": row.symbol_key,
                    "underlying": row.underlying,
                    "assetType": row.asset_type,
                    "direction": row.direction,
                    "closeAction": row.close_action,
                    "quantity": decimal_text(row.qty),
                    "pnl": decimal_text(row.pnl),
                    "fees": decimal_text(row.fees),
                    "earliestOpenDate": row.earliest_open_date,
                    "latestOpenDate": row.latest_open_date,
                },
            }
            for row in result.rows
        ],
        "perSide": result.per_side,
        "basis": result.basis,
    }


def project_import_job(job: Any) -> dict[str, Any]:
    """把公开导入作业投影为 camelCase 字段。

    **不能直接把 dataclass 交给通用序列化器**：它按字段名出站，会把内部的
    snake_case 命名发给客户端，而客户端只会读到 ``undefined``。

    @param job: ``PublicImportJob`` 实例。
    @returns camelCase 字段的公开字典。
    """
    return {
        "id": job.id,
        "fileName": job.file_name,
        "fileType": job.file_type,
        "fileSizeBytes": job.file_size_bytes,
        "status": job.status,
        "targetAccountId": job.target_account_id,
        "baseLedgerVersion": job.base_ledger_version,
        "totalRows": job.total_rows,
        "insertedRows": job.inserted_rows,
        "skippedRows": job.skipped_rows,
        "errorCode": job.error_code,
        "committedLedgerVersion": job.committed_ledger_version,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
    }


def project_import_preview(
    preview: Any,
    *,
    import_id: str,
    account_id: str,
    base_ledger_version: int,
    confirm_token: str,
) -> dict[str, Any]:
    """把导入预览投影为 camelCase 公开响应。

    响应按 Flutter ``ImportPreviewView.fromJson`` 的契约组织：作业 ID 与一次性
    ``confirmToken`` 位于根对象，文件统计位于 ``preview``。服务端只保存令牌哈希，
    预览指纹本身不外发——它是提交校验用的内部凭据。

    @param preview: 领域层预览结果。
    @param import_id: 已创建的作业 ID。
    @param account_id: 已绑定的账户 UUID。
    @param base_ledger_version: 预览时的账本基准版本。
    @param confirm_token: 唯一一次返回的明文确认令牌。
    @returns 包含作业 ID、确认令牌与嵌套预览统计的公开字典。
    """
    return {
        "importId": import_id,
        "confirmToken": confirm_token,
        "preview": {
            "fileName": preview.file_name,
            "accountId": account_id,
            "baseLedgerVersion": base_ledger_version,
            "totalRows": preview.total_rows,
            "insertableRows": preview.insertable_rows,
            "existingRows": preview.existing_rows,
            "byAction": dict(preview.by_action),
        },
    }


def project_account_list(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把账户列表投影为公开字段并稳定排序。

    对应 TypeScript 侧 ``createPublicAccountList``。排序键依次是：状态（active 在前）、
    创建时间、ID——**三级排序是刻意的**：仅按状态排序时同状态账户的顺序取决于数据库
    返回顺序，客户端每次刷新都可能看到不同排列。

    @param accounts: 账户行（含 snake_case 字段）。
    @returns camelCase 字段的公开列表。
    """
    ordered = sorted(
        accounts,
        key=lambda account: (
            0 if account["status"] == "active" else 1,
            account["created_at"],
            account["id"],
        ),
    )
    return [
        {
            "id": account["id"],
            "displayName": account["display_name"],
            "broker": account["broker"],
            "maskedSuffix": account["masked_suffix"],
            "baseCurrency": account["base_currency"],
            "status": account["status"],
            "createdAt": account["created_at"],
        }
        for account in ordered
    ]


def project_agent_session(session: Any) -> dict[str, Any]:
    """把 Agent 会话投影为 App 可公开的快照。

    **逐字段重建**，而不是透传对象：内部运行状态与未来新增的供应商私有字段
    不会因为对象透传进入客户端响应。

    可选字段遵循 TypeScript 的 ``undefined`` / ``null`` 区分：

    - ``status`` 未设置 → 响应里**不出现**该键；
    - ``errorCode`` 未设置 → 不出现；显式为空 → 出现且为 ``null``。

    @param session: 领域层会话快照。
    @returns camelCase 的公开快照。
    """
    from ..domain.agent.switching import UNSET

    messages: list[dict[str, Any]] = []
    for message in session.messages:
        item: dict[str, Any] = {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "createdAt": message.created_at,
        }
        if message.status is not None:
            item["status"] = message.status
        if not isinstance(message.error_code, type(UNSET)):
            item["errorCode"] = message.error_code
        messages.append(item)

    return {
        "id": session.id,
        "provider": session.provider,
        "status": session.status,
        "accountScope": (
            {"kind": "all"}
            if session.account_scope.kind == "all"
            else {"kind": "account", "account": session.account_scope.account}
        ),
        "ledgerVersion": session.ledger_version,
        "asOf": session.as_of,
        "revision": session.revision,
        "messages": messages,
    }


def project_market_positions(
    rows: list[Any],
    market_runtime: Any,
    account_ids: dict[str, str],
    market_settings: Any = None,
) -> dict[str, Any]:
    """把持仓估值行投影为公开响应。

    **camelCase 与 snake_case 的转换点在这里**，正如其它投影函数：一次性转换比让
    每个调用方各自记得转换可靠。

    ``complete`` 的含义是"每一行都有新鲜且可用的报价"，而不是"每一行都有估值"——
    过期报价的行有估值但不完整（§92.3）。

    **账户引用走与 ``project_position`` 相同的脱敏映射**（legacy 键 → 内部 UUID）：
    直接输出 ``account``（legacy 券商标识）会泄漏完整账户键，而 TS 的
    ``PublicPosition`` 用 ``accountId``。两者在字段名与取值上都不同。

    @param rows: 已算好估值的持仓行（snake_case）。
    @param market_runtime: 行情运行时；用于填充来源状态。
    @param account_ids: 账户键到 UUID 的映射。
    @param market_settings: 本次读取固定使用的路由与 revision 快照。
    @returns camelCase 的公开响应。
    @raises KeyError: 账户不在范围内——宁可失败也不泄漏未授权账户。
    """
    from ..domain.market_data.valuation import (
        PositionValuation,
        aggregate_currency_group,
    )

    projected = [
        {
            "accountId": account_ids[row["account"]],
            "symbolKey": row["symbol_key"],
            "currency": row["currency"],
            "instrumentKey": row["instrument_key"],
            "assetType": row["asset_type"],
            "underlying": row["underlying"],
            "expiry": row["expiry"],
            "strike": row["strike"],
            "optionType": row["option_type"],
            "direction": row["direction"],
            "quantity": row["quantity"],
            "cost": row["cost"],
            "firstOpenDate": row["first_open_date"],
            "valuation": row["valuation"],
            "quote": row["quote"],
        }
        for row in rows
    ]
    priced = [row for row in projected if row["valuation"]["marketValue"] is not None]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in projected:
        quote = row["quote"]
        reference = quote["referencePrice"]
        valuation = row["valuation"]
        groups.setdefault(row["currency"], []).append({
            "accountId": row["accountId"],
            "valuation": PositionValuation(
                market_value=valuation["marketValue"],
                unrealized_pnl=valuation["unrealizedPnl"],
                multiplier=valuation["multiplier"],
            ),
            "available": quote["status"] == "available",
            "as_of": None if reference is None else reference["asOf"],
        })
    currency_groups = []
    for currency in sorted(groups):
        group_rows = groups[currency]
        total = aggregate_currency_group(group_rows)
        currency_groups.append({
            "currency": currency,
            "accountIds": sorted({row["accountId"] for row in group_rows}),
            "positions": total.positions,
            "pricedPositions": total.priced_positions,
            "complete": total.complete,
            "marketValue": total.market_value,
            "unrealizedPnl": total.unrealized_pnl,
            "asOfFrom": total.as_of_from,
            "asOfTo": total.as_of_to,
        })
    return {
        "rows": projected,
        "count": len(projected),
        "pricedCount": len(priced),
        "complete": all(
            row["quote"] is not None
            and row["quote"]["status"] == "available"
            and row["valuation"]["marketValue"] is not None
            for row in projected
        ),
        "basis": "current_positions_at_quote_times_excluding_fees",
        "marketData": _market_status(market_runtime, market_settings),
        "currencyGroups": currency_groups,
    }


def _market_status(market_runtime: Any, market_settings: Any = None) -> dict[str, Any]:
    """投影本次请求采用的行情路由、修订号与已知来源状态。

    @param market_runtime: 行情运行时；``None`` 表示未装配。
    @param market_settings: 本次请求已按用户读取的设置快照。
    @raises ApplicationError: 装了运行时却没有给出设置快照。
    """
    if market_runtime is None:
        return {
            "enabled": False,
            "routes": {"stock": [], "option": []},
            "providers": [],
            "settingsRevision": 0,
        }
    if market_settings is None:
        # **这里不能再自己去读设置**：快照按用户读取，而本函数只有运行时、没有用户
        # 身份与连接。原先的 `settings_snapshot()` 无参调用在多用户下要么直接报错，
        # 要么退化成"读某个全局当前值"——后者会让 A 看到 B 的来源。因此改为要求调用方
        # 显式传入它已经按用户取好的快照，缺失即明确失败。
        raise ApplicationError(
            code="MARKET_SETTINGS_SNAPSHOT_REQUIRED",
            message="行情状态投影缺少设置快照",
            status_code=500,
            retryable=False,
        )
    routes = market_settings.routes
    configured = frozenset(market_runtime.provider_ids)
    from ..domain.market_data.settings import effective_routes

    effective = effective_routes(routes, configured)
    return {
        "enabled": any(effective.values()),
        "routes": {
            asset_type: list(providers)
            for asset_type, providers in routes.items()
        },
        "providers": [
            {
                "id": provider.id,
                "displayName": provider.display_name,
                "assetTypes": list(provider.asset_types),
                "configured": provider.configured,
                "timing": {
                    "kind": provider.timing_kind,
                    "delayMinutes": provider.delay_minutes,
                },
                "cacheTtlSeconds": provider.cache_ttl_seconds,
            }
            for provider in market_runtime.provider_options
        ],
        "settingsRevision": market_settings.revision,
    }
