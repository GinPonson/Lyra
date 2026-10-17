import 'package:dio/dio.dart';

import '../security/credential_store.dart';
import 'lyra_api_error.dart';

/// 服务端协议版本；与 TypeScript 服务端的门禁保持一致。
const String kLyraProtocolVersion = '3';

/// 用户已停用的稳定错误码（403、不可重试）。
///
/// 停用是终态：客户端据此结束会话，绝不进入令牌刷新与请求重放循环。
const String kUserDisabledCode = 'USER_DISABLED';

/// Lyra 服务端 HTTP 客户端。
///
/// 统一携带协议版本头与设备访问令牌；401 时用刷新凭据换取新令牌并重放一次。
/// 业务调用只消费稳定错误码，不接触底层 HTTP 细节。
class LyraApiClient {
  LyraApiClient({
    required this.serverUrl,
    required this.deviceId,
    required this.credentialStore,
    required this.tokenRefresher,
    this.onUnauthorized,
    this.onUserDisabled,
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
  final String deviceId;
  final CredentialStore credentialStore;
  final Future<String?> Function() tokenRefresher;
  final Future<void> Function()? onUnauthorized;

  /// 服务端返回 [kUserDisabledCode] 时通知接线方结束会话。
  final Future<void> Function()? onUserDisabled;
  final Dio _dio;

  /// 供文件上传等数据层复用同一套拦截器（协议头与令牌注入）。
  Dio get dio => _dio;

  String? _accessToken;
  bool _invalidated = false;
  Future<String?>? _refreshing;

  /// 供调用方检查当前令牌状态。
  String? get accessToken => _accessToken;

  /// 登录身份结束后永久失效，旧引用不能从安全存储拾取新的登录凭据。
  void invalidate() {
    _invalidated = true;
    _accessToken = null;
  }

  LyraApiError get _sessionChanged => const LyraApiError(
    code: 'AUTH_SESSION_CHANGED',
    message: '登录状态已变化，请重新打开页面。',
    statusCode: 401,
    retryable: false,
  );

  Future<CredentialSnapshot> _readBoundCredentials() async {
    if (_invalidated) throw _sessionChanged;
    final snapshot = await withCredentialMutation(
      credentialStore,
      credentialStore.read,
    );
    if (_invalidated ||
        snapshot == null ||
        snapshot.deviceId != deviceId ||
        _normalizeBaseUrl(snapshot.serverUrl) != _normalizeBaseUrl(serverUrl)) {
      invalidate();
      throw _sessionChanged;
    }
    return snapshot;
  }

  Future<Response<T>> get<T>(String path, Map<String, String> query) {
    return _send<T>(() => _dio.get<T>(path, queryParameters: query));
  }

  Future<Response<T>> post<T>(String path, Object? body) {
    return _send<T>(() => _dio.post<T>(path, data: body));
  }

  /// 上传等自定义请求也经过认证刷新；回调每次必须创建新的可重放请求体。
  Future<Response<T>> execute<T>(
    Future<Response<T>> Function(Dio dio) request,
  ) => _send<T>(() => request(_dio));

  /// 401 时尝试刷新一次并重放；其他错误统一转换为稳定客户端错误。
  Future<Response<T>> _send<T>(Future<Response<T>> Function() send) async {
    try {
      if (_invalidated) throw _sessionChanged;
      final result = await send();
      if (_invalidated) throw _sessionChanged;
      return result;
    } on DioException catch (error) {
      if (error.error is LyraApiError) throw error.error!;
      if (_invalidated) throw _sessionChanged;
      final response = error.response;
      final disabled = _userDisabledError(error);
      if (disabled != null) {
        // 停用是终态：先结束会话再抛出不可重试错误，绝不进入刷新与重放。
        await _endDisabledSession();
        throw disabled;
      }
      if (response?.statusCode == 401) {
        await _readBoundCredentials();
        final attempted = error.requestOptions.headers['authorization'];
        String? newToken;
        try {
          // 并发请求共享一次刷新；其他请求已完成轮换时直接用新令牌重放。
          newToken = _accessToken != null && attempted != 'Bearer $_accessToken'
              ? _accessToken
              : await _refreshOnce();
        } on LyraApiError catch (refreshError) {
          // 刷新本身发现用户已停用时同样结束会话，不再重试。
          if (refreshError.code == kUserDisabledCode) {
            await _endDisabledSession();
          } else if (refreshError.statusCode == 401) {
            await _unauthorized();
          }
          rethrow;
        }
        if (newToken == null) {
          await _unauthorized();
          throw const LyraApiError(
            code: 'AUTH_REQUIRED',
            message: '登录已过期，请重新登录。',
            statusCode: 401,
            retryable: false,
          );
        }
        _accessToken = newToken;
        try {
          final result = await send();
          if (_invalidated) throw _sessionChanged;
          return result;
        } on DioException catch (retryError) {
          if (retryError.error is LyraApiError) throw retryError.error!;
          if (_invalidated) throw _sessionChanged;
          final retryDisabled = _userDisabledError(retryError);
          if (retryDisabled != null) {
            await _endDisabledSession();
            throw retryDisabled;
          }
          if (retryError.response?.statusCode == 401) await _unauthorized();
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

  Future<String?> _refreshOnce() async {
    final pending = _refreshing;
    if (pending != null) return pending;
    final refresh = tokenRefresher();
    _refreshing = refresh;
    try {
      final token = await refresh;
      if (_invalidated) throw _sessionChanged;
      _accessToken = token;
      return token;
    } finally {
      _refreshing = null;
    }
  }

  Future<void> _unauthorized() async {
    _accessToken = null;
    await onUnauthorized?.call();
  }

  /// 解析 403 中的用户停用错误；其他 403 保持既有业务语义。
  LyraApiError? _userDisabledError(DioException error) {
    if (error.response?.statusCode != 403) return null;
    final parsed = LyraApiError.fromResponseBody(
      error.response?.data,
      403,
      requestId: _requestId(error),
    );
    return parsed.code == kUserDisabledCode ? parsed : null;
  }

  /// 用户停用后本客户端不再持有令牌；凭据与会话清理由 [onUserDisabled] 接线方完成。
  Future<void> _endDisabledSession() async {
    _accessToken = null;
    await onUserDisabled?.call();
  }

  /// 服务端在 x-request-id 响应头中回传关联 ID；缺失时不构造占位值。
  String? _requestId(DioException error) {
    return error.response?.headers.value('x-request-id');
  }

  /// 把当前访问令牌附加到每个请求；未登录时不附加。
  Future<void> _attachToken(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    try {
      final target = options.uri;
      final server = Uri.parse(serverUrl);
      if (target.scheme != server.scheme ||
          target.host != server.host ||
          target.port != server.port) {
        throw _sessionChanged;
      }
      final snapshot = await _readBoundCredentials();
      final token = _accessToken ?? snapshot.accessToken;
      _accessToken = token;
      options.headers['x-lyra-protocol-version'] = kLyraProtocolVersion;
      options.headers['authorization'] = 'Bearer $token';
      handler.next(options);
    } on Object catch (error) {
      handler.reject(DioException(requestOptions: options, error: error));
    }
  }

  static String _normalizeBaseUrl(String url) {
    final normalized = url.trim();
    return normalized.endsWith('/')
        ? normalized.substring(0, normalized.length - 1)
        : normalized;
  }
}
