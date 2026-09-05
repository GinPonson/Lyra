import 'package:dio/dio.dart';

import '../security/credential_store.dart';
import 'lyra_api_error.dart';

/// 服务端协议版本；与 TypeScript 服务端的门禁保持一致。
const String kLyraProtocolVersion = '1';

/// Lyra 服务端 HTTP 客户端。
///
/// 统一携带协议版本头与设备访问令牌；401 时用刷新凭据换取新令牌并重放一次。
/// 业务调用只消费稳定错误码，不接触底层 HTTP 细节。
class LyraApiClient {
  LyraApiClient({
    required this.serverUrl,
    required this.credentialStore,
    required this.tokenRefresher,
    Dio? dio,
  }) : _dio =
           dio ??
           Dio(
             BaseOptions(
               baseUrl: _normalizeBaseUrl(serverUrl),
               connectTimeout: const Duration(seconds: 10),
               receiveTimeout: const Duration(seconds: 30),
               headers: <String, Object?>{
                 'x-lyra-protocol-version': kLyraProtocolVersion,
               },
             ),
           ) {
    _dio.interceptors.add(InterceptorsWrapper(onRequest: _attachToken));
  }

  final String serverUrl;
  final CredentialStore credentialStore;
  final Future<String?> Function() tokenRefresher;
  final Dio _dio;

  String? _accessToken;

  /// 供调用方检查当前令牌状态。
  String? get accessToken => _accessToken;

  /// 配对或刷新成功后更新内存令牌；持久化由凭据存储负责。
  Future<void> setAccessToken(String token) async {
    _accessToken = token;
    final snapshot = await credentialStore.read();
    if (snapshot != null) {
      await credentialStore.write(
        CredentialSnapshot(
          serverUrl: snapshot.serverUrl,
          deviceId: snapshot.deviceId,
          accessToken: token,
          refreshToken: snapshot.refreshToken,
          devicePrivateKey: snapshot.devicePrivateKey,
        ),
      );
    }
  }

  Future<void> loadStoredToken() async {
    _accessToken = (await credentialStore.read())?.accessToken;
  }

  Future<Response<T>> get<T>(String path, Map<String, String> query) {
    return _send<T>(() => _dio.get<T>(path, queryParameters: query));
  }

  Future<Response<T>> post<T>(String path, Object? body) {
    return _send<T>(() => _dio.post<T>(path, data: body));
  }

  /// 401 时尝试刷新一次并重放；其他错误统一转换为稳定客户端错误。
  Future<Response<T>> _send<T>(Future<Response<T>> Function() send) async {
    try {
      return await send();
    } on DioException catch (error) {
      final response = error.response;
      if (response?.statusCode == 401) {
        final newToken = await tokenRefresher();
        _accessToken = newToken;
        try {
          return await send();
        } on DioException catch (retryError) {
          throw LyraApiError.fromResponseBody(
            retryError.response?.data,
            retryError.response?.statusCode ?? 0,
            requestId: _requestId(retryError),
          );
        }
      }
      throw LyraApiError.fromResponseBody(
        response?.data,
        response?.statusCode ?? 0,
        requestId: _requestId(error),
      );
    }
  }

  /// 服务端在 x-request-id 响应头中回传关联 ID；缺失时不构造占位值。
  String? _requestId(DioException error) {
    return error.response?.headers.value('x-request-id');
  }

  /// 把当前访问令牌附加到每个请求；未配对时不附加。
  Future<void> _attachToken(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    final token = _accessToken ?? (await credentialStore.read())?.accessToken;
    _accessToken = token;
    if (token != null) {
      options.headers['authorization'] = 'Bearer $token';
    }
    handler.next(options);
  }

  static String _normalizeBaseUrl(String url) {
    final normalized = url.trim();
    return normalized.endsWith('/')
        ? normalized.substring(0, normalized.length - 1)
        : normalized;
  }
}
