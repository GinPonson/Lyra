import 'market_fixture.dart';
import 'market_history_fixture.dart';

/// Supplies deterministic underlying-summary payloads for isolated tests.
class MarketUnderlyingQuoteFixture extends MarketHistoryFixture {
  String? issue;
  String? priceValue = '205.1250';
  String? previousCloseValue = '200.0000';
  String changePercentValue = '2.5625';
  String changeIssueValue = 'previous_close_unavailable';
  String currencyValue = 'USD';
  String instrumentTicker = 'AAPL';
  String fetchedAtValue = '2026-09-04T20:00:00.000Z';
  String priceAsOfValue = '2026-09-04T19:45:00.000Z';
  String previousSessionDate = '2026-09-03';
  int delayMinutesValue = 15;
  int? ledgerVersionOverride;
  int? settingsRevisionOverride;
  int? positionLedgerVersionOverride;
  String? accountOverride;
  String? symbolOverride;
  String? directionOverride;
  String? positionAssetTypeOverride;
  String? instrumentKeyOverride;
  bool nullInstrumentKey = false;
  String? responseCurrencyOverride;

  /// Routes the added API resource while preserving shared settings and ledger fixtures.
  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    if (command == 'underlying-quote') return underlyingQuoteResponse(query);
    final body = super.response(command, query);
    if (command == 'positions' && positionLedgerVersionOverride != null) {
      (body['metadata']! as Map<String, Object?>)['ledgerVersion'] =
          positionLedgerVersionOverride;
    }
    return body;
  }

  /// Builds a summary bound to the requested anchor unless a test alters one field.
  @override
  Map<String, Object?> underlyingQuoteResponse(Map<String, dynamic> query) {
    final symbolKey = query['symbol_key'] as String;
    final isOption =
        symbolKey == MarketFixture.callSymbol ||
        symbolKey == MarketFixture.putSymbol;
    final available = issue == null;
    final previousClose = previousCloseValue;
    return {
      'metadata': {
        'accountScope': {
          'kind': 'account',
          'account': accountOverride ?? query['scope'],
        },
        'ledgerVersion':
            ledgerVersionOverride ??
            int.parse(query['ledger_version'] as String),
        'settingsRevision':
            settingsRevisionOverride ??
            int.parse(query['settings_revision'] as String),
        'fetchedAt': '2026-09-04T20:00:00.000Z',
      },
      'data': {
        'accountId': accountOverride ?? query['scope'],
        'symbolKey': symbolOverride ?? symbolKey,
        'direction': directionOverride ?? query['direction'],
        'positionAssetType':
            positionAssetTypeOverride ?? (isOption ? 'option' : 'stock'),
        'assetType': 'stock',
        'instrumentKey': nullInstrumentKey
            ? null
            : instrumentKeyOverride ?? 'US:stock:$instrumentTicker',
        'currency': responseCurrencyOverride ?? currencyValue,
        'status': available ? 'available' : 'unavailable',
        'issue': issue,
        'cached': false,
        'providerId': available ? 'alpaca' : null,
        'feed': available ? 'sip' : null,
        'timing': {'kind': 'delayed', 'delayMinutes': delayMinutesValue},
        'fetchedAt': available ? fetchedAtValue : null,
        'price': available && priceValue != null
            ? {'value': priceValue, 'asOf': priceAsOfValue}
            : null,
        'previousClose': available && previousClose != null
            ? {'value': previousClose, 'sessionDate': previousSessionDate}
            : null,
        'changePercent': available && previousClose != null
            ? changePercentValue
            : null,
        'changeIssue': available && previousClose == null
            ? changeIssueValue
            : null,
        'comparisonBasis': 'previous_session_close',
        'adjustment': 'split',
      },
    };
  }
}
