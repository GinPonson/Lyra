import '../../core/design/lyra_icons.dart';

import 'package:flutter/material.dart';

/// CLI 只读功能的产品目录；路由、口径和展示字段在此唯一声明。
/// 字段沿用服务端公开协议，避免界面自行推算行情或账户净值。
enum LedgerView {
  positions('持仓', 'positions', '历史开仓成本，不代表实时市值', LyraIcons.stocks, 'costBasis'),
  expiring(
    '期权到期',
    'expiring',
    '未平期权 · 含尚未补齐平仓记录的过期合约',
    LyraIcons.dataCutoff,
    'premiumDollars',
  ),
  trades('全部交易', 'trades', '券商交易记录 · 按成交日期查询', LyraIcons.ledger, 'amount'),
  realized(
    '已实现损益',
    'realized',
    '先进先出计算的已实现损益 · 扣除分摊费用',
    LyraIcons.analytics,
    'pnl',
  ),
  cashflow('现金流', 'cashflow', '转账、股息、利息与税项', LyraIcons.cash, 'amount'),
  monthly(
    '月度表现',
    'monthly',
    '按平仓月份汇总的已实现损益 · 先进先出口径',
    LyraIcons.analytics,
    'netPnl',
    analytics: true,
  ),
  allocation(
    '仓位配置',
    'allocation',
    '历史成本配置 · 非实时市值占比',
    LyraIcons.portfolio,
    'costBasis',
    analytics: true,
  ),
  attribution(
    '利润归因',
    'attribution',
    '按标的拆解已实现损益',
    LyraIcons.analytics,
    'netPnl',
    analytics: true,
  ),
  drawdown(
    '已实现回撤',
    'drawdown',
    '累计已实现损益 · 非账户净值曲线',
    LyraIcons.cost,
    'drawdown',
    analytics: true,
  ),
  risk(
    '期权风险',
    'risk',
    '长期权已付与短期权已收权利金分别展示',
    LyraIcons.security,
    'longPremiumPaid',
    analytics: true,
  ),
  holdingPeriod(
    '持有周期',
    'holding-period',
    '按开仓批次的实际持有天数分组 · 先进先出',
    LyraIcons.dataCutoff,
    'netPnl',
    analytics: true,
  ),
  extremes(
    '交易排行',
    'extremes',
    '同一平仓交易合并后排名',
    LyraIcons.analytics,
    'pnl',
    analytics: true,
  ),
  streaks(
    '连续盈亏',
    'streaks',
    '按平仓交易顺序统计 · 同日顺序不等于盘中先后',
    LyraIcons.cost,
    'pnl',
    analytics: true,
  ),
  stress(
    '压力测试',
    'stress',
    '长期权权利金全部归零的假设损失 · 不含概率',
    LyraIcons.security,
    'lossIfZero',
    analytics: true,
  ),
  audit('账本检查', 'audit', '核对交易、持仓与已实现损益的一致性', LyraIcons.security, 'count');

  const LedgerView(
    this.title,
    this.command,
    this.basis,
    this.icon,
    this.valueField, {
    this.analytics = false,
  });
  final String title;
  final String command;
  final String basis;
  final IconData icon;
  final String valueField;
  final bool analytics;
  String get path => '/v1/${analytics ? 'analytics' : 'ledger'}/$command';
  bool get paginated => [trades, realized, cashflow].contains(this);
  bool get searchable => [positions, trades, realized].contains(this);
  /// 日期筛选只用于服务端已接线的逐笔查询，不给全时段分析提供无效条件。
  bool get dateFilter => [trades, realized, cashflow].contains(this);

  /// 服务端要求的参数显式指定；其它参数只在用户选择后提交。
  Map<String, String> get parameters => switch (this) {
    expiring => {'days': '30'},
    allocation || attribution => {'group_by': 'underlying'},
    extremes => {'limit': '10'},
    trades || realized || cashflow => {'limit': '50'},
    _ => {},
  };
}

/// 公共字段的中文词汇表；明细只显示有定义的用户字段。
const ledgerLabels = <String, String>{
  'quantity': '数量',
  'cost': '历史成本（未乘合约乘数）',
  'premiumDollars': '历史权利金',
  'amount': '发生金额',
  'price': '成交价',
  'fees': '费用',
  'pnl': '净损益',
  'netPnl': '净损益',
  'costBasis': '历史成本',
  'drawdown': '回撤',
  'longPremiumPaid': '长期权已付',
  'shortPremiumReceived': '短期权已收',
  'lossIfZero': '归零损失',
  'count': '记录数',
  'totalPnl': '合计损益',
  'totalFees': '合计费用',
  'total': '合计金额',
  'totalCostBasis': '历史成本合计',
  'endingCumulativePnl': '累计已实现损益',
  'maxDrawdown': '最大已实现回撤',
  'totalLongPremiumPaid': '长期权已付合计',
  'totalShortPremiumReceived': '短期权已收合计',
  'allLongOptionsLossIfZero': '全部长期权归零损失',
  'topThreeLossIfZero': '前三标的归零损失',
  'symbolKey': '证券 / 合约',
  'symbol': '证券代码',
  'underlying': '标的',
  'assetType': '资产类别',
  'direction': '方向',
  'expiry': '到期日',
  'strike': '行权价',
  'optionType': '合约类型',
  'firstOpenDate': '首次开仓',
  'openDate': '开仓日',
  'closeDate': '平仓日',
  'openPrice': '开仓价',
  'closePrice': '平仓价',
  'openFees': '开仓费用',
  'closeFees': '平仓费用',
  'closeAction': '平仓动作',
  'recordDate': '记录日期',
  'transactionDate': '成交日期',
  'action': '交易动作',
  'description': '说明',
  'sourceType': '来源',
  'daysRemaining': '剩余天数',
  'status': '状态',
  'positions': '仓位数',
  'longPositions': '多头仓位',
  'shortPositions': '空头仓位',
  'allocationPct': '配置占比 %',
  'absoluteContributionPct': '绝对贡献 %',
  'closedLots': '平仓批次',
  'closeTransactions': '平仓交易数',
  'wins': '盈利批次',
  'losses': '亏损批次',
  'winRatePct': '胜率 %',
  'grossProfit': '盈利合计',
  'grossLoss': '亏损合计',
  'profitFactor': '盈亏比',
  'dailyPnl': '当日已实现损益',
  'cumulativePnl': '累计已实现损益',
  'runningPeak': '累计峰值',
  'contracts': '合约数量',
  'longContracts': '多头合约数',
  'shortContracts': '空头合约数',
  'longPremiumPct': '长期权成本占比 %',
  'averageHoldingDays': '平均持有天数',
  'averageLotPnl': '平均批次损益',
  'startDate': '开始日期',
  'endDate': '结束日期',
  'premiumAtRisk': '已支付权利金',
  'portfolioPremiumPct': '权利金占比 %',
  'nearestExpiry': '最近到期',
  'farthestExpiry': '最远到期',
  'earliestOpenDate': '最早开仓',
  'latestOpenDate': '最晚开仓',
  'peakDate': '峰值日期',
  'troughDate': '谷值日期',
  'recoveryDate': '恢复日期',
};

/// 字段性质由公开账本协议定义，不能把数量、日期或百分比当作金额遮挡。
const ledgerAmountFields = <String>{
  'cost',
  'premiumDollars',
  'amount',
  'price',
  'fees',
  'pnl',
  'netPnl',
  'costBasis',
  'drawdown',
  'longPremiumPaid',
  'shortPremiumReceived',
  'lossIfZero',
  'totalPnl',
  'totalFees',
  'total',
  'totalCostBasis',
  'endingCumulativePnl',
  'maxDrawdown',
  'totalLongPremiumPaid',
  'totalShortPremiumReceived',
  'allLongOptionsLossIfZero',
  'topThreeLossIfZero',
  'strike',
  'openPrice',
  'closePrice',
  'openFees',
  'closeFees',
  'grossProfit',
  'grossLoss',
  'dailyPnl',
  'cumulativePnl',
  'runningPeak',
  'averageLotPnl',
  'premiumAtRisk',
};

/// 正负方向表达收益或现金流；成本、费用和假设损失的正数仍使用中性色。
const ledgerSignedAmountFields = <String>{
  'amount',
  'pnl',
  'netPnl',
  'totalPnl',
  'total',
  'endingCumulativePnl',
  'grossProfit',
  'grossLoss',
  'dailyPnl',
  'cumulativePnl',
  'averageLotPnl',
};

bool _isOptionRecord(Map<String, Object?> record) =>
    record['assetType'] == 'option' ||
    record['strike'] != null ||
    record['optionType'] != null;

/// 期权完整代码可能包含行权价；隐藏金额时标题与头像只读取独立标的字段。
String ledgerSecurityText(
  Map<String, Object?> record, {
  required bool amountsHidden,
}) {
  if (amountsHidden && _isOptionRecord(record)) {
    return ledgerText(record['underlying'] ?? '期权合约');
  }
  return ledgerText(
    record['underlying'] ??
        record['symbol'] ??
        record['symbolKey'] ??
        record['action'],
  );
}

/// 原文说明和完整期权代码可能含金额；结构化数量、日期及比例仍正常展示。
String ledgerFieldText(
  String field,
  Map<String, Object?> record, {
  required bool amountsHidden,
}) {
  final value = record[field];
  if (field == 'status' && record.containsKey('check')) {
    return ledgerAuditStatusText(value);
  }
  if (amountsHidden && value != null) {
    if (field == 'description') return '说明已隐藏';
    if ((field == 'symbol' || field == 'symbolKey') &&
        _isOptionRecord(record)) {
      return [
        ledgerSecurityText(record, amountsHidden: true),
        if (record['expiry'] != null) ledgerText(record['expiry']),
        if (record['optionType'] != null) ledgerText(record['optionType']),
      ].join(' · ');
    }
  }
  return ledgerText(value);
}

/// 枚举值翻译保持金融语义；未知券商动作仍保留原文供核对。
String ledgerText(Object? value) => switch (value) {
  null => '—',
  'stock' => '股票',
  'option' => '期权',
  'long' => '多头',
  'short' => '空头',
  'C' => '看涨',
  'P' => '看跌',
  'Buy' => '买入',
  'Sell' => '卖出',
  'Buy to Open' => '买入开仓',
  'Sell to Open' => '卖出开仓',
  'Buy to Close' => '买入平仓',
  'Sell to Close' => '卖出平仓',
  'Expired' || 'expired' => '已到期',
  'Assigned' => '已指派',
  'expires_today' => '今日到期',
  'upcoming' => '即将到期',
  'Cash Dividend' => '现金股息',
  'Credit Interest' => '利息收入',
  'MoneyLink Transfer' => '银行转账',
  'csv' => '官方 CSV',
  'excel' => 'Excel',
  'gmail' => 'Gmail 成交确认',
  'win' => '连续盈利',
  'loss' => '连续亏损',
  'pass' => '通过',
  'fail' => '未通过',
  true => '是',
  false => '否',
  String() => value,
  num() when value.isFinite => value.toString(),
  _ => '暂无法显示',
};

/// 天数区间沿用账本返回的边界，只把内部区间名称转换为易读文字。
String ledgerDayRangeText(Object? value) => switch (value) {
  'expired' => '已到期',
  '0-7d' => '0–7 天',
  '0-30d' => '0–30 天',
  '8-30d' => '8–30 天',
  '31-60d' => '31–60 天',
  '31-90d' => '31–90 天',
  '61-90d' => '61–90 天',
  '90d+' => '超过 90 天',
  _ => '时间范围待确认',
};

/// 新增检查也保留实际结果，不把内部检查名称当作用户说明。
String ledgerAuditTitle(Object? check) => switch (check) {
  'transactions_present' => '原始交易记录',
  'invalid_trade_fields' => '交易关键信息是否齐全',
  'unreflected_fifo_transactions' => '交易是否计入持仓与损益',
  'orphan_open_lots' => '持仓来源记录是否齐全',
  'orphan_realized_rows' => '损益来源记录是否齐全',
  'invalid_lot_quantities' => '持仓数量是否有效',
  'fifo_derivatives_present' => '持仓与损益记录是否已生成',
  _ => '其他账本检查',
};

String ledgerAuditStatusText(Object? status) => switch (status) {
  'pass' => '通过',
  'fail' => '未通过',
  _ => '结果待确认',
};
