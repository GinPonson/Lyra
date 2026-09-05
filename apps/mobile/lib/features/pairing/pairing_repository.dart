import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

/// 配对完成后 App 持久化的凭据集。
class PairedDeviceCredentials {
  const PairedDeviceCredentials({
    required this.deviceId,
    required this.displayName,
    required this.accessToken,
    required this.accessTokenExpiresAt,
    required this.refreshToken,
    required this.refreshTokenExpiresAt,
  });

  final String deviceId;
  final String displayName;
  final String accessToken;
  final String accessTokenExpiresAt;
  final String refreshToken;
  final String refreshTokenExpiresAt;

  factory PairedDeviceCredentials.fromJson(Map<String, Object?> json) {
    return PairedDeviceCredentials(
      deviceId: json['deviceId'] as String,
      displayName: json['displayName'] as String,
      accessToken: json['accessToken'] as String,
      accessTokenExpiresAt: json['accessTokenExpiresAt'] as String,
      refreshToken: json['refreshToken'] as String,
      refreshTokenExpiresAt: json['refreshTokenExpiresAt'] as String,
    );
  }
}

/// 一次刷新轮换后 App 获得的新令牌集；设备身份不随刷新变化。
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

/// 设备配对数据层；配对码由服务端持有方生成，App 只负责兑换。
class PairingRepository {
  PairingRepository({required this._dio});

  final Dio _dio;

  Future<PairedDeviceCredentials> pair({
    required String code,
    required String displayName,
    required String publicKeyPem,
  }) async {
    final response = await _post('/v1/devices/pair', <String, Object?>{
      'code': code,
      'displayName': displayName,
      'publicKeyPem': publicKeyPem,
    });
    return PairedDeviceCredentials.fromJson(
      (response.data as Map).cast<String, Object?>(),
    );
  }

  /// 用刷新凭据换取新令牌；服务端会轮换刷新凭据并撤销被重放的旧代。
  /// 刷新响应不含 deviceId/displayName,设备身份沿用本地快照。
  Future<RefreshedDeviceTokens> refresh({required String refreshToken}) async {
    final response = await _post('/v1/devices/tokens', <String, Object?>{
      'refreshToken': refreshToken,
    });
    return RefreshedDeviceTokens.fromJson(
      (response.data as Map).cast<String, Object?>(),
    );
  }

  Future<Response<dynamic>> _post(String path, Map<String, Object?> body) async {
    try {
      // 配对/刷新走裸 Dio,不经过 LyraApiClient;协议门禁要求显式携带版本头。
      return await _dio.post<dynamic>(
        path,
        data: body,
        options: Options(
          headers: <String, Object?>{
            'x-lyra-protocol-version': kLyraProtocolVersion,
          },
        ),
      );
    } on DioException catch (error) {
      throw LyraApiError.fromResponseBody(
        error.response?.data,
        error.response?.statusCode ?? 0,
        requestId: _requestId(error),
      );
    }
  }
}

/// 服务端在 x-request-id 响应头中回传关联 ID。
String? _requestId(DioException error) {
  return error.response?.headers.value('x-request-id');
}
