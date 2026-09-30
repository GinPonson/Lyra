/// 同一标的、同一币种的展示分组；保留每条原始记录，不合计或冲销持仓。
class PositionGroup {
  const PositionGroup({
    required this.symbol,
    required this.currency,
    required this.rows,
  });
  final String symbol;
  final String? currency;
  final List<Map<String, Object?>> rows;

  /// [currencyFor] 从响应元数据解析每条持仓币种；输出保持首次出现的顺序。
  /// 无币种记录单独展示，避免把币种尚不明确的金额混到一起。
  static List<PositionGroup> fromRows(
    List<Map<String, Object?>> rows, {
    required String? Function(Map<String, Object?>) currencyFor,
  }) {
    final grouped = <(String, String?, int?), List<Map<String, Object?>>>{};
    for (final (index, row) in rows.indexed) {
      final symbol = row['underlying'] ?? row['symbolKey'];
      if (symbol is! String || symbol.trim().isEmpty) {
        throw const FormatException('持仓缺少有效标的代码');
      }
      final currency = currencyFor(row);
      final key = (
        symbol.trim().toUpperCase(),
        currency,
        currency == null ? index : null,
      );
      grouped.putIfAbsent(key, () => []).add(row);
    }
    return [
      for (final entry in grouped.entries)
        PositionGroup(
          symbol: entry.key.$1,
          currency: entry.key.$2,
          rows: List.unmodifiable(entry.value),
        ),
    ];
  }
}
