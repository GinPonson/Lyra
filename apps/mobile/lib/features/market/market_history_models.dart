import 'package:flutter/foundation.dart';

import 'market_models.dart';

/// Selects one server-supported history period and its fixed bar resolution.
enum MarketHistoryRange {
  day('1d', '1日', '15Min'),
  week('1w', '1周', '1Hour'),
  month('1m', '1月', '1Day'),
  quarter('3m', '3月', '1Day');

  const MarketHistoryRange(this.apiValue, this.label, this.timeframe);

  final String apiValue;
  final String label;
  final String timeframe;

  /// Returns the known range for [value], or throws on an unknown server value.
  static MarketHistoryRange parse(String value) {
    for (final range in MarketHistoryRange.values) {
      if (range.apiValue == value) return range;
    }
    throw const FormatException('行情历史周期无效');
  }
}

/// Selects whether history follows the exact position or its underlying stock.
enum MarketHistoryTarget {
  position('position'),
  underlying('underlying');

  const MarketHistoryTarget(this.apiValue);

  final String apiValue;

  /// Parses one supported target and rejects unknown server values.
  static MarketHistoryTarget parse(String value) => switch (value) {
    'position' => MarketHistoryTarget.position,
    'underlying' => MarketHistoryTarget.underlying,
    _ => throw const FormatException('行情历史目标无效'),
  };
}

typedef _HistorySourceIdentity = ({
  String accountId,
  String symbolKey,
  String normalizedSymbolKey,
  String direction,
  String assetType,
  String? underlying,
});

/// Immutable query identity for one source position, target and history period.
@immutable
class MarketHistoryQuery {
  const MarketHistoryQuery({
    required this.originScope,
    required this.accountId,
    required this.symbolKey,
    required this.direction,
    required this.target,
    required this.sourceAssetType,
    required this.assetType,
    required this.expectedCurrency,
    required this.expectedInstrumentKey,
    required this.ledgerVersion,
    required this.range,
  });

  /// Captures the global ledger scope so a later scope change invalidates reads.
  final String originScope;
  final String accountId;
  final String symbolKey;
  final String direction;
  final MarketHistoryTarget target;
  final String sourceAssetType;

  /// Asset type of the requested time series, which can differ from its row.
  final String assetType;
  final String expectedCurrency;
  final String expectedInstrumentKey;
  final int ledgerVersion;
  final MarketHistoryRange range;

  /// Builds a history identity from a validated row without fabricating a stock row.
  factory MarketHistoryQuery.fromPosition({
    required Map<String, Object?> position,
    required int ledgerVersion,
    required String originScope,
    required MarketHistoryRange range,
    MarketHistoryTarget target = MarketHistoryTarget.position,
  }) {
    final source = _historySourceIdentity(position);
    final currency = position['currency'];
    if (currency is! String || !RegExp(r'^[A-Za-z]{3}$').hasMatch(currency)) {
      throw const FormatException('持仓币种缺失，无法安全读取行情历史');
    }
    final positionIdentity = target == MarketHistoryTarget.position
        ? marketPositionIdentity(position)
        : null;
    if (source == null ||
        (target == MarketHistoryTarget.position && positionIdentity == null) ||
        ledgerVersion < 0 ||
        originScope.trim().isEmpty ||
        !_isUuid(source.accountId)) {
      throw const FormatException('持仓缺少有效的行情历史标识');
    }
    final underlying = source.underlying ?? source.normalizedSymbolKey;
    final seriesAssetType = switch (target) {
      MarketHistoryTarget.position => source.assetType,
      MarketHistoryTarget.underlying => 'stock',
    };
    final instrumentKey = switch (target) {
      MarketHistoryTarget.position when source.assetType == 'option' =>
        'US:option:${positionIdentity!.underlying}:'
            '${positionIdentity.expiry}:${positionIdentity.optionType}:'
            '${positionIdentity.strike}',
      MarketHistoryTarget.position => 'US:stock:$underlying',
      MarketHistoryTarget.underlying => 'US:stock:$underlying',
    };
    return MarketHistoryQuery(
      originScope: originScope,
      accountId: source.accountId,
      symbolKey: source.symbolKey,
      direction: source.direction,
      target: target,
      sourceAssetType: source.assetType,
      assetType: seriesAssetType,
      expectedCurrency: currency,
      expectedInstrumentKey: instrumentKey,
      ledgerVersion: ledgerVersion,
      range: range,
    );
  }

  /// Query families use value equality so range changes cannot share a request.
  @override
  bool operator ==(Object other) =>
      other is MarketHistoryQuery &&
      originScope == other.originScope &&
      accountId == other.accountId &&
      symbolKey == other.symbolKey &&
      direction == other.direction &&
      target == other.target &&
      sourceAssetType == other.sourceAssetType &&
      assetType == other.assetType &&
      expectedCurrency == other.expectedCurrency &&
      expectedInstrumentKey == other.expectedInstrumentKey &&
      ledgerVersion == other.ledgerVersion &&
      range == other.range;

  @override
  int get hashCode => Object.hash(
    originScope,
    accountId,
    symbolKey,
    direction,
    target,
    sourceAssetType,
    assetType,
    expectedCurrency,
    expectedInstrumentKey,
    ledgerVersion,
    range,
  );
}

/// Provider timing metadata returned with the historical bars.
@immutable
class MarketHistoryTiming {
  /// Creates provider timing metadata from the server's stable fields.
  const MarketHistoryTiming({required this.kind, required this.delayMinutes});

  final String kind;
  final int? delayMinutes;
}

/// One real provider bar; each OHLC value remains the original decimal text.
@immutable
class MarketHistoryBar {
  /// Stores OHLC values without rounding their original decimal strings.
  const MarketHistoryBar({
    required this.timestamp,
    required this.open,
    required this.high,
    required this.low,
    required this.close,
    required this.volume,
  });

  /// UTC start of the bar interval, not the time of the final trade.
  final String timestamp;
  final String open;
  final String high;
  final String low;
  final String close;
  final int volume;

  /// Parses one interval and rejects malformed prices, OHLC ordering or volume.
  factory MarketHistoryBar.fromJson(Object? value) {
    final json = _object(value, '行情区间格式无效');
    final bar = MarketHistoryBar(
      timestamp: _utcTimestamp(json['timestamp']),
      open: _price(json['open']),
      high: _price(json['high']),
      low: _price(json['low']),
      close: _price(json['close']),
      volume: _nonNegativeInt(json['volume'], '成交量格式无效'),
    );
    if (_comparePrices(bar.high, bar.low) < 0 ||
        _comparePrices(bar.open, bar.low) < 0 ||
        _comparePrices(bar.open, bar.high) > 0 ||
        _comparePrices(bar.close, bar.low) < 0 ||
        _comparePrices(bar.close, bar.high) > 0) {
      throw const FormatException('行情区间 OHLC 价格关系无效');
    }
    if (bar.volume == 0) {
      throw const FormatException('行情区间必须包含实际成交量');
    }
    return bar;
  }
}

/// A validated series tied to one exact holding, period, settings revision and ledger.
@immutable
class MarketHistoryRead {
  const MarketHistoryRead._({
    required this.accountId,
    required this.symbolKey,
    required this.direction,
    required this.target,
    required this.positionAssetType,
    required this.assetType,
    required this.currency,
    required this.instrumentKey,
    required this.range,
    required this.timeframe,
    required this.status,
    required this.issue,
    required this.cached,
    required this.providerId,
    required this.feed,
    required this.timing,
    required this.adjustment,
    required this.expired,
    required this.cutoff,
    required this.fetchedAt,
    required this.metadataFetchedAt,
    required this.ledgerVersion,
    required this.settingsRevision,
    required this.bars,
  });

  /// Parses and verifies a response against the exact request that produced it.
  factory MarketHistoryRead.fromJson(
    Object? value, {
    required MarketHistoryQuery query,
    required int settingsRevision,
  }) {
    final json = _object(value, '行情历史响应格式无效');
    final metadata = _object(json['metadata'], '行情历史元数据格式无效');
    final accountScope = _object(metadata['accountScope'], '行情历史账户范围格式无效');
    final data = _object(json['data'], '行情历史数据格式无效');
    final accountId = _text(data['accountId'], '行情历史账户标识无效');
    final symbolKey = _text(data['symbolKey'], '行情历史标的标识无效');
    final direction = _text(data['direction'], '行情历史方向无效');
    final target = MarketHistoryTarget.parse(_text(data['target'], '行情历史目标无效'));
    final positionAssetType = _text(
      data['positionAssetType'],
      '行情历史来源持仓资产类型无效',
    );
    final assetType = _text(data['assetType'], '行情历史资产类型无效');
    final currency = _text(data['currency'], '行情历史币种无效');
    final range = MarketHistoryRange.parse(_text(data['range'], '行情历史周期无效'));
    final timeframe = _text(data['timeframe'], '行情历史粒度无效');
    final status = _text(data['status'], '行情历史状态无效');
    final issue = _nullableText(data['issue'], '行情历史错误码无效');
    final instrumentKey = _nullableText(data['instrumentKey'], '行情历史合约标识无效');
    final providerId = _nullableText(data['providerId'], '行情来源标识无效');
    final feed = _nullableText(data['feed'], '行情数据类型无效');
    final timingJson = _object(data['timing'], '行情历史时效格式无效');
    final timing = MarketHistoryTiming(
      kind: _text(timingJson['kind'], '行情历史时效类型无效'),
      delayMinutes: timingJson['delayMinutes'] == null
          ? null
          : _nonNegativeInt(timingJson['delayMinutes'], '延迟时间格式无效'),
    );
    final adjustment = _text(data['adjustment'], '复权口径无效');
    final expired = _boolean(data['expired'], '期权到期状态无效');
    final cutoff = _utcTimestamp(data['cutoff']);
    final fetchedAt = _utcTimestamp(data['fetchedAt']);
    final metadataFetchedAt = _utcTimestamp(metadata['fetchedAt']);
    final responseLedgerVersion = _nonNegativeInt(
      metadata['ledgerVersion'],
      '行情历史账本版本无效',
    );
    final responseSettingsRevision = _nonNegativeInt(
      metadata['settingsRevision'],
      '行情历史设置版本无效',
    );
    final responseAccount = _text(accountScope['account'], '行情历史账户范围无效');
    if (accountScope['kind'] != 'account' ||
        responseAccount != query.accountId ||
        accountId != query.accountId ||
        symbolKey != query.symbolKey ||
        direction != query.direction ||
        target != query.target ||
        positionAssetType != query.sourceAssetType ||
        assetType != query.assetType ||
        (target == MarketHistoryTarget.position &&
            assetType != positionAssetType) ||
        (target == MarketHistoryTarget.underlying && assetType != 'stock') ||
        currency != query.expectedCurrency ||
        (instrumentKey != null &&
            instrumentKey != query.expectedInstrumentKey) ||
        (status == 'available' &&
            instrumentKey != query.expectedInstrumentKey) ||
        range != query.range ||
        timeframe != query.range.timeframe ||
        responseLedgerVersion != query.ledgerVersion ||
        responseSettingsRevision != settingsRevision ||
        settingsRevision < 0 ||
        !RegExp(r'^[A-Za-z]{3}$').hasMatch(currency) ||
        !['stock', 'option'].contains(positionAssetType) ||
        !['stock', 'option'].contains(assetType) ||
        !['long', 'short'].contains(direction) ||
        !['available', 'unavailable'].contains(status) ||
        !['delayed', 'end_of_day'].contains(timing.kind) ||
        (timing.kind == 'delayed' && timing.delayMinutes == null) ||
        (timing.kind == 'end_of_day' && timing.delayMinutes != null) ||
        !['split', 'raw'].contains(adjustment) ||
        (assetType == 'stock' && adjustment != 'split') ||
        (assetType == 'option' && adjustment != 'raw') ||
        (assetType == 'stock' && expired)) {
      throw const FormatException('行情历史响应与持仓或查询条件不匹配');
    }
    if (instrumentKey != null && instrumentKey.isEmpty) {
      throw const FormatException('行情历史合约标识无效');
    }
    if ((status == 'available' && (providerId == null || feed == null)) ||
        (providerId == null && feed != null)) {
      throw const FormatException('行情来源与数据类型无效');
    }
    final rawBars = _array(data['bars'], '行情历史缺少区间列表');
    final bars = rawBars.map(MarketHistoryBar.fromJson).toList(growable: false);
    DateTime? previous;
    for (final bar in bars) {
      final timestamp = DateTime.parse(bar.timestamp);
      if (previous != null && !timestamp.isAfter(previous)) {
        throw const FormatException('行情区间必须按时间严格升序且不能重复');
      }
      previous = timestamp;
    }
    if ((status == 'available' && (bars.isEmpty || issue != null)) ||
        (status == 'unavailable' && (bars.isNotEmpty || issue == null)) ||
        (issue != null && issue.isEmpty)) {
      throw const FormatException('行情历史状态与区间列表不匹配');
    }
    return MarketHistoryRead._(
      accountId: accountId,
      symbolKey: symbolKey,
      direction: direction,
      target: target,
      positionAssetType: positionAssetType,
      assetType: assetType,
      currency: currency,
      instrumentKey: instrumentKey,
      range: range,
      timeframe: timeframe,
      status: status,
      issue: issue,
      cached: _boolean(data['cached'], '行情缓存状态无效'),
      providerId: providerId,
      feed: feed,
      timing: timing,
      adjustment: adjustment,
      expired: expired,
      cutoff: cutoff,
      fetchedAt: fetchedAt,
      metadataFetchedAt: metadataFetchedAt,
      ledgerVersion: responseLedgerVersion,
      settingsRevision: responseSettingsRevision,
      bars: List.unmodifiable(bars),
    );
  }

  final String accountId;
  final String symbolKey;
  final String direction;
  final MarketHistoryTarget target;
  final String positionAssetType;
  final String assetType;
  final String currency;
  final String? instrumentKey;
  final MarketHistoryRange range;
  final String timeframe;
  final String status;
  final String? issue;
  final bool cached;
  final String? providerId;
  final String? feed;
  final MarketHistoryTiming timing;
  final String adjustment;
  final bool expired;
  final String cutoff;
  final String fetchedAt;
  final String metadataFetchedAt;
  final int ledgerVersion;
  final int settingsRevision;
  final List<MarketHistoryBar> bars;
}

/// Extracts the source row identity without requiring option contract details.
_HistorySourceIdentity? _historySourceIdentity(Map<String, Object?> row) {
  String? field(String name) {
    final value = row[name];
    return value is String && value.trim().isNotEmpty ? value.trim() : null;
  }

  final accountId = field('accountId');
  final symbolKey = field('symbolKey');
  final direction = field('direction');
  final assetType = field('assetType');
  final underlying = field('underlying')?.toUpperCase();
  if (accountId == null ||
      symbolKey == null ||
      direction == null ||
      !['long', 'short'].contains(direction) ||
      !['stock', 'option'].contains(assetType) ||
      (assetType == 'option' && underlying == null)) {
    return null;
  }
  return (
    accountId: accountId,
    symbolKey: symbolKey,
    normalizedSymbolKey: symbolKey.toUpperCase(),
    direction: direction,
    assetType: assetType!,
    underlying: underlying,
  );
}

/// Requires a JSON object and reports [message] when its shape is invalid.
Map<String, Object?> _object(Object? value, String message) {
  if (value is! Map<String, Object?>) throw FormatException(message);
  return value;
}

/// Requires a JSON array and reports [message] when its shape is invalid.
List<Object?> _array(Object? value, String message) {
  if (value is! List<Object?>) throw FormatException(message);
  return value;
}

/// Requires nonblank text and reports [message] when it is absent or invalid.
String _text(Object? value, String message) {
  if (value is! String || value.trim().isEmpty) throw FormatException(message);
  return value;
}

/// Parses nullable text while rejecting blank strings when a value is present.
String? _nullableText(Object? value, String message) {
  if (value == null) return null;
  return _text(value, message);
}

/// Requires an ISO timestamp explicitly marked with the UTC `Z` suffix.
String _utcTimestamp(Object? value) {
  if (value is! String ||
      !value.endsWith('Z') ||
      DateTime.tryParse(value) == null) {
    throw const FormatException('行情历史时间必须是有效 UTC 时间');
  }
  return value;
}

/// Requires a positive finite decimal string and preserves its exact text.
String _price(Object? value) {
  if (value is! String ||
      !RegExp(r'^\d+(?:\.\d+)?$').hasMatch(value) ||
      !RegExp(r'[1-9]').hasMatch(value)) {
    throw const FormatException('行情价格必须为正十进制字符串');
  }
  // Floating point is only used later for drawing; reject values that cannot
  // form finite canvas coordinates while retaining this original exact string.
  if (!double.parse(value).isFinite) {
    throw const FormatException('行情价格超出可显示范围');
  }
  return value;
}

/// Parses a nonnegative integer and reports [message] for any invalid value.
int _nonNegativeInt(Object? value, String message) {
  if (value is! int || value < 0) throw FormatException(message);
  return value;
}

/// Parses a required boolean and reports [message] for any invalid value.
bool _boolean(Object? value, String message) {
  if (value is! bool) throw FormatException(message);
  return value;
}

/// Returns whether [value] has the canonical UUID text shape.
bool _isUuid(String value) =>
    RegExp(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
        .hasMatch(value);

/// Compares decimal strings without rounding through binary floating point.
int _comparePrices(String left, String right) {
  final leftParts = left.split('.');
  final rightParts = right.split('.');
  final leftInteger = leftParts.first.replaceFirst(RegExp(r'^0+(?=\d)'), '');
  final rightInteger = rightParts.first.replaceFirst(RegExp(r'^0+(?=\d)'), '');
  if (leftInteger.length != rightInteger.length) {
    return leftInteger.length.compareTo(rightInteger.length);
  }
  final integerOrder = leftInteger.compareTo(rightInteger);
  if (integerOrder != 0) return integerOrder;
  final leftRawFraction = leftParts.length == 1 ? '' : leftParts.last;
  final rightRawFraction = rightParts.length == 1 ? '' : rightParts.last;
  final fractionLength = leftRawFraction.length > rightRawFraction.length
      ? leftRawFraction.length
      : rightRawFraction.length;
  final leftFraction = leftRawFraction.padRight(fractionLength, '0');
  final rightFraction = rightRawFraction.padRight(fractionLength, '0');
  return leftFraction.compareTo(rightFraction);
}
