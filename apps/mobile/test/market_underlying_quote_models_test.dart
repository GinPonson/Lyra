import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/market/market_underlying_quote_models.dart';

import 'support/ledger_fixture.dart';
import 'support/market_fixture.dart';
import 'support/market_underlying_quote_fixture.dart';

/// Creates a real source-row identity with optional, intentionally absent OCC fields.
Map<String, Object?> _position({
  required String symbolKey,
  required String assetType,
  String direction = 'long',
  String? underlying = 'AAPL',
  String? currency = 'USD',
  bool includeOptionDetails = false,
}) => {
  'accountId': LedgerFixture.account,
  'symbolKey': symbolKey,
  'assetType': assetType,
  'direction': direction,
  'underlying': ?underlying,
  'currency': ?currency,
  if (includeOptionDetails && assetType == 'option') ...{
    'strike': '385.25',
    'expiry': '2027-03-19',
    'optionType': 'C',
  },
};

/// Creates the checked summary query used by all response tests.
MarketUnderlyingQuoteQuery _query({
  String symbolKey = MarketFixture.callSymbol,
  String assetType = 'option',
  String direction = 'long',
  String? underlying = 'AAPL',
  String? currency = 'USD',
}) => MarketUnderlyingQuoteQuery.fromPosition(
  position: _position(
    symbolKey: symbolKey,
    assetType: assetType,
    direction: direction,
    underlying: underlying,
    currency: currency,
  ),
  ledgerVersion: 12,
  originScope: 'all',
);

/// Builds a response fixture whose requested row is the option anchor by default.
Map<String, Object?> _response({
  MarketUnderlyingQuoteFixture? fixture,
  MarketUnderlyingQuoteQuery? query,
}) {
  final source = fixture ?? MarketUnderlyingQuoteFixture();
  final request = query ?? _query();
  source.positionAssetTypeOverride ??= request.positionAssetType;
  source.instrumentTicker = 'AAPL';
  return source.underlyingQuoteResponse({
    'scope': request.accountId,
    'symbol_key': request.symbolKey,
    'direction': request.direction,
    'ledger_version': request.ledgerVersion.toString(),
    'settings_revision': '7',
  });
}

void main() {
  test('仅期权锚点即使缺少 OCC 字段仍生成正股摘要查询', () {
    final query = _query();

    expect(query.accountId, LedgerFixture.account);
    expect(query.symbolKey, MarketFixture.callSymbol);
    expect(query.direction, 'long');
    expect(query.positionAssetType, 'option');
    expect(query.expectedInstrumentKey, 'US:stock:AAPL');
    expect(query.expectedCurrency, 'USD');
  });

  test('股票 opaque symbol_key 优先使用真实 underlying 作为证券键', () {
    final query = _query(
      symbolKey: 'broker-stock-row-17',
      assetType: 'stock',
      underlying: 'aapl',
    );

    expect(query.symbolKey, 'broker-stock-row-17');
    expect(query.expectedInstrumentKey, 'US:stock:AAPL');
  });

  test('缺少币种或期权 underlying 时拒绝构造查询', () {
    expect(() => _query(currency: null), throwsA(isA<FormatException>()));
    expect(() => _query(underlying: null), throwsA(isA<FormatException>()));
  });

  test('解析保留报价精度、UTC 时点与前收交易日', () {
    final query = _query();
    final read = MarketUnderlyingQuoteRead.fromJson(
      _response(query: query),
      query: query,
      settingsRevision: 7,
    );

    expect(read.price?.value, '205.1250');
    expect(read.price?.asOf, '2026-09-04T19:45:00.000Z');
    expect(read.previousClose?.value, '200.0000');
    expect(read.previousClose?.sessionDate, '2026-09-03');
    expect(read.changePercent, '2.5625');
  });

  test('前收缺失时保留可用现价并要求明确涨跌缺失原因', () {
    final query = _query();
    final fixture = MarketUnderlyingQuoteFixture()
      ..positionAssetTypeOverride = 'option'
      ..previousCloseValue = null
      ..changeIssueValue = 'previous_close_unavailable';
    final read = MarketUnderlyingQuoteRead.fromJson(
      _response(fixture: fixture, query: query),
      query: query,
      settingsRevision: 7,
    );

    expect(read.price?.value, '205.1250');
    expect(read.previousClose, isNull);
    expect(read.changePercent, isNull);
    expect(read.changeIssue, 'previous_close_unavailable');
  });

  test('负值与零涨跌幅都按原始十进制字符串保留', () {
    final query = _query();
    final fixture = MarketUnderlyingQuoteFixture()
      ..positionAssetTypeOverride = 'option'
      ..changePercentValue = '-0.1250';
    final negative = MarketUnderlyingQuoteRead.fromJson(
      _response(fixture: fixture, query: query),
      query: query,
      settingsRevision: 7,
    );
    fixture.changePercentValue = '0.0000';
    final zero = MarketUnderlyingQuoteRead.fromJson(
      _response(fixture: fixture, query: query),
      query: query,
      settingsRevision: 7,
    );

    expect(negative.changePercent, '-0.1250');
    expect(zero.changePercent, '0.0000');
  });

  test('服务端拒绝时允许无抓取时点，但不允许夹带价格', () {
    final query = _query();
    final fixture = MarketUnderlyingQuoteFixture()
      ..issue = 'not_configured'
      ..positionAssetTypeOverride = 'option';
    final read = MarketUnderlyingQuoteRead.fromJson(
      _response(fixture: fixture, query: query),
      query: query,
      settingsRevision: 7,
    );
    expect(read.status, 'unavailable');
    expect(read.issue, 'not_configured');
    expect(read.fetchedAt, isNull);
    expect(read.price, isNull);

    final body = _response(fixture: fixture, query: query);
    (body['data']! as Map<String, Object?>)['price'] = {
      'value': '205.1250',
      'asOf': '2026-09-04T19:45:00.000Z',
    };
    expect(
      () => MarketUnderlyingQuoteRead.fromJson(
        body,
        query: query,
        settingsRevision: 7,
      ),
      throwsA(isA<FormatException>()),
    );
  });

  test('不可用响应可不含证券键，但不能携带价格或涨跌数据', () {
    final query = _query();
    final unsupported = MarketUnderlyingQuoteFixture()
      ..positionAssetTypeOverride = 'option'
      ..issue = 'currency_unsupported'
      ..nullInstrumentKey = true;
    final read = MarketUnderlyingQuoteRead.fromJson(
      _response(fixture: unsupported, query: query),
      query: query,
      settingsRevision: 7,
    );
    expect(read.status, 'unavailable');
    expect(read.instrumentKey, isNull);
    expect(read.issue, 'currency_unsupported');

    final body = _response(fixture: unsupported, query: query);
    final data = body['data']! as Map<String, Object?>;
    data['price'] = {'value': '205.1250', 'asOf': '2026-09-04T19:45:00.000Z'};
    expect(
      () => MarketUnderlyingQuoteRead.fromJson(
        body,
        query: query,
        settingsRevision: 7,
      ),
      throwsA(isA<FormatException>()),
    );
  });

  test('不同账户、标的、方向、类型、币种和版本的响应均被拒绝', () {
    final query = _query();
    final mutations = <void Function(MarketUnderlyingQuoteFixture)>[
      (fixture) =>
          fixture.accountOverride = 'a2000000-0000-4000-8000-000000000002',
      (fixture) => fixture.symbolOverride = 'other-option-row',
      (fixture) => fixture.directionOverride = 'short',
      (fixture) => fixture.positionAssetTypeOverride = 'stock',
      (fixture) => fixture.instrumentKeyOverride = 'US:stock:MSFT',
      (fixture) => fixture.responseCurrencyOverride = 'EUR',
      (fixture) => fixture.ledgerVersionOverride = 13,
      (fixture) => fixture.settingsRevisionOverride = 8,
    ];

    for (final mutate in mutations) {
      final fixture = MarketUnderlyingQuoteFixture()
        ..positionAssetTypeOverride = 'option';
      mutate(fixture);
      expect(
        () => MarketUnderlyingQuoteRead.fromJson(
          _response(fixture: fixture, query: query),
          query: query,
          settingsRevision: 7,
        ),
        throwsA(isA<FormatException>()),
      );
    }
  });

  test('零价格、非 UTC 时间和不匹配的前收状态均被拒绝', () {
    final query = _query();
    final fixture = MarketUnderlyingQuoteFixture()
      ..positionAssetTypeOverride = 'option'
      ..priceValue = '0.0000';
    expect(
      () => MarketUnderlyingQuoteRead.fromJson(
        _response(fixture: fixture, query: query),
        query: query,
        settingsRevision: 7,
      ),
      throwsA(isA<FormatException>()),
    );

    fixture
      ..priceValue = '205.1250'
      ..priceAsOfValue = '2026-09-04T19:45:00-04:00';
    expect(
      () => MarketUnderlyingQuoteRead.fromJson(
        _response(fixture: fixture, query: query),
        query: query,
        settingsRevision: 7,
      ),
      throwsA(isA<FormatException>()),
    );

    fixture
      ..priceAsOfValue = '2026-09-04T19:45:00.000Z'
      ..previousCloseValue = null
      ..changePercentValue = '1.0';
    final body = _response(fixture: fixture, query: query);
    (body['data']! as Map<String, Object?>)['changePercent'] = '1.0';
    expect(
      () => MarketUnderlyingQuoteRead.fromJson(
        body,
        query: query,
        settingsRevision: 7,
      ),
      throwsA(isA<FormatException>()),
    );
  });
}
