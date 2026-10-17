/// 只读账本响应的权威语境与 summary 模型。
///
/// 所有金额保持服务端序列化的十进制字符串；App 不用 double 参与任何金融
/// 计算（设计方案 §6.3），仅在展示层格式化。
class LedgerSummaryMetadata {
  const LedgerSummaryMetadata({
    required this.ledgerVersion,
    required this.asOf,
    required this.fetchedAt,
    required this.aggregateCurrency,
  });

  final int ledgerVersion;
  final String asOf;
  final String fetchedAt;
  final String? aggregateCurrency;

  factory LedgerSummaryMetadata.fromJson(Map<String, Object?> json) {
    return LedgerSummaryMetadata(
      ledgerVersion: (json['ledgerVersion'] as num).toInt(),
      asOf: json['asOf'] as String,
      fetchedAt: json['fetchedAt'] as String,
      aggregateCurrency: json['aggregateCurrency'] as String?,
    );
  }
}

/// 单币种 summary 的稳定公开结果。
class LedgerSummaryEntry {
  const LedgerSummaryEntry({
    required this.currency,
    required this.netDeposits,
    required this.realizedPnlTotal,
    required this.realizedPnlStock,
    required this.realizedPnlOption,
    required this.totalFees,
    required this.dividends,
    required this.interest,
    required this.taxes,
    required this.cashBalance,
    required this.openPositions,
    required this.transactionCount,
  });

  final String currency;
  final String netDeposits;
  final String realizedPnlTotal;
  final String realizedPnlStock;
  final String realizedPnlOption;
  final String totalFees;
  final String dividends;
  final String interest;
  final String taxes;
  final String cashBalance;
  final int openPositions;
  final int transactionCount;

  factory LedgerSummaryEntry.fromJson(
    String currency,
    Map<String, Object?> json,
  ) {
    return LedgerSummaryEntry(
      currency: currency,
      netDeposits: requiredDecimalAmount(json, 'netDeposits'),
      realizedPnlTotal: requiredDecimalAmount(json, 'realizedPnlTotal'),
      realizedPnlStock: requiredDecimalAmount(json, 'realizedPnlStock'),
      realizedPnlOption: requiredDecimalAmount(json, 'realizedPnlOption'),
      totalFees: requiredDecimalAmount(json, 'totalFees'),
      dividends: requiredDecimalAmount(json, 'dividends'),
      interest: requiredDecimalAmount(json, 'interest'),
      taxes: requiredDecimalAmount(json, 'taxes'),
      cashBalance: requiredDecimalAmount(json, 'cashBalance'),
      openPositions: requiredCount(json, 'openPositions'),
      transactionCount: requiredCount(json, 'transactionCount'),
    );
  }
}

/// GET /v1/ledger/summary 的完整响应。
class LedgerSummaryRead {
  const LedgerSummaryRead({required this.metadata, required this.entries});

  final LedgerSummaryMetadata metadata;
  final List<LedgerSummaryEntry> entries;

  factory LedgerSummaryRead.fromJson(Map<String, Object?> json) {
    final metadata = LedgerSummaryMetadata.fromJson(
      json['metadata'] as Map<String, Object?>,
    );
    final data = json['data'] as Map<String, Object?>;
    final groups = data['groups'] as List<Object?>? ?? const <Object?>[];
    return LedgerSummaryRead(
      metadata: metadata,
      entries: groups
          .map(
            (group) => LedgerSummaryEntry.fromJson(
              (group as Map<String, Object?>)['currency'] as String,
              group['data'] as Map<String, Object?>,
            ),
          )
          .toList(growable: false),
    );
  }
}

/// 服务端公开协议的定点十进制文本；金额与数量只接受此形态。
final RegExp _decimalAmountText = RegExp(r'^-?\d+(?:\.\d+)?$');

/// 判定任意值是否为十进制金额文本；图表坐标与明细字段共用同一形态约束。
bool isDecimalAmountText(Object? value) =>
    value is String && _decimalAmountText.hasMatch(value);

/// 读取必填的十进制金额字符串。
///
/// 字段名 [field] 用于定位协议违例。服务端协议中这些汇总金额均为必填：
/// 缺失或形态不符时在解析边界抛出 [FormatException]，由上层进入可重试的
/// 错误态，绝不伪造 0 假装是真实金融读数。
String requiredDecimalAmount(Map<String, Object?> json, String field) {
  final value = json[field];
  if (value is! String || !_decimalAmountText.hasMatch(value)) {
    throw FormatException('字段 $field 必须是十进制金额字符串', value);
  }
  return value;
}

/// 读取必填的有限整数计数字段（仓位数、交易笔数）。
///
/// 非数值、非有限或带小数的计数都是协议违例；同样在解析边界失败。
int requiredCount(Map<String, Object?> json, String field) {
  final value = json[field];
  if (value is! num || !value.isFinite || value != value.truncate()) {
    throw FormatException('字段 $field 必须是整数', value);
  }
  return value.toInt();
}
