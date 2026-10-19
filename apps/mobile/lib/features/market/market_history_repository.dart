import '../../core/api/lyra_api_client.dart';
import 'market_history_models.dart';

/// Reads provider-backed historical bars without changing the active quote route.
class MarketHistoryRepository {
  const MarketHistoryRepository(this._client);

  final LyraApiClient _client;

  /// Fetches one position or underlying series for [query] at [settingsRevision].
  Future<MarketHistoryRead> history({
    required MarketHistoryQuery query,
    required int settingsRevision,
  }) async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/market/history',
      {
        'scope': query.accountId,
        'symbol_key': query.symbolKey,
        'direction': query.direction,
        'target': query.target.apiValue,
        'range': query.range.apiValue,
        'ledger_version': query.ledgerVersion.toString(),
        'settings_revision': settingsRevision.toString(),
      },
    );
    final body = response.data;
    if (body == null) throw const FormatException('行情历史响应为空');
    return MarketHistoryRead.fromJson(
      body,
      query: query,
      settingsRevision: settingsRevision,
    );
  }
}
