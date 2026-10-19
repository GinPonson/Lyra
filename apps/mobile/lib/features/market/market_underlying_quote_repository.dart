import '../../core/api/lyra_api_client.dart';
import 'market_underlying_quote_models.dart';

/// Reads the server-backed underlying-stock quote summary for one source row.
class MarketUnderlyingQuoteRepository {
  const MarketUnderlyingQuoteRepository(this._client);

  final LyraApiClient _client;

  /// Fetches a quote summary using the ledger and saved-settings revisions.
  Future<MarketUnderlyingQuoteRead> quote({
    required MarketUnderlyingQuoteQuery query,
    required int settingsRevision,
  }) async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/market/underlying-quote',
      {
        'scope': query.accountId,
        'symbol_key': query.symbolKey,
        'direction': query.direction,
        'ledger_version': query.ledgerVersion.toString(),
        'settings_revision': settingsRevision.toString(),
      },
    );
    final body = response.data;
    if (body == null) throw const FormatException('正股行情响应为空');
    return MarketUnderlyingQuoteRead.fromJson(
      body,
      query: query,
      settingsRevision: settingsRevision,
    );
  }
}
