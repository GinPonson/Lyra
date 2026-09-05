/// 服务端稳定错误协议的客户端模型。
///
/// 服务端保证不返回堆栈、路径或令牌；客户端只消费 [code]、[message]、
/// [retryable] 与可操作的结构化 [details]，用于分支处理而不是展示底层细节。
class LyraApiError implements Exception {
  const LyraApiError({
    required this.code,
    required this.message,
    required this.statusCode,
    required this.retryable,
    this.requestId,
    this.details,
  });

  final String code;
  final String message;
  final int statusCode;
  final bool retryable;
  final String? requestId;

  /// 服务端可操作信息（如 `candidate_account_ids`）；非结构化时保持 null。
  final Map<String, Object?>? details;

  /// 把服务端稳定错误体或框架层异常转换为客户端错误；未知形态不猜测细节。
  factory LyraApiError.from(Object error) {
    if (error is LyraApiError) {
      return error;
    }
    return const LyraApiError(
      code: 'CLIENT_REQUEST_FAILED',
      message: '无法连接 Lyra 服务端',
      statusCode: 0,
      retryable: true,
    );
  }

  static LyraApiError fromResponseBody(
    Object? body,
    int statusCode, {
    String? requestId,
  }) {
    if (body is Map) {
      final code = body['code'];
      final message = body['message'];
      if (code is String && message is String) {
        final detailsRaw = body['details'];
        return LyraApiError(
          code: code,
          message: message,
          statusCode: statusCode,
          retryable: body['retryable'] == true,
          requestId: requestId ?? (body['request_id'] as String?),
          details: detailsRaw is Map
              ? detailsRaw.cast<String, Object?>()
              : null,
        );
      }
    }
    return LyraApiError(
      code: 'CLIENT_REQUEST_FAILED',
      message: '服务端请求失败',
      statusCode: statusCode,
      retryable: false,
      requestId: requestId,
    );
  }

  @override
  String toString() => 'LyraApiError($code, $statusCode): $message';
}
