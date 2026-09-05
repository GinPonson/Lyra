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
      netDeposits: json['netDeposits'] as String? ?? '0',
      realizedPnlTotal: json['realizedPnlTotal'] as String? ?? '0',
      realizedPnlStock: json['realizedPnlStock'] as String? ?? '0',
      realizedPnlOption: json['realizedPnlOption'] as String? ?? '0',
      totalFees: json['totalFees'] as String? ?? '0',
      dividends: json['dividends'] as String? ?? '0',
      interest: json['interest'] as String? ?? '0',
      taxes: json['taxes'] as String? ?? '0',
      cashBalance: json['cashBalance'] as String? ?? '0',
      openPositions: (json['openPositions'] as num?)?.toInt() ?? 0,
      transactionCount: (json['transactionCount'] as num?)?.toInt() ?? 0,
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
