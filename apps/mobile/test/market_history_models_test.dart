import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/market/market_history_models.dart';

const _account = 'a2000000-0000-4000-8000-000000000001';

/// Creates a synthetic stock row with a selectable currency code.
Map<String, Object?> _stock({
  String currency = 'USD',
  String symbolKey = 'SYNTHETIC-LOT',
  String? underlying,
}) => {
  'accountId': _account,
  'symbolKey': symbolKey,
  'underlying': underlying,
  'assetType': 'stock',
  'direction': 'long',
  'currency': currency,
};

/// Creates one synthetic call or put identity for parser validation.
Map<String, Object?> _option({String optionType = 'C'}) => {
  'accountId': _account,
  'symbolKey': 'SYNTHETIC-OPTION',
  'underlying': 'SYNTHETIC',
  'assetType': 'option',
  'direction': 'short',
  'currency': 'USD',
  'expiry': '2027-03-19',
  'strike': '12.3400',
  'optionType': optionType,
};

/// Builds a consistent query for a fixture position and selected range.
MarketHistoryQuery _query(
  Map<String, Object?> position, {
  MarketHistoryRange range = MarketHistoryRange.month,
  MarketHistoryTarget target = MarketHistoryTarget.position,
}) => MarketHistoryQuery.fromPosition(
  position: position,
  ledgerVersion: 12,
  originScope: 'all',
  range: range,
  target: target,
);

/// Builds a synthetic response matching [query] unless a mismatch is requested.
Map<String, Object?> _response(
  MarketHistoryQuery query, {
  int settingsRevision = 7,
  String? instrumentKey,
  String? currency,
  MarketHistoryTarget? target,
  String? positionAssetType,
  String? assetType,
  bool expired = false,
  String? status = 'available',
  String? issue,
  List<Map<String, Object?>>? bars,
}) {
  final timestamp = '2026-09-04T19:30:00.000Z';
  final actualBars =
      bars ??
      (status == 'available'
          ? [
              {
                'timestamp': timestamp,
                'open': '12.340000',
                'high': '12.35',
                'low': '12.30',
                'close': '12.3456789',
                'volume': 17,
              },
            ]
          : <Map<String, Object?>>[]);
  return {
    'metadata': {
      'accountScope': {'kind': 'account', 'account': query.accountId},
      'ledgerVersion': query.ledgerVersion,
      'settingsRevision': settingsRevision,
      'fetchedAt': timestamp,
    },
    'data': {
      'accountId': query.accountId,
      'symbolKey': query.symbolKey,
      'direction': query.direction,
      'target': (target ?? query.target).apiValue,
      'positionAssetType': positionAssetType ?? query.sourceAssetType,
      'assetType': assetType ?? query.assetType,
      'currency': currency ?? query.expectedCurrency,
      'instrumentKey': instrumentKey ?? query.expectedInstrumentKey,
      'range': query.range.apiValue,
      'timeframe': query.range.timeframe,
      'status': status,
      'issue': issue,
      'cached': false,
      'providerId': status == 'unavailable' ? null : 'test-provider',
      'feed': status == 'unavailable' ? null : 'synthetic-bars',
      'timing': {'kind': 'delayed', 'delayMinutes': 16},
      'adjustment': (assetType ?? query.assetType) == 'stock' ? 'split' : 'raw',
      'expired': expired,
      'cutoff': timestamp,
      'fetchedAt': timestamp,
      'bars': actualBars,
    },
  };
}

void main() {
  group('MarketHistoryQuery', () {
    test('保留股票身份、币种、原始标识和服务端周期', () {
      final query = _query(_stock(), range: MarketHistoryRange.day);

      expect(query.accountId, _account);
      expect(query.symbolKey, 'SYNTHETIC-LOT');
      expect(query.expectedCurrency, 'USD');
      expect(query.expectedInstrumentKey, 'US:stock:SYNTHETIC-LOT');
      expect(query.range.timeframe, '15Min');

      final underlying = _query(
        _stock(),
        target: MarketHistoryTarget.underlying,
      );
      expect(underlying.sourceAssetType, 'stock');
      expect(underlying.assetType, 'stock');
      expect(underlying.expectedInstrumentKey, 'US:stock:SYNTHETIC-LOT');
      expect(underlying, isNot(query));
    });

    test('期权合约身份包含方向、看涨看跌及规范化行权价', () {
      final query = _query(_option());

      expect(query.direction, 'short');
      expect(
        query.expectedInstrumentKey,
        'US:option:SYNTHETIC:2027-03-19:C:12.34',
      );
      expect(
        _query(_option(optionType: 'P')).expectedInstrumentKey,
        'US:option:SYNTHETIC:2027-03-19:P:12.34',
      );
    });

    test('期权来源可单独请求正股且 target 参与缓存身份', () {
      final positionQuery = _query(_option());
      final underlyingQuery = _query(
        _option(),
        target: MarketHistoryTarget.underlying,
      );

      expect(underlyingQuery.accountId, positionQuery.accountId);
      expect(underlyingQuery.symbolKey, positionQuery.symbolKey);
      expect(underlyingQuery.direction, positionQuery.direction);
      expect(underlyingQuery.sourceAssetType, 'option');
      expect(underlyingQuery.assetType, 'stock');
      expect(underlyingQuery.expectedInstrumentKey, 'US:stock:SYNTHETIC');
      expect(underlyingQuery.target, MarketHistoryTarget.underlying);
      expect(underlyingQuery, isNot(positionQuery));
    });

    test('正股查询不依赖锚点期权的完整 OCC 字段', () {
      final incompleteOption = {
        ..._option(),
        'expiry': '2024-06-21',
        'strike': 'not-an-occ-strike',
        'optionType': null,
      };

      final underlyingQuery = _query(
        incompleteOption,
        target: MarketHistoryTarget.underlying,
      );
      expect(underlyingQuery.symbolKey, 'SYNTHETIC-OPTION');
      expect(underlyingQuery.sourceAssetType, 'option');
      expect(underlyingQuery.expectedInstrumentKey, 'US:stock:SYNTHETIC');
      expect(() => _query(incompleteOption), throwsFormatException);
    });

    test('股票来源优先使用券商提供的 underlying 且请求仍带原始 symbolKey', () {
      final row = _stock(symbolKey: 'broker-stock-id', underlying: 'AAPL');
      final positionQuery = _query(row);
      final underlyingQuery = _query(
        row,
        target: MarketHistoryTarget.underlying,
      );

      expect(positionQuery.symbolKey, 'broker-stock-id');
      expect(positionQuery.expectedInstrumentKey, 'US:stock:AAPL');
      expect(underlyingQuery.symbolKey, 'broker-stock-id');
      expect(underlyingQuery.expectedInstrumentKey, 'US:stock:AAPL');
      for (final query in [positionQuery, underlyingQuery]) {
        expect(
          MarketHistoryRead.fromJson(
            _response(query),
            query: query,
            settingsRevision: 7,
          ).instrumentKey,
          'US:stock:AAPL',
        );
      }
    });

    test('缺失币种或账户身份时拒绝发起查询', () {
      expect(
        () => _query({..._stock()}..remove('currency')),
        throwsFormatException,
      );
      expect(
        () => _query({..._stock(), 'accountId': 'not-a-uuid'}),
        throwsFormatException,
      );
    });
  });

  group('MarketHistoryRead', () {
    test('保留服务端十进制精度并接受不同尾零写法的等值 OHLC', () {
      final query = _query(_stock());
      final read = MarketHistoryRead.fromJson(
        _response(
          query,
          bars: [
            {
              'timestamp': '2026-09-04T19:30:00.000Z',
              'open': '1',
              'high': '1.0',
              'low': '1.00',
              'close': '1.000',
              'volume': 1,
            },
          ],
        ),
        query: query,
        settingsRevision: 7,
      );

      expect(read.bars.single.close, '1.000');
      expect(read.bars.single.volume, 1);
    });

    test('拒绝方向相同但看涨看跌合约不匹配的行情', () {
      final query = _query(_option());

      expect(
        () => MarketHistoryRead.fromJson(
          _response(
            query,
            instrumentKey: 'US:option:SYNTHETIC:2027-03-19:P:12.34',
          ),
          query: query,
          settingsRevision: 7,
        ),
        throwsFormatException,
      );
    });

    test('正股序列接受期权来源身份并强制匹配target及资产标识', () {
      final query = _query(
        _option(optionType: 'P'),
        target: MarketHistoryTarget.underlying,
      );
      final read = MarketHistoryRead.fromJson(
        _response(query, expired: false),
        query: query,
        settingsRevision: 7,
      );

      expect(read.target, MarketHistoryTarget.underlying);
      expect(read.positionAssetType, 'option');
      expect(read.assetType, 'stock');
      expect(read.instrumentKey, 'US:stock:SYNTHETIC');
      expect(read.adjustment, 'split');
      expect(read.expired, isFalse);

      for (final response in [
        _response(query, target: MarketHistoryTarget.position),
        _response(query, positionAssetType: 'stock'),
        _response(query, assetType: 'option'),
        _response(query, instrumentKey: 'US:stock:OTHER'),
      ]) {
        expect(
          () => MarketHistoryRead.fromJson(
            response,
            query: query,
            settingsRevision: 7,
          ),
          throwsFormatException,
        );
      }
    });

    test('拒绝币种、设置版本和非升序时间不匹配的结果', () {
      final query = _query(_stock());
      for (final response in [
        _response(query, currency: 'EUR'),
        _response(query, settingsRevision: 8),
        _response(
          query,
          bars: [
            {
              'timestamp': '2026-09-04T19:45:00.000Z',
              'open': '12',
              'high': '13',
              'low': '11',
              'close': '12',
              'volume': 1,
            },
            {
              'timestamp': '2026-09-04T19:30:00.000Z',
              'open': '12',
              'high': '13',
              'low': '11',
              'close': '12',
              'volume': 1,
            },
          ],
        ),
      ]) {
        expect(
          () => MarketHistoryRead.fromJson(
            response,
            query: query,
            settingsRevision: 7,
          ),
          throwsFormatException,
        );
      }
    });

    test('拒绝零成交量、零价和错误 OHLC 关系', () {
      final query = _query(_stock());
      for (final bar in [
        {
          'timestamp': '2026-09-04T19:30:00.000Z',
          'open': '12',
          'high': '13',
          'low': '11',
          'close': '12',
          'volume': 0,
        },
        {
          'timestamp': '2026-09-04T19:30:00.000Z',
          'open': '0',
          'high': '13',
          'low': '0',
          'close': '12',
          'volume': 1,
        },
        {
          'timestamp': '2026-09-04T19:30:00.000Z',
          'open': '14',
          'high': '13',
          'low': '11',
          'close': '12',
          'volume': 1,
        },
      ]) {
        expect(
          () => MarketHistoryRead.fromJson(
            _response(query, bars: [bar]),
            query: query,
            settingsRevision: 7,
          ),
          throwsFormatException,
        );
      }
    });

    test('不可用状态接受稳定 issue 且要求 bars 为空', () {
      final query = _query(_stock());
      final read = MarketHistoryRead.fromJson(
        _response(query, status: 'unavailable', issue: 'history_not_supported'),
        query: query,
        settingsRevision: 7,
      );

      expect(read.issue, 'history_not_supported');
      expect(read.bars, isEmpty);

      final providerFailure = _response(
        query,
        status: 'unavailable',
        issue: 'rate_limited',
      );
      (providerFailure['data']! as Map<String, Object?>)
        ..['providerId'] = 'test-provider'
        ..['feed'] = null;
      expect(
        MarketHistoryRead.fromJson(
          providerFailure,
          query: query,
          settingsRevision: 7,
        ).providerId,
        'test-provider',
      );
      expect(
        () => MarketHistoryRead.fromJson(
          _response(
            query,
            status: 'unavailable',
            issue: 'no_data',
            bars: [
              {
                'timestamp': '2026-09-04T19:30:00.000Z',
                'open': '12',
                'high': '13',
                'low': '11',
                'close': '12',
                'volume': 1,
              },
            ],
          ),
          query: query,
          settingsRevision: 7,
        ),
        throwsFormatException,
      );
    });
  });
}
