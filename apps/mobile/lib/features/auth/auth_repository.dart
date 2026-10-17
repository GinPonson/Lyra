import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import 'auth_provider.dart';
import 'user_profile.dart';

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

  factory OAuthLoginStart.fromJson(
    Map<String, Object?> json, {
    AuthProvider provider = AuthProvider.google,
  }) {
    final url = Uri.parse(json['authorizationUrl'] as String);
    final interval = (json['pollIntervalSeconds'] as num).toInt();
    if (!provider.acceptsAuthorizationUrl(url) ||
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

/// 登录完成后持久化的 Lyra 凭据，不包含第三方访问令牌。
class LoginCredentials {
  const LoginCredentials({
    required this.user,
    required this.deviceId,
    required this.displayName,
    required this.accessToken,
    required this.accessTokenExpiresAt,
    required this.refreshToken,
    required this.refreshTokenExpiresAt,
  });

  /// 登录响应中的 `user` 对象；与 `GET /v1/me` 共用同一解析器与形状。
  final LyraUserProfile user;
  final String deviceId;
  final String displayName;
  final String accessToken;
  final String accessTokenExpiresAt;
  final String refreshToken;
  final String refreshTokenExpiresAt;

  AuthProvider get provider => user.identity.provider;
  String get subject => user.identity.subject;
  String? get userEmail => user.identity.email;
  String? get userDisplayName => user.displayName;
  String? get googleSubject =>
      provider == AuthProvider.google ? user.identity.subject : null;

  factory LoginCredentials.fromJson(Map<String, Object?> json) {
    final user = json['user'];
    if (user is! Map<String, Object?>) {
      throw const FormatException('登录身份响应格式无效');
    }
    return LoginCredentials(
      user: LyraUserProfile.fromJson(user),
      deviceId: json['deviceId'] as String,
      displayName: json['displayName'] as String,
      accessToken: json['accessToken'] as String,
      accessTokenExpiresAt: json['accessTokenExpiresAt'] as String,
      refreshToken: json['refreshToken'] as String,
      refreshTokenExpiresAt: json['refreshTokenExpiresAt'] as String,
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

  Future<AuthProviderCatalog> providers({CancelToken? cancelToken}) async {
    try {
      final response = await _dio.get<dynamic>(
        '/v1/auth/providers',
        cancelToken: cancelToken,
        options: Options(
          followRedirects: false,
          headers: const {'x-lyra-protocol-version': kLyraProtocolVersion},
        ),
      );
      return AuthProviderCatalog.parse(_body(response));
    } on DioException catch (error) {
      if (CancelToken.isCancel(error)) rethrow;
      throw LyraApiError.fromResponseBody(
        error.response?.data,
        error.response?.statusCode ?? 0,
      );
    }
  }

  Future<OAuthLoginStart> startGoogleLogin({
    required String displayName,
    required String publicKeyPem,
    required String codeChallenge,
    CancelToken? cancelToken,
  }) => startLogin(
    provider: AuthProvider.google,
    displayName: displayName,
    publicKeyPem: publicKeyPem,
    codeChallenge: codeChallenge,
    cancelToken: cancelToken,
  );

  Future<OAuthLoginStart> startLogin({
    required AuthProvider provider,
    required String displayName,
    required String publicKeyPem,
    required String codeChallenge,
    String? inviteCode,
    CancelToken? cancelToken,
  }) async {
    final response = await _post(
      '/v1/auth/${provider.id}/start',
      <String, Object?>{
        'displayName': displayName,
        'publicKeyPem': publicKeyPem,
        'codeChallenge': codeChallenge,
        'codeChallengeMethod': 'S256',
        // 旧服务端不认该键，空邀请码按"未填写"处理，不发空字符串。
        if (inviteCode != null && inviteCode.isNotEmpty)
          'inviteCode': inviteCode,
      },
      cancelToken: cancelToken,
    );
    return OAuthLoginStart.fromJson(_body(response), provider: provider);
  }

  /// pending 返回 null；成功只能兑换一次，错误由稳定错误码表达。
  Future<LoginCredentials?> exchangeGoogleLogin({
    required String loginId,
    required String codeVerifier,
    CancelToken? cancelToken,
  }) => exchangeLogin(
    provider: AuthProvider.google,
    loginId: loginId,
    codeVerifier: codeVerifier,
    cancelToken: cancelToken,
  );

  Future<LoginCredentials?> exchangeLogin({
    required AuthProvider provider,
    required String loginId,
    required String codeVerifier,
    CancelToken? cancelToken,
  }) async {
    final response = await _post(
      '/v1/auth/${provider.id}/exchange',
      <String, Object?>{'loginId': loginId, 'codeVerifier': codeVerifier},
      cancelToken: cancelToken,
    );
    final body = _body(response);
    if (response.statusCode == 202 && body['status'] == 'pending') {
      return null;
    }
    if (response.statusCode != 200 || body['status'] != 'complete') {
      throw const FormatException('登录响应格式无效');
    }
    final credentials = LoginCredentials.fromJson(body);
    if (credentials.provider != provider) {
      throw const FormatException('登录身份与所选方式不一致');
    }
    return credentials;
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
          followRedirects: false,
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
