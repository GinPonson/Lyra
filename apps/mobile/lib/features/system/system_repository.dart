import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

enum ServerConnectionStatus { healthy, degraded, unavailable, incompatible }

class ServerConnectionCheck {
  const ServerConnectionCheck({
    required this.serverUrl,
    required this.serverVersion,
    required this.status,
    required this.checkedAt,
    required this.isCompatible,
  });

  final String serverUrl;
  final String serverVersion;
  final ServerConnectionStatus status;
  final DateTime checkedAt;
  final bool isCompatible;
}

/// 与登录入口相同的地址边界；普通远程服务器使用 HTTPS。
String normalizeLyraServerUrl(String value) {
  final uri = Uri.tryParse(value.trim());
  final loopback =
      uri != null && const ['localhost', '127.0.0.1', '::1'].contains(uri.host);
  if (uri == null ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment ||
      !(uri.scheme == 'https' || (uri.scheme == 'http' && loopback))) {
    throw const LyraApiError(
      code: 'SERVER_URL_INVALID',
      message: '请输入 HTTPS 服务端地址（本机开发可使用 HTTP）。',
      statusCode: 0,
      retryable: false,
    );
  }
  return uri.toString().replaceFirst(RegExp(r'/+$'), '');
}

/// 公开服务检查使用独立 HTTP 客户端，不读取凭据、不刷新登录、不撤销会话。
class SystemRepository {
  SystemRepository({required String serverUrl, Dio? dio})
    : serverUrl = normalizeLyraServerUrl(serverUrl),
      _dio = dio ?? createDio(normalizeLyraServerUrl(serverUrl));

  final String serverUrl;
  final Dio _dio;

  static Dio createDio(String serverUrl) => Dio(
    BaseOptions(
      baseUrl: serverUrl,
      connectTimeout: const Duration(seconds: 8),
      receiveTimeout: const Duration(seconds: 8),
      followRedirects: false,
    ),
  );

  Future<ServerConnectionCheck> check({CancelToken? cancelToken}) async {
    try {
      final response = await _dio.get<Object?>(
        '$serverUrl/v1/system/info',
        cancelToken: cancelToken,
        options: Options(followRedirects: false),
      );
      final body = response.data;
      if (body is! Map<String, Object?>) throw const FormatException();
      final version = body['server_version'];
      final protocol = body['protocol_version'];
      final minimum = body['min_supported_protocol_version'];
      final maximum = body['max_supported_protocol_version'];
      final schema = body['schema_version'];
      if (version is! String ||
          version.trim().isEmpty ||
          protocol is! int ||
          protocol < 1 ||
          minimum is! int ||
          minimum < 1 ||
          maximum is! int ||
          maximum < minimum ||
          protocol < minimum ||
          protocol > maximum ||
          schema is! int ||
          schema < 0) {
        throw const FormatException();
      }
      final clientProtocol = int.parse(kLyraProtocolVersion);
      final compatible = clientProtocol >= minimum && clientProtocol <= maximum;
      if (!compatible) {
        return ServerConnectionCheck(
          serverUrl: serverUrl,
          serverVersion: version,
          status: ServerConnectionStatus.incompatible,
          checkedAt: DateTime.now().toUtc(),
          isCompatible: false,
        );
      }

      final health = await _dio.get<Object?>(
        '$serverUrl/v1/health',
        cancelToken: cancelToken,
        options: Options(
          followRedirects: false,
          validateStatus: (status) => status == 200 || status == 503,
        ),
      );
      final data = health.data;
      if (data is! Map<String, Object?>) throw const FormatException();
      final status = switch (data['status']) {
        'healthy' => ServerConnectionStatus.healthy,
        'degraded' => ServerConnectionStatus.degraded,
        'unavailable' => ServerConnectionStatus.unavailable,
        _ => throw const FormatException(),
      };
      if (health.statusCode == 503 &&
          status != ServerConnectionStatus.unavailable) {
        throw const FormatException();
      }
      return ServerConnectionCheck(
        serverUrl: serverUrl,
        serverVersion: version,
        status: status,
        checkedAt: DateTime.now().toUtc(),
        isCompatible: true,
      );
    } on FormatException {
      throw const LyraApiError(
        code: 'SERVER_INFO_INVALID',
        message: '该地址没有返回有效的 Lyra 服务信息。',
        statusCode: 0,
        retryable: false,
      );
    } on DioException catch (error) {
      if (CancelToken.isCancel(error)) rethrow;
      final status = error.response?.statusCode ?? 0;
      throw LyraApiError(
        code: 'SERVER_CHECK_FAILED',
        message: status == 0
            ? '暂时无法连接服务端，请检查网络或服务器状态。'
            : '服务检查未能完成，请检查服务端配置后重试。',
        statusCode: status,
        retryable: status == 0 || status >= 500 || status == 429,
      );
    }
  }

  void close() => _dio.close(force: true);
}
