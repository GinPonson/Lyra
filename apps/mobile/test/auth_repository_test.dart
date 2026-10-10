import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/auth/auth_repository.dart';

void main() {
  test('登录响应解析完整凭据集', () {
    final credentials = LoginCredentials.fromJson(const {
      'deviceId': 'd1000000-0000-4000-8000-000000000001',
      'displayName': 'Test phone',
      'accessToken': 'access-token-1',
      'accessTokenExpiresAt': '2026-09-02T10:15:00.000Z',
      'refreshToken': 'refresh-token-1',
      'refreshTokenExpiresAt': '2026-10-02T10:00:00.000Z',
    });
    expect(credentials.deviceId, 'd1000000-0000-4000-8000-000000000001');
    expect(credentials.displayName, 'Test phone');
    expect(credentials.accessToken, 'access-token-1');
    expect(credentials.refreshToken, 'refresh-token-1');
  });

  test('刷新响应按服务端四字段形状解析，不要求设备身份字段', () {
    // 服务端 /v1/devices/tokens 只返回令牌与到期时间；设备身份沿用本地快照。
    final tokens = RefreshedDeviceTokens.fromJson(const {
      'accessToken': 'access-token-2',
      'accessTokenExpiresAt': '2026-09-05T13:44:50.737Z',
      'refreshToken': 'refresh-token-2',
      'refreshTokenExpiresAt': '2026-10-05T13:29:50.737Z',
    });
    expect(tokens.accessToken, 'access-token-2');
    expect(tokens.accessTokenExpiresAt, '2026-09-05T13:44:50.737Z');
    expect(tokens.refreshToken, 'refresh-token-2');
    expect(tokens.refreshTokenExpiresAt, '2026-10-05T13:29:50.737Z');
  });
}
