/// Provider-neutral market data. Monetary values stay decimal strings;
/// reference prices, contract multipliers and valuations come from the server.
typedef MarketPositionIdentity = ({
  String accountId,
  String symbolKey,
  String direction,
  String assetType,
  String? underlying,
  String? expiry,
  String? strike,
  String? optionType,
});

MarketPositionIdentity? marketPositionIdentity(Map<String, Object?> row) {
  String? field(String name) {
    final value = row[name];
    return value is String && value.trim().isNotEmpty ? value.trim() : null;
  }

  final accountId = field('accountId');
  final symbolKey = field('symbolKey');
  final direction = field('direction');
  final assetType = field('assetType');
  if (accountId == null ||
      symbolKey == null ||
      direction == null ||
      !['stock', 'option'].contains(assetType)) {
    return null;
  }
  final underlying = field('underlying')?.toUpperCase();
  final expiry = field('expiry');
  final strike = field('strike');
  final optionType = field('optionType')?.toUpperCase();
  if (assetType == 'option' &&
      (underlying == null ||
          expiry == null ||
          strike == null ||
          !RegExp(r'^\d+(?:\.\d+)?$').hasMatch(strike) ||
          !['C', 'P'].contains(optionType))) {
    return null;
  }
  return (
    accountId: accountId,
    symbolKey: symbolKey.toUpperCase(),
    direction: direction,
    assetType: assetType!,
    underlying: underlying,
    expiry: expiry,
    strike: strike == null ? null : _canonicalDecimal(strike),
    optionType: optionType,
  );
}

String _canonicalDecimal(String value) {
  final parts = value.split('.');
  final integer = parts.first.replaceFirst(RegExp(r'^0+(?=\d)'), '');
  final fraction = parts.length == 2
      ? parts.last.replaceFirst(RegExp(r'0+$'), '')
      : '';
  return fraction.isEmpty ? integer : '$integer.$fraction';
}

Map<String, Object?> _map(Object? value) {
  if (value is! Map<String, Object?>) {
    throw const FormatException('行情响应格式无效');
  }
  return value;
}

List<Object?> _list(Object? value) {
  if (value is! List<Object?>) {
    throw const FormatException('行情列表格式无效');
  }
  return value;
}

String _text(Object? value) {
  if (value is! String || value.isEmpty) {
    throw const FormatException('行情字段格式无效');
  }
  return value;
}

String? _decimal(Object? value) {
  if (value == null) return null;
  if (value is! String || !RegExp(r'^-?\d+(?:\.\d+)?$').hasMatch(value)) {
    throw const FormatException('行情金额必须为十进制字符串');
  }
  return value;
}

String _timestamp(Object? value) {
  final text = _text(value);
  if (!RegExp(r'(?:Z|[+-]\d{2}:\d{2})$').hasMatch(text) ||
      DateTime.tryParse(text) == null) {
    throw const FormatException('行情时间必须包含有效时区');
  }
  return text;
}

int _count(Object? value) {
  if (value is! int || value < 0) {
    throw const FormatException('行情数量格式无效');
  }
  return value;
}

bool _flag(Object? value) {
  if (value is! bool) throw const FormatException('行情状态格式无效');
  return value;
}

class MarketPricePoint {
  const MarketPricePoint({required this.value, required this.asOf});
  final String value;
  final String asOf;

  factory MarketPricePoint.fromJson(Object? value) {
    final json = _map(value);
    final amount = _decimal(json['value']);
    if (amount == null) throw const FormatException('报价缺少价格');
    return MarketPricePoint(value: amount, asOf: _timestamp(json['asOf']));
  }
}

class MarketQuote {
  MarketQuote.fromJson(Map<String, Object?> json)
    : instrumentKey = _text(json['instrumentKey']),
      providerId = _text(json['providerId']),
      feed = _text(json['feed']),
      currency = _text(json['currency']),
      fetchedAt = _timestamp(json['fetchedAt']),
      timingKind = _text(_map(json['timing'])['kind']),
      delayMinutes = _map(json['timing'])['delayMinutes'] == null
          ? null
          : _count(_map(json['timing'])['delayMinutes']),
      prices = Map.unmodifiable({
        for (final field in ['bid', 'ask', 'last', 'close'])
          field: json[field] == null
              ? null
              : MarketPricePoint.fromJson(json[field]),
      }) {
    if (!['delayed', 'end_of_day'].contains(timingKind)) {
      throw const FormatException('行情时效格式无效');
    }
  }

  final String instrumentKey;
  final String providerId;
  final String feed;
  final String currency;
  final String fetchedAt;
  final String timingKind;
  final int? delayMinutes;
  final Map<String, MarketPricePoint?> prices;
}

class MarketPosition {
  MarketPosition.fromJson(Map<String, Object?> json)
    : position = Map.unmodifiable(json),
      currency = _text(json['currency']),
      instrumentKey = json['instrumentKey'] as String?,
      status = _text(_map(json['quote'])['status']),
      issue = _map(json['quote'])['issue'] as String?,
      cached = _flag(_map(json['quote'])['cached']),
      quote = _map(json['quote'])['quote'] == null
          ? null
          : MarketQuote.fromJson(_map(_map(json['quote'])['quote'])),
      referencePrice = _map(json['quote'])['referencePrice'] == null
          ? null
          : MarketPricePoint.fromJson(_map(json['quote'])['referencePrice']),
      referenceBasis = _map(json['quote'])['referencePrice'] == null
          ? null
          : _text(_map(_map(json['quote'])['referencePrice'])['basis']),
      marketValue = _decimal(_map(json['valuation'])['marketValue']),
      unrealizedPnl = _decimal(_map(json['valuation'])['unrealizedPnl']),
      multiplier = _decimal(_map(json['valuation'])['multiplier']) {
    if (!['available', 'stale', 'unavailable'].contains(status) ||
        _map(json['valuation'])['basis'] != 'reference_price_excluding_fees' ||
        (referenceBasis != null &&
            !['mid', 'last', 'close'].contains(referenceBasis)) ||
        (quote != null &&
            (quote!.currency != currency ||
                quote!.instrumentKey != instrumentKey)) ||
        (marketValue != null && (referencePrice == null || quote == null)) ||
        (unrealizedPnl != null && marketValue == null) ||
        (status == 'unavailable' &&
            (referencePrice != null ||
                marketValue != null ||
                unrealizedPnl != null))) {
      throw const FormatException('行情与估值口径不一致');
    }
  }

  final Map<String, Object?> position;
  final String currency;
  final String? instrumentKey;
  final String status;
  final String? issue;
  final bool cached;
  final MarketQuote? quote;
  final MarketPricePoint? referencePrice;
  final String? referenceBasis;
  final String? marketValue;
  final String? unrealizedPnl;
  final String? multiplier;
}

class MarketCurrencyGroup {
  MarketCurrencyGroup.fromJson(Map<String, Object?> json)
    : currency = _text(json['currency']),
      positions = _count(json['positions']),
      pricedPositions = _count(json['pricedPositions']),
      complete = _flag(json['complete']),
      marketValue = _decimal(json['marketValue']),
      unrealizedPnl = _decimal(json['unrealizedPnl']),
      asOfFrom = json['asOfFrom'] == null ? null : _timestamp(json['asOfFrom']),
      asOfTo = json['asOfTo'] == null ? null : _timestamp(json['asOfTo']) {
    if (pricedPositions > positions ||
        (complete && pricedPositions != positions) ||
        ((asOfFrom == null) != (asOfTo == null))) {
      throw const FormatException('行情覆盖范围无效');
    }
  }

  final String currency;
  final int positions;
  final int pricedPositions;
  final bool complete;
  final String? marketValue;
  final String? unrealizedPnl;
  final String? asOfFrom;
  final String? asOfTo;
}

class MarketPositionsRead {
  MarketPositionsRead.fromJson(Map<String, Object?> json)
    : ledgerVersion = _count(_map(json['metadata'])['ledgerVersion']),
      fetchedAt = _timestamp(_map(json['metadata'])['fetchedAt']),
      count = _count(_map(json['data'])['count']),
      pricedCount = _count(_map(json['data'])['pricedCount']),
      complete = _flag(_map(json['data'])['complete']),
      enabled = _flag(_map(_map(json['data'])['marketData'])['enabled']),
      rows = List.unmodifiable(
        _list(_map(json['data'])['rows'])
            .map((value) => MarketPosition.fromJson(_map(value))),
      ),
      currencyGroups = List.unmodifiable(
        _list(_map(json['data'])['currencyGroups'])
            .map((value) => MarketCurrencyGroup.fromJson(_map(value))),
      ) {
    if (_map(json['data'])['basis'] !=
            'current_positions_at_quote_times_excluding_fees' ||
        count != rows.length ||
        pricedCount > count ||
        (complete && pricedCount != count)) {
      throw const FormatException('持仓估值响应格式无效');
    }
  }

  final int ledgerVersion;
  final String fetchedAt;
  final int count;
  final int pricedCount;
  final bool complete;
  final bool enabled;
  final List<MarketPosition> rows;
  final List<MarketCurrencyGroup> currencyGroups;

  /// Never join options by underlying alone. Ambiguous or incomplete identities
  /// remain unpriced rather than borrowing another contract/account's value.
  MarketPosition? match(
    Map<String, Object?> position, {
    required String? currency,
  }) {
    final identity = marketPositionIdentity(position);
    if (identity == null || currency == null) return null;
    final matches = rows
        .where(
          (row) =>
              row.currency == currency &&
              marketPositionIdentity(row.position) == identity,
        )
        .toList(growable: false);
    return matches.length == 1 ? matches.single : null;
  }
}
