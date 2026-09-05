import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/ledger/ledger_models.dart';

/// 与 TypeScript 服务端 GET /v1/ledger/summary 响应同构的 fixture。
const Map<String, Object?> summaryJson = <String, Object?>{
  'metadata': <String, Object?>{
    'accountScope': <String, Object?>{'kind': 'all'},
    'accountIds': <String>['a2000000-0000-4000-8000-000000000001'],
    'currencyGroups': <Object?>[
      <String, Object?>{
        'currency': 'USD',
        'accountIds': <String>['a2000000-0000-4000-8000-000000000001'],
      },
    ],
    'aggregateCurrency': 'USD',
    'ledgerVersion': 3,
    'asOf': '2026-09-02T10:00:00.000Z',
    'fetchedAt': '2026-09-02T10:00:00.000Z',
  },
  'data': <String, Object?>{
    'aggregateCurrency': 'USD',
    'groups': <Object?>[
      <String, Object?>{
        'currency': 'USD',
        'accountIds': <String>['a2000000-0000-4000-8000-000000000001'],
        'data': <String, Object?>{
          'netDeposits': '1000.00',
          'realizedPnlTotal': '12.34',
          'realizedPnlStock': '10.00',
          'realizedPnlOption': '2.34',
          'totalFees': '1.00',
          'dividends': '0.65',
          'interest': '0.10',
          'taxes': '0',
          'cashBalance': '500.005',
          'openPositions': 1,
          'transactionCount': 3,
        },
      },
    ],
  },
};

void main() {
  test('summary 解析保留十进制字符串与权威语境', () {
    final read = LedgerSummaryRead.fromJson(summaryJson);
    expect(read.metadata.ledgerVersion, 3);
    expect(read.metadata.aggregateCurrency, 'USD');
    expect(read.entries, hasLength(1));

    final entry = read.entries.single;
    // 金额必须保持服务端字符串原样；禁止在此转换为 double。
    expect(entry.netDeposits, '1000.00');
    expect(entry.realizedPnlTotal, '12.34');
    expect(entry.cashBalance, '500.005');
    expect(entry.openPositions, 1);
    expect(entry.transactionCount, 3);
  });

  test('多币种汇总不生成伪合计', () {
    final Map<String, Object?> multiCurrency = <String, Object?>{
      ...summaryJson,
      'metadata': <String, Object?>{
        ...(summaryJson['metadata'] as Map<String, Object?>),
        'aggregateCurrency': null,
        'currencyGroups': <Object?>[
          <String, Object?>{'currency': 'USD', 'accountIds': <String>['x']},
          <String, Object?>{'currency': 'EUR', 'accountIds': <String>['y']},
        ],
      },
      'data': <String, Object?>{
        'aggregateCurrency': null,
        'groups': <Object?>[],
      },
    };
    final read = LedgerSummaryRead.fromJson(multiCurrency);
    expect(read.metadata.aggregateCurrency, isNull);
    expect(read.entries, isEmpty);
  });
}
