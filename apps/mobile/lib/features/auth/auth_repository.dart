import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

/// 浏览器授权事务；兑换凭证只在 App 内存中保存，不进入 URL。
class OAuthLoginStart {
  const OAuthLoginStart({
    required this.loginId,
    required this.authorizationUrl,
    required this.expiresAt,
    required this.pollInterval,
  });

  final String loginId;
  final Uri authorizationUrl;
  final DateTime expiresAt;
  final Duration pollInterval;

  factory OAuthLoginStart.fromJson(Map<String, Object?> json) {
    final url = Uri.parse(json['authorizationUrl'] as String);
    final interval = (json['pollIntervalSeconds'] as num).toInt();
    if (url.scheme != 'https' ||
        url.host != 'accounts.google.com' ||
        interval < 1 ||
        interval > 30) {
      throw const FormatException('登录响应格式无效');
    }
    return OAuthLoginStart(
      loginId: json['loginId'] as String,
      authorizationUrl: url,
      expiresAt: DateTime.parse(json['expiresAt'] as String),
      pollInterval: Duration(seconds: interval),
    );
  }
}

/// 登录完成后持久化的 Lyra 凭据，不包含 Google 访问令牌。
class LoginCredentials {
  const LoginCredentials({
    required this.deviceId,
    required this.displayName,
    required this.accessToken,
    required this.accessTokenExpiresAt,
    required this.refreshToken,
    required this.refreshTokenExpiresAt,
    this.userEmail,
  });

  final String deviceId;
  final String displayName;
  final String accessToken;
  final String accessTokenExpiresAt;
  final String refreshToken;
  final String refreshTokenExpiresAt;
  final String? userEmail;

  factory LoginCredentials.fromJson(Map<String, Object?> json) {
    final user = json['user'] as Map<String, Object?>?;
    return LoginCredentials(
      deviceId: json['deviceId'] as String,
      displayName: json['displayName'] as String,
      accessToken: json['accessToken'] as String,
      accessTokenExpiresAt: json['accessTokenExpiresAt'] as String,
      refreshToken: json['refreshToken'] as String,
      refreshTokenExpiresAt: json['refreshTokenExpiresAt'] as String,
      userEmail: user?['email'] as String?,
    );
  }
}

/// 一次刷新轮换后获得的新令牌集；设备身份不随刷新变化。
class RefreshedDeviceTokens {
  const RefreshedDeviceTokens({
    required this.accessToken,
    required this.accessTokenExpiresAt,
    required this.refreshToken,
    required this.refreshTokenExpiresAt,
  });

  final String accessToken;
  final String accessTokenExpiresAt;
  final String refreshToken;
  final String refreshTokenExpiresAt;

  factory RefreshedDeviceTokens.fromJson(Map<String, Object?> json) {
    return RefreshedDeviceTokens(
      accessToken: json['accessToken'] as String,
      accessTokenExpiresAt: json['accessTokenExpiresAt'] as String,
      refreshToken: json['refreshToken'] as String,
      refreshTokenExpiresAt: json['refreshTokenExpiresAt'] as String,
    );
  }
}

class AuthRepository {
  AuthRepository({required this._dio});

  final Dio _dio;

  Future<OAuthLoginStart> startGoogleLogin({
    required String displayName,
    required String publicKeyPem,
    required String codeChallenge,
    CancelToken? cancelToken,
  }) async {
    final response = await _post('/v1/auth/google/start', <String, Object?>{
      'displayName': displayName,
      'publicKeyPem': publicKeyPem,
      'codeChallenge': codeChallenge,
      'codeChallengeMethod': 'S256',
    }, cancelToken: cancelToken);
    return OAuthLoginStart.fromJson(_body(response));
  }

  /// pending 返回 null；成功只能兑换一次，错误由稳定错误码表达。
  Future<LoginCredentials?> exchangeGoogleLogin({
    required String loginId,
    required String codeVerifier,
    CancelToken? cancelToken,
  }) async {
    final response = await _post('/v1/auth/google/exchange', <String, Object?>{
      'loginId': loginId,
      'codeVerifier': codeVerifier,
    }, cancelToken: cancelToken);
    final body = _body(response);
    if (response.statusCode == 202 && body['status'] == 'pending') {
      return null;
    }
    if (response.statusCode != 200 || body['status'] != 'complete') {
      throw const FormatException('登录响应格式无效');
    }
    return LoginCredentials.fromJson(body);
  }

  Future<RefreshedDeviceTokens> refresh({required String refreshToken}) async {
    final response = await _post('/v1/devices/tokens', <String, Object?>{
      'refreshToken': refreshToken,
    });
    return RefreshedDeviceTokens.fromJson(_body(response));
  }

  /// 退出使用捕获的原设备令牌，不从当前登录存储读取其他身份。
  Future<void> logout({required String accessToken}) async {
    await _post(
      '/v1/auth/logout',
      const <String, Object?>{},
      accessToken: accessToken,
    );
  }

  Map<String, Object?> _body(Response<dynamic> response) =>
      (response.data as Map).cast<String, Object?>();

  Future<Response<dynamic>> _post(
    String path,
    Map<String, Object?> body, {
    CancelToken? cancelToken,
    String? accessToken,
  }) async {
    try {
      return await _dio.post<dynamic>(
        path,
        data: body,
        cancelToken: cancelToken,
        options: Options(
          headers: <String, Object?>{
            'x-lyra-protocol-version': kLyraProtocolVersion,
            if (accessToken != null) 'authorization': 'Bearer $accessToken',
          },
        ),
      );
    } on DioException catch (error) {
      if (CancelToken.isCancel(error)) rethrow;
      throw LyraApiError.fromResponseBody(
        error.response?.data,
        error.response?.statusCode ?? 0,
        requestId: error.response?.headers.value('x-request-id'),
      );
    }
  }
}
