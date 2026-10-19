import 'market_fixture.dart';

/// Supplies deterministic history API responses for isolated mobile tests.
class MarketHistoryFixture extends MarketFixture {
  MarketHistoryFixture({
    this.settingsRevision = 7,
    super.enabled,
    super.unavailable,
    super.stale,
  });

  int settingsRevision;
  final Map<String, String> unavailableIssues = {};
  final Map<String, List<Map<String, Object?>>> barsBySymbol = {};
  final Map<String, String> stockUnderlyingsBySymbol = {};
  List<String> stockRoutes = ['alpaca'];
  List<String> optionRoutes = ['alpaca'];
  bool alpacaConfigured = true;

  /// Provides a configured delayed source so requests carry a real revision.
  @override
  Map<String, Object?> marketSettingsResponse() => {
    'revision': settingsRevision,
    'routes': {'stock': stockRoutes, 'option': optionRoutes},
    'effectiveRoutes': {'stock': stockRoutes, 'option': optionRoutes},
    'providers': [
      {
        'id': 'alpaca',
        'displayName': 'Alpaca',
        'assetTypes': ['stock', 'option'],
        'configured': alpacaConfigured,
        'timing': {'kind': 'delayed', 'delayMinutes': 16},
        'cacheTtlSeconds': 60,
      },
    ],
    'updatedAt': '2026-09-04T20:00:00.000Z',
  };

  /// Routes history reads to the exact symbol response while retaining the
  /// inherited ledger, settings and session fixture behavior.
  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    if (command == 'history') return historyResponse(query);
    return super.response(command, query);
  }

  /// Builds a response bound to the requested account, symbol, side and range.
  Map<String, Object?> historyResponse(Map<String, dynamic> query) {
    final accountId = query['scope'] as String;
    final symbolKey = query['symbol_key'] as String;
    final direction = query['direction'] as String;
    // Older direct fixture callers model the API's default position target.
    final target = query['target'] as String? ?? 'position';
    final range = query['range'] as String;
    final ledgerVersion = int.parse(query['ledger_version'] as String);
    final requestedRevision = int.parse(query['settings_revision'] as String);
    final isCall = symbolKey == MarketFixture.callSymbol;
    final isPut = symbolKey == MarketFixture.putSymbol;
    final positionIsOption = isCall || isPut;
    final seriesIsOption = positionIsOption && target == 'position';
    final issue = unavailableIssues[symbolKey];
    final bars = issue == null
        ? barsBySymbol[symbolKey] ??
              _bars(
                range,
                isCall: seriesIsOption && isCall,
                isPut: seriesIsOption && isPut,
              )
        : const <Map<String, Object?>>[];
    final instrumentKey = seriesIsOption
        ? 'US:option:AAPL:2027-03-19:${isCall ? 'C' : 'P'}:385.25'
        : 'US:stock:${positionIsOption ? 'AAPL' : stockUnderlyingsBySymbol[symbolKey] ?? symbolKey}';
    final timeframe = switch (range) {
      '1d' => '15Min',
      '1w' => '1Hour',
      '1m' || '3m' => '1Day',
      _ => 'invalid',
    };
    const cutoff = '2026-09-04T20:00:00.000Z';
    return {
      'metadata': {
        'accountScope': {'kind': 'account', 'account': accountId},
        'ledgerVersion': ledgerVersion,
        'settingsRevision': requestedRevision,
        'fetchedAt': cutoff,
      },
      'data': {
        'accountId': accountId,
        'symbolKey': symbolKey,
        'direction': direction,
        'target': target,
        'positionAssetType': positionIsOption ? 'option' : 'stock',
        'assetType': seriesIsOption ? 'option' : 'stock',
        'currency': 'USD',
        'instrumentKey': instrumentKey,
        'range': range,
        'timeframe': timeframe,
        'status': issue == null ? 'available' : 'unavailable',
        'issue': issue,
        'cached': false,
        'providerId': 'alpaca',
        'feed': seriesIsOption ? 'alpaca_option_bars' : 'sip',
        'timing': {'kind': 'delayed', 'delayMinutes': 16},
        'adjustment': seriesIsOption ? 'raw' : 'split',
        'expired': false,
        'cutoff': cutoff,
        'fetchedAt': cutoff,
        'bars': bars,
      },
    };
  }

  /// Builds deterministic positive-volume bars for stock and option rows.
  List<Map<String, Object?>> _bars(
    String range, {
    required bool isCall,
    required bool isPut,
  }) {
    final firstTimestamp = switch (range) {
      '1d' => '2026-09-04T19:30:00.000Z',
      '1w' => '2026-09-01T14:30:00.000Z',
      '1m' || '3m' => '2026-08-20T14:30:00.000Z',
      _ => '2026-09-01T14:30:00.000Z',
    };
    final secondTimestamp = switch (range) {
      '1d' => '2026-09-04T19:45:00.000Z',
      '1w' => '2026-09-04T19:30:00.000Z',
      '1m' || '3m' => '2026-09-04T19:30:00.000Z',
      _ => '2026-09-04T19:30:00.000Z',
    };
    final (open, close) = switch ((isCall, isPut)) {
      (true, false) => ('23.1250', '23.2500'),
      (false, true) => ('24.0000', '24.2500'),
      _ => (
        '202.0000',
        switch (range) {
          '1d' => '204.1250',
          '1w' => '203.1250',
          '3m' => '201.1250',
          _ => '202.1250',
        },
      ),
    };
    final high = isCall
        ? '23.3750'
        : isPut
        ? '24.3750'
        : '205.0000';
    final low = isCall
        ? '23.0000'
        : isPut
        ? '23.8750'
        : '200.0000';
    return [
      {
        'timestamp': firstTimestamp,
        'open': open,
        'high': high,
        'low': low,
        'close': open,
        'volume': 10,
      },
      {
        'timestamp': secondTimestamp,
        'open': open,
        'high': high,
        'low': low,
        'close': close,
        'volume': 20,
      },
    ];
  }
}
