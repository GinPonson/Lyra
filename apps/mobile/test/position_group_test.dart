import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/ledger/position_group.dart';

void main() {
  test('同标的股票和不同期权归组，原数量、成本及多空记录保持不变', () {
    final rows = <Map<String, Object?>>[
      {
        'underlying': 'GOOG',
        'assetType': 'stock',
        'quantity': '1.002',
        'cost': '123.4567',
      },
      {
        'underlying': 'META',
        'assetType': 'stock',
        'quantity': '2',
        'cost': '50',
      },
      {
        'underlying': 'GOOG',
        'assetType': 'option',
        'strike': '385',
        'direction': 'long',
        'cost': '18.6500',
      },
      {
        'underlying': 'GOOG',
        'assetType': 'option',
        'strike': '430',
        'direction': 'short',
        'cost': '20.4000',
      },
    ];
    final groups = PositionGroup.fromRows(rows, currencyFor: (_) => 'USD');
    expect(groups.map((group) => group.symbol), ['GOOG', 'META']);
    expect(groups.first.rows, [rows[0], rows[2], rows[3]]);
    expect(identical(groups.first.rows.first, rows.first), isTrue);
    expect(groups.first.rows.last['direction'], 'short');
    expect(groups.first.rows[1]['cost'], '18.6500');
  });

  test('不同币种或币种不明不混组，不同股份代码不归并', () {
    final rows = <Map<String, Object?>>[
      {'underlying': 'GOOG', 'currency': 'USD'},
      {'underlying': 'GOOG', 'currency': 'EUR'},
      {'underlying': 'GOOG'},
      {'underlying': 'GOOG'},
      {'underlying': 'GOOGL', 'currency': 'USD'},
    ];
    expect(
      PositionGroup.fromRows(
        rows,
        currencyFor: (row) => row['currency'] as String?,
      ),
      hasLength(5),
    );
    expect(PositionGroup.fromRows([], currencyFor: (_) => 'USD'), isEmpty);
    expect(
      () => PositionGroup.fromRows([{}], currencyFor: (_) => 'USD'),
      throwsFormatException,
    );
  });
}
