import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

import '../session_flow_test.dart' show FakeLyraAdapter;

/// 仅用于测试的合成记录；不读取真实券商文件，不进入生产入口。
class LedgerFixture {
  final requests = <RequestOptions>[];
  final failures = <String>{};
  static const account = 'a2000000-0000-4000-8000-000000000001';
  static const timestamp = '2026-09-08T08:00:00.000Z';

  Future<SessionDependencies> dependencies({bool paired = true}) async {
    final store = MemoryCredentialStore();
    if (paired) {
      await store.write(
        const CredentialSnapshot(
          serverUrl: 'https://fixture.invalid',
          deviceId: 'test-device',
          accessToken: 'test-only-token',
          refreshToken: 'test-only-refresh',
          authMethod: 'google',
        ),
      );
    }
    return SessionDependencies(
      credentialStore: store,
      clientFactory: (url) =>
          Dio(BaseOptions(baseUrl: url))
            ..httpClientAdapter = FakeLyraAdapter((options) async {
              requests.add(options);
              final market = options.uri.path == '/v1/market/positions';
              final command = market
                  ? 'market-positions'
                  : options.uri.path.split('/').last;
              return ResponseBody.fromString(
                jsonEncode(
                  failures.contains(command)
                      ? {
                          'code': 'QUERY_FAILED',
                          'message': '测试连接中断',
                          'retryable': true,
                        }
                      : market
                      ? marketResponse(options.queryParameters)
                      : response(command, options.queryParameters),
                ),
                failures.contains(command) ? 503 : 200,
                headers: {
                  Headers.contentTypeHeader: [Headers.jsonContentType],
                },
              );
            }),
    );
  }

  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    final data = payload(command, query);
    return {
      'metadata': {
        'ledgerVersion': 12,
        'asOf': timestamp,
        'fetchedAt': timestamp,
        'aggregateCurrency': 'USD',
        'currencyGroups': [
          {
            'currency': 'USD',
            'accountIds': [account],
          },
        ],
      },
      'data': data,
    };
  }

  /// Market data is optional in existing ledger tests. A separate response
  /// keeps the standard ledger payload and all historical costs untouched.
  Map<String, Object?> marketResponse(Map<String, dynamic> query) {
    final ledger = response('positions', query);
    final positions = ((ledger['data'] as Map<String, Object?>)['rows'] as List)
        .cast<Map<String, Object?>>();
    return {
      'metadata': ledger['metadata'],
      'data': {
        'rows': [
          for (final position in positions)
            {
              ...position,
              'currency': 'USD',
              'instrumentKey': null,
              'quote': {
                'status': 'unavailable',
                'quote': null,
                'referencePrice': null,
                'issue': 'not_configured',
                'cached': false,
              },
              'valuation': {
                'marketValue': null,
                'unrealizedPnl': null,
                'multiplier': null,
                'basis': 'reference_price_excluding_fees',
              },
            },
        ],
        'count': positions.length,
        'pricedCount': 0,
        'complete': positions.isEmpty,
        'basis': 'current_positions_at_quote_times_excluding_fees',
        'marketData': {
          'enabled': false,
          'routes': {'stock': <String>[], 'option': <String>[]},
          'providers': <Object?>[],
        },
        'currencyGroups': [
          if (positions.isNotEmpty)
            {
              'currency': 'USD',
              'accountIds': [account],
              'positions': positions.length,
              'pricedPositions': 0,
              'complete': false,
              'marketValue': null,
              'unrealizedPnl': null,
              'asOfFrom': null,
              'asOfTo': null,
            },
        ],
      },
    };
  }

  /// 返回与服务端公开类型一致的 JSON；任何未列出的请求都使测试失败。
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    Map<String, Object?> grouped(Map<String, Object?> data) => {
      'aggregateCurrency': 'USD',
      'groups': [
        {
          'currency': 'USD',
          'accountIds': [account],
          'data': data,
        },
      ],
    };
    final positions = [
      for (final item in [
        ('AAPL', '24', '4286.40'),
        ('NVDA', '36', '3924.72'),
        ('MSFT', '12', '4986.00'),
        ('VOO', '8', '4109.52'),
      ])
        {
          'accountId': account,
          'symbolKey': item.$1,
          'underlying': item.$1,
          'assetType': 'stock',
          'direction': 'long',
          'quantity': item.$2,
          'cost': item.$3,
          'firstOpenDate': '2026-05-12',
          'expiry': null,
          'strike': null,
          'optionType': null,
        },
    ];
    final trade = {
      'accountId': account,
      'symbol': 'AAPL',
      'underlying': 'AAPL',
      'action': 'Buy',
      'assetType': 'stock',
      'quantity': '12',
      'price': '178.60',
      'fees': '0.12',
      'amount': '-2143.32',
      'transactionDate': '2026-09-04',
      'recordDate': '2026-09-04',
      'sourceType': 'csv',
    };
    switch (command) {
      case 'summary':
        return grouped({
          'netDeposits': '24850.00',
          'realizedPnlTotal': '2846.72',
          'realizedPnlStock': '2194.80',
          'realizedPnlOption': '651.92',
          'totalFees': '18.46',
          'dividends': '126.38',
          'interest': '23.18',
          'taxes': '0.00',
          'cashBalance': '6584.16',
          'openPositions': 4,
          'transactionCount': 86,
        });
      case 'accounts':
        return {
          'rows': [
            {
              'id': account,
              'displayName': '长期投资',
              'maskedSuffix': '••276',
              'baseCurrency': 'USD',
              'status': 'active',
            },
          ],
          'count': 1,
        };
      case 'positions':
        return {
          'rows': positions
              .where(
                (row) =>
                    query['underlying'] == null ||
                    row['underlying'] == query['underlying'],
              )
              .toList(),
          'count': 4,
        };
      case 'expiring':
        return {
          'rows': [
            {
              ...positions.first,
              'assetType': 'option',
              'expiry': '2026-09-18',
              'strike': '200',
              'optionType': 'C',
              'quantity': '2',
              'cost': '6.20',
              'premiumDollars': '620.00',
              'daysRemaining': 10,
              'status': 'upcoming',
            },
          ],
          'count': 1,
        };
      case 'trades':
        return {
          'rows': query['cursor'] == null
              ? [
                  trade,
                  {
                    ...trade,
                    'symbol': 'NVDA',
                    'underlying': 'NVDA',
                    'amount': '1084.38',
                    'action': 'Sell',
                    'transactionDate': '2026-09-03',
                  },
                ]
              : [
                  {...trade, 'symbol': 'MSFT', 'underlying': 'MSFT'},
                ],
          'count': 2,
          'nextCursor': query['cursor'] == null ? 'next-page' : null,
        };
      case 'cashflow':
        return {
          'rows': [
            {
              ...trade,
              'symbol': null,
              'underlying': null,
              'assetType': 'cash',
              'action': 'Cash Dividend',
              'quantity': null,
              'amount': '126.38',
            },
          ],
          'count': 1,
          'totalCount': 1,
          'total': '126.38',
          'nextCursor': null,
        };
      case 'realized':
        return {
          'rows': [
            {
              'accountId': account,
              'underlying': 'AAPL',
              'symbolKey': 'AAPL',
              'assetType': 'stock',
              'direction': 'long',
              'openDate': '2026-05-12',
              'closeDate': '2026-09-04',
              'quantity': '8',
              'openPrice': '168.20',
              'closePrice': '192.80',
              'openFees': '0.10',
              'closeFees': '0.12',
              'pnl': '196.58',
              'closeAction': 'Sell',
            },
          ],
          'count': 1,
          'totalCount': 1,
          'totalPnl': '196.58',
          'nextCursor': null,
        };
      case 'monthly':
        return grouped({
          'rows': [
            for (final (month, pnl) in [
              ('04', '382.48'),
              ('05', '-148.62'),
              ('06', '724.30'),
              ('07', '486.12'),
              ('08', '913.28'),
              ('09', '489.16'),
            ])
              {
                'month': '2026-$month',
                'netPnl': pnl,
                'closedLots': 5,
                'closeTransactions': 4,
                'wins': 3,
                'losses': 2,
                'winRatePct': '60.00',
                'grossProfit': '700.50',
                'grossLoss': '-120.30',
                'profitFactor': '5.82',
                'fees': '3.20',
              },
          ],
          'totalPnl': '2846.72',
        });
      case 'allocation':
        return grouped({
          'rows': [
            for (final item in [
              ('AAPL', '24.77', '4286.40'),
              ('NVDA', '22.68', '3924.72'),
              ('MSFT', '28.81', '4986.00'),
              ('VOO', '23.74', '4109.52'),
            ])
              {
                'category': item.$1,
                'allocationPct': item.$2,
                'costBasis': item.$3,
                'positions': 1,
              },
          ],
          'totalCostBasis': '17306.64',
        });
      case 'attribution':
        return grouped({
          'rows': [
            {
              'category': 'AAPL',
              'netPnl': '2194.80',
              'absoluteContributionPct': '77.10',
              'winRatePct': '62.50',
            },
          ],
          'totalPnl': '2194.80',
        });
      case 'drawdown':
        return grouped({
          'rows': [
            for (final (i, amount) in [
              '0.00',
              '248.16',
              '196.84',
              '412.60',
              '389.50',
              '724.30',
            ].indexed)
              {
                'date': '2026-09-0${i + 1}',
                'dailyPnl': '0.00',
                'cumulativePnl': amount,
                'runningPeak': '724.30',
                'drawdown': '0.00',
              },
          ],
          'maxDrawdown': '51.32',
        });
      case 'risk':
        return grouped({
          'rows': [
            {
              'bucket': '0-30d',
              'longPremiumPaid': '620.00',
              'shortPremiumReceived': '0.00',
              'contracts': '2',
              'longContracts': '2',
              'shortContracts': '0',
            },
          ],
          'totalLongPremiumPaid': '620.00',
          'totalShortPremiumReceived': '0.00',
        });
      case 'holding-period':
        return grouped({
          'rows': [
            {
              'bucket': '8-30d',
              'netPnl': '412.38',
              'averageHoldingDays': '18.42',
              'averageLotPnl': '51.55',
              'winRatePct': '62.50',
            },
          ],
        });
      case 'extremes':
        return grouped({
          'rows': [
            {
              'side': 'best',
              'rank': 1,
              'trade': {
                'accountId': account,
                'underlying': 'AAPL',
                'closeDate': '2026-09-04',
                'pnl': '412.38',
                'quantity': '8',
              },
            },
          ],
        });
      case 'streaks':
        return grouped({
          'rows': [
            {
              'kind': 'win',
              'count': 4,
              'startDate': '2026-08-12',
              'endDate': '2026-08-28',
              'pnl': '821.36',
            },
          ],
        });
      case 'stress':
        return grouped({
          'rows': [
            {
              'underlying': 'AAPL',
              'contracts': '2',
              'premiumAtRisk': '620.00',
              'lossIfZero': '620.00',
              'portfolioPremiumPct': '100.00',
              'nearestExpiry': '2026-09-18',
            },
          ],
          'allLongOptionsLossIfZero': '620.00',
        });
      case 'audit':
        return {
          'rows': [
            {
              'check': 'transactions_present',
              'count': 86,
              'status': 'pass',
              'severity': 'error',
              'expectation': '至少一条原始交易',
            },
          ],
          'ok': true,
          'failed': 0,
        };
      default:
        throw StateError('测试收到未定义接口 $command');
    }
  }
}
