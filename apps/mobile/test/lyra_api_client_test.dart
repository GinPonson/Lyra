import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;

void main() {
  test('并发 401 共享一次刷新，所有请求使用新令牌重放且携带协议 3', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final started = Completer<void>();
    final refreshed = Completer<String?>();
    var refreshCount = 0;
    final headers = <Object?>[];
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () {
        refreshCount++;
        if (!started.isCompleted) started.complete();
        return refreshed.future;
      },
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          expect(options.headers['x-lyra-protocol-version'], '3');
          final authorization = options.headers['authorization'];
          headers.add(authorization);
          return _response(authorization == 'Bearer fresh-token' ? 200 : 401);
        }),
    );
    final requests = [
      client.get<Object?>('/one', {}),
      client.get<Object?>('/two', {}),
    ];
    await started.future;
    await pumpEventQueue();
    expect(refreshCount, 1);
    refreshed.complete('fresh-token');
    await Future.wait(requests);
    expect(refreshCount, 1);
    expect(
      headers.where((value) => value == 'Bearer fresh-token'),
      hasLength(2),
    );
  });

  test('无刷新凭据时停止重放并通知会话退出', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requestCount = 0;
    var signedOut = false;
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async => null,
      onUnauthorized: () async {
        signedOut = true;
        await store.clear();
      },
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requestCount++;
          return _response(401);
        }),
    );
    await expectLater(
      client.get<Object?>('/private', {}),
      throwsA(isA<LyraApiError>().having((e) => e.statusCode, 'status', 401)),
    );
    expect(signedOut, isTrue);
    expect(requestCount, 1);
    expect(await store.read(), isNull);
  });
  for (final otherServer in [false, true]) {
    test('旧客户端拒绝不同${otherServer ? '服务端' : '设备'}的存储凭据', () async {
      final store = MemoryCredentialStore();
      await store.write(_snapshot);
      var requests = 0;
      final client = LyraApiClient(
        serverUrl: _snapshot.serverUrl,
        deviceId: _snapshot.deviceId,
        credentialStore: store,
        tokenRefresher: () async => throw StateError('不能刷新其他登录'),
        dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
          ..httpClientAdapter = FakeLyraAdapter((options) async {
            requests++;
            expect(options.headers['authorization'], 'Bearer expired-token');
            return _response(200);
          }),
      );
      await client.get<Object?>('/first', {});
      await store.write(
        CredentialSnapshot(
          serverUrl: otherServer
              ? 'https://other.example.com'
              : _snapshot.serverUrl,
          deviceId: otherServer ? _snapshot.deviceId : 'other-device',
          accessToken: 'other-private-token',
          refreshToken: 'other-refresh',
          userId: 'other-user',
          authMethod: 'google',
        ),
      );
      await expectLater(
        client.get<Object?>('/stale', {}),
        throwsA(
          isA<LyraApiError>().having(
            (e) => e.code,
            'code',
            'AUTH_SESSION_CHANGED',
          ),
        ),
      );
      expect(requests, 1);
      expect((await store.read())?.accessToken, 'other-private-token');
    });
  }

  test('用户停用结束会话且不刷新、不重放，错误保持不可重试', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requests = 0;
    var refreshes = 0;
    var disabledSessions = 0;
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async {
        refreshes++;
        return 'fresh-token';
      },
      onUserDisabled: () async {
        disabledSessions++;
        await store.clear();
      },
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests++;
          return ResponseBody.fromString(
            '{"code":"USER_DISABLED","message":"该账号已停用","retryable":false}',
            403,
            headers: {
              Headers.contentTypeHeader: [Headers.jsonContentType],
            },
          );
        }),
    );
    await expectLater(
      client.get<Object?>('/v1/ledger/summary', {}),
      throwsA(
        isA<LyraApiError>()
            .having((e) => e.code, 'code', 'USER_DISABLED')
            .having((e) => e.statusCode, 'status', 403)
            .having((e) => e.retryable, 'retryable', isFalse),
      ),
    );
    expect(disabledSessions, 1);
    expect(refreshes, 0);
    expect(requests, 1);
    expect(client.accessToken, isNull);
    expect(await store.read(), isNull);
  });

  test('身份失效后的旧客户端不能重用缓存令牌', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requests = 0;
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async => null,
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests++;
          return _response(200);
        }),
    );
    await client.get<Object?>('/first', {});
    client.invalidate();
    await expectLater(
      client.get<Object?>('/after-logout', {}),
      throwsA(isA<LyraApiError>()),
    );
    expect(requests, 1);
  });
}

const _snapshot = CredentialSnapshot(
  serverUrl: 'https://lyra.example.com',
  deviceId: 'test-device',
  accessToken: 'expired-token',
  refreshToken: 'refresh-token',
  userId: 'client-user',
  authMethod: 'google',
);

ResponseBody _response(int status) => ResponseBody.fromString(
  status == 200 ? '{}' : '{"code":"DEVICE_AUTH_INVALID","message":"expired"}',
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);
