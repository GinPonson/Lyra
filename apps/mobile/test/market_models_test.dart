import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/market/market_models.dart';

import 'support/market_fixture.dart';

Map<String, Object?> _data(Map<String, Object?> body) =>
    body['data'] as Map<String, Object?>;
List<Map<String, Object?>> _rows(Map<String, Object?> body) =>
    (_data(body)['rows'] as List).cast<Map<String, Object?>>();

void main() {
  test('保留服务端十进制金额与各自报价、获取时点，不在手机重新估值', () {
    final body = MarketFixture().marketResponse({});
    final row = _rows(body).first;
    final lookup = row['quote'] as Map<String, Object?>;
    (lookup['referencePrice'] as Map<String, Object?>)['value'] =
        '9007199254740993.995';
    (row['valuation'] as Map<String, Object?>)['marketValue'] =
        '216172782113783855.880';
    final read = MarketPositionsRead.fromJson(body);
    expect(read.rows.first.referencePrice!.value, '9007199254740993.995');
    expect(read.rows.first.marketValue, '216172782113783855.880');
    expect(read.rows.first.referencePrice!.asOf, MarketFixture.quoteTime);
    expect(read.rows.first.quote!.fetchedAt, MarketFixture.fetchTime);
    expect(read.rows[1].referenceBasis, 'mid');
    expect(read.rows[1].multiplier, '100');
    expect(read.rows[1].position['cost'], '18.6500');

    lookup['referencePrice'] = <String, Object?>{
      ...(lookup['referencePrice'] as Map<String, Object?>),
      'value': 202.125,
    };
    expect(() => MarketPositionsRead.fromJson(body), throwsFormatException);
  });

  test('报价按账户、方向和完整合约关联，不借用同标的另一行的报价', () {
    final read = MarketPositionsRead.fromJson(
      MarketFixture().marketResponse({}),
    );
    final call = read.rows[1].position;
    expect(read.match(call, currency: 'USD'), same(read.rows[1]));
    expect(
      read.match({...call, 'strike': '0385.2500'}, currency: 'USD'),
      same(read.rows[1]),
    );
    for (final entry in <String, Object?>{
      'accountId': 'another-account',
      'symbolKey': 'AAPL',
      'direction': 'short',
      'underlying': 'MSFT',
      'expiry': '2027-04-16',
      'strike': '385.5',
      'optionType': 'P',
      'assetType': 'stock',
    }.entries) {
      expect(
        read.match({...call, entry.key: entry.value}, currency: 'USD'),
        isNull,
        reason: entry.key,
      );
    }
    expect(read.match(call, currency: 'HKD'), isNull);
    expect(read.match(call, currency: null), isNull);
    expect(read.match({...call, 'strike': null}, currency: 'USD'), isNull);
    expect(
      read.match({...call, 'strike': '385.25.0'}, currency: 'USD'),
      isNull,
    );
  });

  test('相同完整身份重复出现时不显示任一行的估值', () {
    final body = MarketFixture().marketResponse({});
    final rows = _rows(body);
    _data(body)['rows'] = [...rows, rows.first];
    _data(body)['count'] = 4;
    _data(body)['pricedCount'] = 4;
    final read = MarketPositionsRead.fromJson(body);
    expect(read.match(rows.first, currency: 'USD'), isNull);
  });

  test('缺报价保留 null，过期报价只保留行估值，issue 为空仍然过期', () {
    final missing = MarketPositionsRead.fromJson(
      MarketFixture(unavailable: {MarketFixture.putSymbol}).marketResponse({}),
    );
    expect(missing.pricedCount, 2);
    expect(missing.complete, isFalse);
    expect(missing.rows.last.referencePrice, isNull);
    expect(missing.rows.last.marketValue, isNull);
    expect(missing.currencyGroups.single.marketValue, isNull);

    final stale = MarketPositionsRead.fromJson(
      MarketFixture(stale: {MarketFixture.callSymbol}).marketResponse({}),
    );
    expect(stale.pricedCount, 3);
    expect(stale.complete, isFalse);
    expect(stale.rows[1].status, 'stale');
    expect(stale.rows[1].issue, isNull);
    expect(stale.rows[1].marketValue, '2325.00');
    expect(stale.currencyGroups.single.complete, isFalse);
  });

  test('报价标识、币种和时区不符合标准响应时进入行情错误态', () {
    for (final mutation in <void Function(Map<String, Object?>)>[
      (row) => row['currency'] = 'HKD',
      (row) => row['instrumentKey'] = 'US:stock:MSFT',
      (row) {
        final lookup = row['quote'] as Map<String, Object?>;
        (lookup['quote'] as Map<String, Object?>)['fetchedAt'] =
            '2026-09-08T07:59:55';
      },
    ]) {
      final body = MarketFixture().marketResponse({});
      mutation(_rows(body).first);
      expect(() => MarketPositionsRead.fromJson(body), throwsFormatException);
    }
  });
}
