import '../../core/api/lyra_api_client.dart';
import 'market_models.dart';
import 'market_settings.dart';

class MarketRepository {
  MarketRepository(this._client);
  final LyraApiClient _client;

  Future<MarketSettings> settings() async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/market/settings',
      const {},
    );
    if (response.data == null) throw const FormatException('行情设置响应为空');
    return MarketSettings.fromJson(response.data!);
  }

  Future<MarketSettings> saveSettings({
    required int expectedRevision,
    required MarketRoutes routes,
  }) async {
    final response = await _client.execute<Map<String, Object?>>(
      (dio) => dio.put<Map<String, Object?>>(
        '/v1/market/settings',
        data: {'expectedRevision': expectedRevision, 'routes': routes.toJson()},
      ),
    );
    if (response.data == null) throw const FormatException('行情设置响应为空');
    return MarketSettings.fromJson(response.data!);
  }

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
