import 'package:flutter/foundation.dart';

import 'market_history_models.dart';

/// Immutable identity for one underlying-stock quote summary and ledger version.
@immutable
class MarketUnderlyingQuoteQuery {
  const MarketUnderlyingQuoteQuery({
    required this.originScope,
    required this.accountId,
    required this.symbolKey,
    required this.direction,
    required this.positionAssetType,
    required this.expectedCurrency,
    required this.expectedInstrumentKey,
    required this.ledgerVersion,
  });

  /// The global account view whose changes invalidate this summary read.
  final String originScope;
  final String accountId;
  final String symbolKey;
  final String direction;
  final String positionAssetType;
  final String expectedCurrency;
  final String expectedInstrumentKey;
  final int ledgerVersion;

  /// Reuses the exact underlying identity rules without requiring OCC details.
  factory MarketUnderlyingQuoteQuery.fromPosition({
    required Map<String, Object?> position,
    required int ledgerVersion,
    required String originScope,
  }) {
    final identity = MarketHistoryQuery.fromPosition(
      position: position,
      ledgerVersion: ledgerVersion,
      originScope: originScope,
      range: MarketHistoryRange.month,
      target: MarketHistoryTarget.underlying,
    );
    return MarketUnderlyingQuoteQuery(
      originScope: identity.originScope,
      accountId: identity.accountId,
      symbolKey: identity.symbolKey,
      direction: identity.direction,
      positionAssetType: identity.sourceAssetType,
      expectedCurrency: identity.expectedCurrency,
      expectedInstrumentKey: identity.expectedInstrumentKey,
      ledgerVersion: identity.ledgerVersion,
    );
  }

  /// Compares all source-position fields so unrelated summaries never share data.
  @override
  bool operator ==(Object other) =>
      other is MarketUnderlyingQuoteQuery &&
      originScope == other.originScope &&
      accountId == other.accountId &&
      symbolKey == other.symbolKey &&
      direction == other.direction &&
      positionAssetType == other.positionAssetType &&
      expectedCurrency == other.expectedCurrency &&
      expectedInstrumentKey == other.expectedInstrumentKey &&
      ledgerVersion == other.ledgerVersion;

  @override
  int get hashCode => Object.hash(
    originScope,
    accountId,
    symbolKey,
    direction,
    positionAssetType,
    expectedCurrency,
    expectedInstrumentKey,
    ledgerVersion,
  );
}

/// One exact decimal price and its UTC observation timestamp.
@immutable
class MarketUnderlyingQuotePrice {
  const MarketUnderlyingQuotePrice({required this.value, required this.asOf});

  final String value;
  final String asOf;

  /// Parses a positive price without rounding its server-provided precision.
  factory MarketUnderlyingQuotePrice.fromJson(Object? value) {
    final json = _object(value, '正股价格格式无效');
    return MarketUnderlyingQuotePrice(
      value: _positiveDecimal(json['value'], '正股价格必须为正十进制字符串'),
      asOf: _utcTimestamp(json['asOf'], '正股报价时间必须是 UTC'),
    );
  }
}

/// One previous-session close whose date names the comparison session.
@immutable
class MarketUnderlyingPreviousClose {
  const MarketUnderlyingPreviousClose({
    required this.value,
    required this.sessionDate,
  });

  final String value;
  final String sessionDate;

  /// Parses a positive close and a real ISO calendar date.
  factory MarketUnderlyingPreviousClose.fromJson(Object? value) {
    final json = _object(value, '正股前收格式无效');
    return MarketUnderlyingPreviousClose(
      value: _positiveDecimal(json['value'], '正股前收必须为正十进制字符串'),
      sessionDate: _sessionDate(json['sessionDate']),
    );
  }
}

/// A validated, server-backed stock quote summary for one exact source row.
@immutable
class MarketUnderlyingQuoteRead {
  const MarketUnderlyingQuoteRead._({
    required this.accountId,
    required this.symbolKey,
    required this.direction,
    required this.positionAssetType,
    required this.assetType,
    required this.instrumentKey,
    required this.currency,
    required this.status,
    required this.issue,
    required this.cached,
    required this.providerId,
    required this.feed,
    required this.delayMinutes,
    required this.fetchedAt,
    required this.price,
    required this.previousClose,
    required this.changePercent,
    required this.changeIssue,
    required this.comparisonBasis,
    required this.adjustment,
    required this.metadataFetchedAt,
    required this.ledgerVersion,
    required this.settingsRevision,
  });

  /// Parses and binds a summary to its source row and both server revisions.
  factory MarketUnderlyingQuoteRead.fromJson(
    Object? value, {
    required MarketUnderlyingQuoteQuery query,
    required int settingsRevision,
  }) {
    final json = _object(value, '正股行情响应格式无效');
    final metadata = _object(json['metadata'], '正股行情元数据格式无效');
    final accountScope = _object(metadata['accountScope'], '账户范围格式无效');
    final data = _object(json['data'], '正股行情数据格式无效');
    final accountId = _text(data['accountId'], '正股行情账户标识无效');
    final symbolKey = _text(data['symbolKey'], '正股行情标的标识无效');
    final direction = _text(data['direction'], '正股行情持仓方向无效');
    final positionAssetType = _text(data['positionAssetType'], '正股行情来源持仓类型无效');
    final assetType = _text(data['assetType'], '正股行情资产类型无效');
    final instrumentKey = _nullableText(data['instrumentKey'], '正股行情证券标识无效');
    final currency = _text(data['currency'], '正股行情币种无效');
    final status = _text(data['status'], '正股行情状态无效');
    final issue = _nullableText(data['issue'], '正股行情错误码无效');
    final cached = _boolean(data['cached'], '行情缓存状态无效');
    final providerId = _nullableText(data['providerId'], '行情来源标识无效');
    final feed = _nullableText(data['feed'], '行情数据类型无效');
    final timing = _object(data['timing'], '正股行情时效格式无效');
    final timingKind = _text(timing['kind'], '正股行情时效类型无效');
    final delayMinutes = _nonNegativeInt(timing['delayMinutes'], '正股行情延迟时间无效');
    final fetchedAt = _nullableUtcTimestamp(data['fetchedAt'], '行情抓取时间必须是 UTC');
    final metadataFetchedAt = _utcTimestamp(
      metadata['fetchedAt'],
      '行情元数据时间必须是 UTC',
    );
    final responseLedgerVersion = _nonNegativeInt(
      metadata['ledgerVersion'],
      '正股行情账本版本无效',
    );
    final responseSettingsRevision = _nonNegativeInt(
      metadata['settingsRevision'],
      '正股行情设置版本无效',
    );
    final responseAccount = _text(accountScope['account'], '账户范围账户无效');
    final price = data['price'] == null
        ? null
        : MarketUnderlyingQuotePrice.fromJson(data['price']);
    final previousClose = data['previousClose'] == null
        ? null
        : MarketUnderlyingPreviousClose.fromJson(data['previousClose']);
    final changePercent = data['changePercent'] == null
        ? null
        : _signedDecimal(data['changePercent'], '涨跌幅格式无效');
    final changeIssue = _nullableText(data['changeIssue'], '涨跌幅错误码无效');
    final comparisonBasis = _text(data['comparisonBasis'], '涨跌比较口径无效');
    final adjustment = _text(data['adjustment'], '正股复权口径无效');

    if (accountScope['kind'] != 'account' ||
        responseAccount != query.accountId ||
        accountId != query.accountId ||
        symbolKey != query.symbolKey ||
        direction != query.direction ||
        positionAssetType != query.positionAssetType ||
        assetType != 'stock' ||
        (instrumentKey != null &&
            instrumentKey != query.expectedInstrumentKey) ||
        (status == 'available' && instrumentKey == null) ||
        currency != query.expectedCurrency ||
        responseLedgerVersion != query.ledgerVersion ||
        responseSettingsRevision != settingsRevision ||
        settingsRevision < 0 ||
        !RegExp(r'^[A-Za-z]{3}$').hasMatch(currency) ||
        !['stock', 'option'].contains(positionAssetType) ||
        !['long', 'short'].contains(direction) ||
        !['available', 'unavailable'].contains(status) ||
        timingKind != 'delayed' ||
        delayMinutes != 15 ||
        comparisonBasis != 'previous_session_close' ||
        adjustment != 'split') {
      throw const FormatException('正股行情响应与来源持仓或查询条件不匹配');
    }
    if ((status == 'available' &&
            (price == null || issue != null || fetchedAt == null)) ||
        (status == 'unavailable' &&
            (price != null || issue == null || issue.isEmpty))) {
      throw const FormatException('正股行情状态与价格不匹配');
    }
    if ((status == 'available' && (providerId == null || feed == null)) ||
        (providerId == null && feed != null)) {
      throw const FormatException('行情来源与数据类型无效');
    }
    if (status == 'unavailable' &&
        (previousClose != null ||
            changePercent != null ||
            changeIssue != null)) {
      throw const FormatException('不可用行情不能包含前收或涨跌幅');
    }
    if (status == 'available' &&
        (previousClose == null
            ? changePercent != null || changeIssue == null
            : changePercent == null || changeIssue != null)) {
      throw const FormatException('前收与涨跌幅状态不匹配');
    }

    return MarketUnderlyingQuoteRead._(
      accountId: accountId,
      symbolKey: symbolKey,
      direction: direction,
      positionAssetType: positionAssetType,
      assetType: assetType,
      instrumentKey: instrumentKey,
      currency: currency,
      status: status,
      issue: issue,
      cached: cached,
      providerId: providerId,
      feed: feed,
      delayMinutes: delayMinutes,
      fetchedAt: fetchedAt,
      price: price,
      previousClose: previousClose,
      changePercent: changePercent,
      changeIssue: changeIssue,
      comparisonBasis: comparisonBasis,
      adjustment: adjustment,
      metadataFetchedAt: metadataFetchedAt,
      ledgerVersion: responseLedgerVersion,
      settingsRevision: responseSettingsRevision,
    );
  }

  final String accountId;
  final String symbolKey;
  final String direction;
  final String positionAssetType;
  final String assetType;
  final String? instrumentKey;
  final String currency;
  final String status;
  final String? issue;
  final bool cached;
  final String? providerId;
  final String? feed;
  final int delayMinutes;
  final String? fetchedAt;
  final MarketUnderlyingQuotePrice? price;
  final MarketUnderlyingPreviousClose? previousClose;
  final String? changePercent;
  final String? changeIssue;
  final String comparisonBasis;
  final String adjustment;
  final String metadataFetchedAt;
  final int ledgerVersion;
  final int settingsRevision;
}

/// Requires a JSON object and reports [message] when its shape is invalid.
Map<String, Object?> _object(Object? value, String message) {
  if (value is! Map<String, Object?>) throw FormatException(message);
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

/// Parses a positive decimal and preserves the service's original precision.
String _positiveDecimal(Object? value, String message) {
  if (value is! String ||
      !RegExp(r'^\d+(?:\.\d+)?$').hasMatch(value) ||
      !RegExp(r'[1-9]').hasMatch(value)) {
    throw FormatException(message);
  }
  return value;
}

/// Parses a signed decimal string, including an exact zero, without conversion.
String _signedDecimal(Object? value, String message) {
  if (value is! String || !RegExp(r'^-?\d+(?:\.\d+)?$').hasMatch(value)) {
    throw FormatException(message);
  }
  return value;
}

/// Requires an ISO timestamp explicitly marked with the UTC `Z` suffix.
String _utcTimestamp(Object? value, String message) {
  if (value is! String ||
      !value.endsWith('Z') ||
      DateTime.tryParse(value) == null) {
    throw FormatException(message);
  }
  return value;
}

/// Parses optional UTC fetch time, absent when the provider did no fetch.
String? _nullableUtcTimestamp(Object? value, String message) =>
    value == null ? null : _utcTimestamp(value, message);

/// Validates and retains a real YYYY-MM-DD session date.
String _sessionDate(Object? value) {
  if (value is! String || !RegExp(r'^\d{4}-\d{2}-\d{2}$').hasMatch(value)) {
    throw const FormatException('前收交易日格式无效');
  }
  final parts = value.split('-').map(int.parse).toList(growable: false);
  final date = DateTime.utc(parts[0], parts[1], parts[2]);
  if (date.year != parts[0] || date.month != parts[1] || date.day != parts[2]) {
    throw const FormatException('前收交易日无效');
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
