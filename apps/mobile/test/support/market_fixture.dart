import 'ledger_fixture.dart';

/// Synthetic server-computed values, with one stock and two distinct contracts.
/// No vendor credentials, live prices, or client-side valuation calculations.
class MarketFixture extends LedgerFixture {
  MarketFixture({
    this.enabled = true,
    this.unavailable = const {},
    this.stale = const {},
  });

  final bool enabled;
  final Set<String> unavailable;
  final Set<String> stale;
  static const callSymbol = 'AAPL270319C00385250';
  static const putSymbol = 'AAPL270319P00385250';
  static const quoteTime = '2026-09-04T20:00:00.000Z';
  static const optionQuoteTime = '2026-09-03T20:00:00.000Z';
  static const fetchTime = '2026-09-08T07:59:55.000Z';

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command != 'positions') return data;
    final stock =
        (super.payload('positions', {})['rows'] as List).first
            as Map<String, Object?>;
    final rows =
        <Map<String, Object?>>[
              stock,
              {
                ...stock,
                'symbolKey': callSymbol,
                'assetType': 'option',
                'strike': '385.25',
                'expiry': '2027-03-19',
                'optionType': 'C',
                'quantity': '1',
                'cost': '18.6500',
              },
              {
                ...stock,
                'symbolKey': putSymbol,
                'assetType': 'option',
                'strike': '385.25',
                'expiry': '2027-03-19',
                'optionType': 'P',
                'direction': 'short',
                'quantity': '2',
                'cost': '40.80',
              },
            ]
            .where(
              (row) =>
                  query['underlying'] == null ||
                  row['underlying'] == query['underlying'],
            )
            .toList();
    return {'rows': rows, 'count': rows.length};
  }

  @override
  Map<String, Object?> marketResponse(Map<String, dynamic> query) {
    if (!enabled) return super.marketResponse(query);
    final ledger = response('positions', query);
    final positions = ((ledger['data'] as Map<String, Object?>)['rows'] as List)
        .cast<Map<String, Object?>>();
    final rows = positions.map((position) {
      final symbol = position['symbolKey'] as String;
      final option = position['assetType'] == 'option';
      final call = position['optionType'] == 'C';
      final key = option
          ? 'US:option:AAPL:2027-03-19:${position['optionType']}:385.25'
          : 'US:stock:AAPL';
      final time = option ? optionQuoteTime : quoteTime;
      final price = !option
          ? '202.125'
          : call
          ? '23.25'
          : '24.25';
      final missing = unavailable.contains(symbol);
      final old = stale.contains(symbol);
      return <String, Object?>{
        ...position,
        'currency': 'USD',
        'instrumentKey': key,
        'quote': {
          'status': missing
              ? 'unavailable'
              : old
              ? 'stale'
              : 'available',
          'quote': missing
              ? null
              : {
                  'instrumentKey': key,
                  'providerId': 'fixture-feed',
                  'feed': 'synthetic historical quotes',
                  'currency': 'USD',
                  'bid': option
                      ? {'value': call ? '23.125' : '24.125', 'asOf': time}
                      : null,
                  'ask': option
                      ? {'value': call ? '23.375' : '24.375', 'asOf': time}
                      : null,
                  'last': null,
                  'close': option ? null : {'value': price, 'asOf': time},
                  'timing': {'kind': 'end_of_day', 'delayMinutes': null},
                  'fetchedAt': fetchTime,
                },
          'referencePrice': missing
              ? null
              : {
                  'value': price,
                  'asOf': time,
                  'basis': option ? 'mid' : 'close',
                },
          'issue': missing ? 'no_data' : null,
          'cached': old,
        },
        'valuation': {
          'marketValue': missing
              ? null
              : !option
              ? '4851.00'
              : call
              ? '2325.00'
              : '-4850.00',
          'unrealizedPnl': missing
              ? null
              : !option
              ? '564.60'
              : call
              ? '460.00'
              : '-770.00',
          'multiplier': option ? '100' : '1',
          'basis': 'reference_price_excluding_fees',
        },
      };
    }).toList();
    final priced = rows
        .where(
          (row) =>
              (row['quote'] as Map<String, Object?>)['status'] != 'unavailable',
        )
        .length;
    final complete = rows.every(
      (row) => (row['quote'] as Map<String, Object?>)['status'] == 'available',
    );
    return {
      'metadata': ledger['metadata'],
      'data': {
        'rows': rows,
        'count': rows.length,
        'pricedCount': priced,
        'complete': complete,
        'basis': 'current_positions_at_quote_times_excluding_fees',
        'marketData': {
          'enabled': true,
          'routes': {
            'stock': ['fixture-feed'],
            'option': ['fixture-feed'],
          },
          'providers': [
            {
              'id': 'fixture-feed',
              'assetTypes': ['stock', 'option'],
              'cacheTtlSeconds': 900,
            },
          ],
        },
        'currencyGroups': [
          if (rows.isNotEmpty)
            {
              'currency': 'USD',
              'accountIds': [LedgerFixture.account],
              'positions': rows.length,
              'pricedPositions': priced,
              'complete': complete,
              'marketValue': complete ? '2326.00' : null,
              'unrealizedPnl': complete ? '254.60' : null,
              'asOfFrom': priced == 0 ? null : optionQuoteTime,
              'asOfTo': priced == 0 ? null : quoteTime,
            },
        ],
      },
    };
  }
}
