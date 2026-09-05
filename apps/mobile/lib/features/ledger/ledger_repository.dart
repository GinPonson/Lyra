import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import 'ledger_models.dart';

/// 只读账本数据层；查询显式携带账户范围（设计方案 §11.1）。
class LedgerRepository {
  LedgerRepository(this._apiClient);

  final LyraApiClient _apiClient;

  Future<LedgerSummaryRead> summary({required String scope}) async {
    final response = await _apiClient.get<Map<String, Object?>>(
      '/v1/ledger/summary',
      <String, String>{'scope': scope},
    );
    final body = response.data;
    if (body is! Map<String, Object?>) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '账本查询响应格式无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return LedgerSummaryRead.fromJson(body);
  }
}
