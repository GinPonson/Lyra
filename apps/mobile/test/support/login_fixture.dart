import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';

import '../session_flow_test.dart' show FakeLyraAdapter;
import 'ledger_fixture.dart';

const loginFixtureServer = 'https://demo.lyra.example';

/// 已配对凭据的稳定 userId；`/v1/me` 必须返回同一个值，否则会话会被判定换人。
const loginFixtureUserId = 'restored-user';

/// 按 P0 冻结契约 §3.9 生成登录响应与 `/v1/me` 共用的 `user` 对象。
Map<String, Object?> userPayload({
  required String userId,
  required AuthProvider provider,
  required String subject,
  String? email,
  String displayName = 'Demo Google',
  String status = 'active',
  String role = 'member',
}) => {
  'userId': userId,
  'displayName': displayName,
  'status': status,
  'role': role,
  'identity': {
    'provider': provider.id,
    'subject': subject,
    'email': email,
  },
};

class LoginCredentialStore extends MemoryCredentialStore {
  bool failClear = false;
  @override
  Future<void> clear() async {
    if (failClear) throw StateError('Synthetic secure-storage failure');
    await super.clear();
  }
}

/// 三种真实协议形状的合成响应，不访问第三方、密钥或真实用户数据。
class LoginFixture extends LedgerFixture {
  final store = LoginCredentialStore();
  final agentStore = MemoryAgentSessionStore();
  final openedUrls = <Uri>[];
  Set<AuthProvider> available = AuthProvider.values.toSet();
  bool pending = false;
  bool browserAvailable = true;
  bool logoutOffline = false;
  bool credentialsExpired = false;
  String? loginError;
  int loginErrorStatus = 403;
  int? catalogStatus;
  bool registrationRequiresInvite = false;
  AuthProvider? exchangedProvider;
  String? authorizationUrlOverride;
  Completer<ResponseBody>? catalogGate;

  Dio dio(String url) => Dio(BaseOptions(baseUrl: url))
    ..httpClientAdapter = FakeLyraAdapter((options) async {
      requests.add(options);
      final path = options.uri.path;
      if (credentialsExpired &&
          (path == '/v1/ledger/summary' || path == '/v1/devices/tokens')) {
        return json({
          'code': 'AUTH_REQUIRED',
          'message': '登录已过期，请重新登录。',
          'retryable': false,
        }, 401);
      }
      if (path == '/v1/auth/providers') {
        if (catalogGate != null) return catalogGate!.future;
        if (catalogStatus != null) {
          return json({
            'code': 'UNAVAILABLE',
            'message': '暂不可用',
            'retryable': true,
          }, catalogStatus!);
        }
        return json(catalog());
      }
      if (path == '/v1/auth/logout') {
        if (logoutOffline) {
          throw DioException(
            requestOptions: options,
            type: DioExceptionType.connectionTimeout,
          );
        }
        return ResponseBody.fromString('', 204);
      }
      if (path == '/v1/me') {
        // 身份核对读取当前设备会话解析出的用户；与已配对凭据使用同一 userId。
        return json({
          'user': userPayload(
            userId: loginFixtureUserId,
            provider: AuthProvider.google,
            subject: 'legacy-google-subject',
            email: 'demo@example.com',
            displayName: 'Demo Google',
          ),
        });
      }
      if (path == '/v1/system/info') {
        return json({
          'server_version': '0.1.0',
          'protocol_version': 3,
          'min_supported_protocol_version': 3,
          'max_supported_protocol_version': 3,
          'schema_version': 16,
        });
      }
      if (path == '/v1/health') return json({'status': 'healthy'});
      final parts = path.split('/');
      final provider = parts.length == 5 && parts[2] == 'auth'
          ? AuthProvider.fromId(parts[3])
          : null;
      if (provider != null && parts[4] == 'start') {
        if (!available.contains(provider)) {
          return json({
            'code': 'AUTH_NOT_CONFIGURED',
            'message': '这个登录方式尚未配置。',
            'retryable': false,
          }, 503);
        }
        return json({
          'loginId': '${provider.id}-login',
          'authorizationUrl':
              authorizationUrlOverride ??
              'https://${provider.authorizationHost}${provider.authorizationPath}?state=synthetic-${provider.id}',
          'expiresAt': DateTime.now()
              .add(const Duration(minutes: 10))
              .toUtc()
              .toIso8601String(),
          'pollIntervalSeconds': 1,
        }, 201);
      }
      if (provider != null && parts[4] == 'exchange') {
        if (loginError != null) {
          return json({
            'code': loginError,
            'message': '此账号尚未获准访问这个服务端。',
            'retryable': false,
          }, loginErrorStatus);
        }
        if (pending) return json({'status': 'pending'}, 202);
        final resultProvider = exchangedProvider ?? provider;
        return json({
          'status': 'complete',
          'user': userPayload(
            userId: '${resultProvider.id}-user',
            provider: resultProvider,
            subject: 'subject-${resultProvider.id}',
            email: resultProvider == AuthProvider.google
                ? 'demo@example.com'
                : null,
            displayName: 'Demo ${resultProvider.label}',
          ),
          'deviceId': '${resultProvider.id}-device',
          'displayName': 'Lyra Mobile',
          'accessToken': '${resultProvider.id}-access',
          'refreshToken': '${resultProvider.id}-refresh',
          'accessTokenExpiresAt': '2026-10-12T10:00:00Z',
          'refreshTokenExpiresAt': '2026-11-12T10:00:00Z',
        });
      }
      if (path == '/v1/devices/tokens') {
        return json({
          'accessToken': 'refreshed-access',
          'refreshToken': 'refreshed-refresh',
          'accessTokenExpiresAt': '2026-10-12T10:00:00Z',
          'refreshTokenExpiresAt': '2026-11-12T10:00:00Z',
        });
      }
      return json(response(path.split('/').last, options.queryParameters));
    });

  Map<String, Object?> catalog() => {
    'providers': [
      for (final provider in AuthProvider.values)
        {
          'id': provider.id,
          'name': provider.label,
          'available': available.contains(provider),
        },
    ],
    'registrationRequiresInvite': registrationRequiresInvite,
  };

  static ResponseBody json(Object data, [int status = 200]) =>
      ResponseBody.fromString(
        jsonEncode(data),
        status,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );

  @override
  Future<SessionDependencies> dependencies({bool paired = false}) async {
    if (paired) {
      await store.write(
        const CredentialSnapshot(
          serverUrl: loginFixtureServer,
          deviceId: 'restored-device',
          accessToken: 'restored-access',
          refreshToken: 'restored-refresh',
          userId: loginFixtureUserId,
          authMethod: 'google',
          googleSubject: 'legacy-google-subject',
          userEmail: 'demo@example.com',
        ),
      );
    }
    return SessionDependencies(
      credentialStore: store,
      agentSessionStore: agentStore,
      clientFactory: dio,
      openAuthorizationUrl: (url) async {
        openedUrls.add(url);
        return browserAvailable;
      },
    );
  }
}
