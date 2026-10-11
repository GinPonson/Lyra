import '../../core/api/lyra_api_client.dart';
import 'market_models.dart';

class MarketRepository {
  MarketRepository(this._client);
  final LyraApiClient _client;

  Future<MarketPositionsRead> positions({
    required String scope,
    String? underlying,
  }) async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/market/positions',
      {
        'scope': scope,
        if (underlying != null && underlying.isNotEmpty)
          'underlying': underlying,
      },
    );
    final body = response.data;
    if (body == null) throw const FormatException('行情响应为空');
    return MarketPositionsRead.fromJson(body);
  }
}
