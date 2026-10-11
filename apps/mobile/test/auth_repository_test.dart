import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/auth/auth_repository.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';

void main() {
  for (final provider in AuthProvider.values) {
    test('${provider.label} 只接受绑定的 HTTPS 授权入口', () {
      final canonical =
          'https://${provider.authorizationHost}${provider.authorizationPath}?state=synthetic';
      OAuthLoginStart parse(String url) => OAuthLoginStart.fromJson({
        'loginId': 'login-1',
        'authorizationUrl': url,
        'expiresAt': '2026-10-11T11:00:00Z',
        'pollIntervalSeconds': 2,
      }, provider: provider);
      expect(
        parse(canonical).authorizationUrl.host,
        provider.authorizationHost,
      );
      for (final url in [
        canonical.replaceFirst('https:', 'http:'),
        canonical.replaceFirst(
          provider.authorizationHost,
          '${provider.authorizationHost}.invalid',
        ),
        canonical.replaceFirst(
          provider.authorizationHost,
          '${provider.authorizationHost}:8443',
        ),
        canonical.replaceFirst('https://', 'https://user@'),
        canonical.replaceFirst(provider.authorizationPath, '/unexpected'),
        '$canonical#token',
      ]) {
        expect(() => parse(url), throwsFormatException);
      }
    });
  }

  test('目录拒绝重复、未知或缺失身份，保持固定提供方顺序', () {
    final entries = [
      for (final provider in AuthProvider.values.reversed)
        {
          'id': provider.id,
          'name': provider.label,
          'available': provider != AuthProvider.linuxdo,
        },
    ];
    final catalog = AuthProviderAvailability.parseCatalog({
      'providers': entries,
    });
    expect(catalog.map((item) => item.provider), AuthProvider.values);
    expect(catalog[1].available, isFalse);
    for (final invalid in [
      entries.take(2).toList(),
      [entries[0], entries[0], entries[2]],
      [
        entries[0],
        entries[1],
        {'id': 'custom', 'name': 'custom', 'available': true},
      ],
    ]) {
      expect(
        () => AuthProviderAvailability.parseCatalog({'providers': invalid}),
        throwsFormatException,
      );
    }
  });

  test('登录响应解析完整凭据集', () {
    final credentials = LoginCredentials.fromJson(const {
      'deviceId': 'd1000000-0000-4000-8000-000000000001',
      'displayName': 'Test phone',
      'accessToken': 'access-token-1',
      'accessTokenExpiresAt': '2026-09-02T10:15:00.000Z',
      'refreshToken': 'refresh-token-1',
      'refreshTokenExpiresAt': '2026-10-02T10:00:00.000Z',
      'user': {
        'provider': 'google',
        'subject': 'stable-google-subject',
        'email': 'owner@example.com',
      },
    });
    expect(credentials.deviceId, 'd1000000-0000-4000-8000-000000000001');
    expect(credentials.displayName, 'Test phone');
    expect(credentials.accessToken, 'access-token-1');
    expect(credentials.refreshToken, 'refresh-token-1');
    expect(credentials.googleSubject, 'stable-google-subject');
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
