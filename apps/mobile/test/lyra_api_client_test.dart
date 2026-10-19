import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;

void main() {
  for (final expiredDeviceFirst in [false, true]) {
    test('Gmail授权失效保留Lyra会话（设备先过期：$expiredDeviceFirst）', () async {
      final store = MemoryCredentialStore();
      await store.write(_snapshot);
      var requests = 0;
      var refreshes = 0;
      var signedOut = false;
      final client = LyraApiClient(
        serverUrl: _snapshot.serverUrl,
        deviceId: _snapshot.deviceId,
        credentialStore: store,
        tokenRefresher: () async {
          refreshes++;
          return 'fresh-token';
        },
        onUnauthorized: () async => signedOut = true,
        dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
          ..httpClientAdapter = FakeLyraAdapter((options) async {
            requests++;
            if (expiredDeviceFirst && requests == 1) return _response(401);
            return _httpErrorResponse(401, 'GMAIL_AUTH_REVOKED');
          }),
      );
      await expectLater(
        client.post<Object?>('/v1/google/connections/connection/sync', {}),
        throwsA(
          isA<LyraApiError>().having(
            (error) => error.code,
            'Gmail grant failure',
            'GMAIL_AUTH_REVOKED',
          ),
        ),
      );
      expect(requests, expiredDeviceFirst ? 2 : 1);
      expect(refreshes, expiredDeviceFirst ? 1 : 0);
      expect(signedOut, isFalse);
      expect(await store.read(), isNotNull);
    });
  }

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

  for (final scenario in [
    (DioExceptionType.connectionError, 'CLIENT_CONNECTION_FAILED', true),
    (DioExceptionType.connectionTimeout, 'CLIENT_REQUEST_TIMEOUT', true),
    (DioExceptionType.sendTimeout, 'CLIENT_REQUEST_TIMEOUT', true),
    (DioExceptionType.receiveTimeout, 'CLIENT_REQUEST_TIMEOUT', true),
    (DioExceptionType.transformTimeout, 'CLIENT_REQUEST_TIMEOUT', true),
    (DioExceptionType.badCertificate, 'CLIENT_CERTIFICATE_INVALID', false),
    (DioExceptionType.cancel, 'CLIENT_REQUEST_CANCELLED', false),
    (DioExceptionType.unknown, 'CLIENT_REQUEST_FAILED', false),
    (DioExceptionType.badResponse, 'CLIENT_PROTOCOL_ERROR', false),
  ]) {
    test('${scenario.$1.name} 映射为 ${scenario.$2}', () async {
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
            throw DioException(requestOptions: options, type: scenario.$1);
          }),
      );

      await expectLater(
        client.post<Object?>('/v1/ledger/import', {'rows': <Object?>[]}),
        throwsA(
          isA<LyraApiError>()
              .having((e) => e.code, 'code', scenario.$2)
              .having((e) => e.statusCode, 'status', 0)
              .having((e) => e.retryable, 'retryable', scenario.$3),
        ),
      );
      // 普通网络失败不会自动重放写请求。
      expect(requests, 1);
    });
  }

  test('HTTP 错误保留服务端码、requestId、details 与重试标记', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async => null,
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((_) async {
          return ResponseBody.fromString(
            '{"code":"IMPORT_VALIDATION_FAILED",'
            '"message":"invalid row",'
            '"request_id":"body-request-id",'
            '"retryable":true,"details":{"row":7}}',
            422,
            headers: {
              Headers.contentTypeHeader: [Headers.jsonContentType],
              'x-request-id': ['header-request-id'],
            },
          );
        }),
    );

    await expectLater(
      client.post<Object?>('/v1/ledger/import', {'rows': <Object?>[]}),
      throwsA(
        isA<LyraApiError>()
            .having((e) => e.code, 'code', 'IMPORT_VALIDATION_FAILED')
            .having((e) => e.statusCode, 'status', 422)
            .having((e) => e.requestId, 'requestId', 'header-request-id')
            .having((e) => e.retryable, 'retryable', isTrue)
            .having((e) => e.details?['row'], 'details.row', 7),
      ),
    );
  });

  test('401 后仅重放一次；重放时网络失败不会再次自动重放写请求', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requests = 0;
    var refreshes = 0;
    final authorizations = <Object?>[];
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async {
        refreshes++;
        return 'fresh-token';
      },
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests++;
          authorizations.add(options.headers['authorization']);
          if (requests == 1) return _response(401);
          throw DioException(
            requestOptions: options,
            type: DioExceptionType.connectionError,
          );
        }),
    );

    await expectLater(
      client.post<Object?>('/v1/ledger/import', {'rows': <Object?>[]}),
      throwsA(
        isA<LyraApiError>()
            .having((e) => e.code, 'code', 'CLIENT_CONNECTION_FAILED')
            .having((e) => e.retryable, 'retryable', isTrue),
      ),
    );
    expect(refreshes, 1);
    expect(requests, 2);
    expect(authorizations, ['Bearer expired-token', 'Bearer fresh-token']);
  });

  for (final type in [
    DioExceptionType.connectionError,
    DioExceptionType.connectionTimeout,
    DioExceptionType.sendTimeout,
    DioExceptionType.receiveTimeout,
  ]) {
    test('GET 的 ${type.name} 最多恢复一次并保留 query/cursor/scope', () async {
      final store = MemoryCredentialStore();
      await store.write(_snapshot);
      final originalQuery = <String, String>{
        'cursor': 'cursor-secret',
        'scope': 'account-scope',
        'limit': '50',
      };
      final queries = <Map<String, dynamic>>[];
      var requests = 0;
      final client = LyraApiClient(
        serverUrl: _snapshot.serverUrl,
        deviceId: _snapshot.deviceId,
        credentialStore: store,
        tokenRefresher: () async => null,
        dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
          ..httpClientAdapter = FakeLyraAdapter((options) async {
            requests++;
            queries.add(Map<String, dynamic>.from(options.queryParameters));
            if (requests == 1) {
              throw DioException(requestOptions: options, type: type);
            }
            return _response(200);
          }),
      );

      final response = client.get<Object?>('/v1/ledger/entries', originalQuery);
      originalQuery['cursor'] = 'mutated-after-call';
      expect((await response).statusCode, 200);
      expect(requests, 2);
      expect(queries, hasLength(2));
      for (final query in queries) {
        expect(query, {
          'cursor': 'cursor-secret',
          'scope': 'account-scope',
          'limit': '50',
        });
      }
    });
  }

  test('GET 连续传输失败最多请求两次并抛出第二次的真实分类', () async {
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
          throw DioException(
            requestOptions: options,
            type: requests == 1
                ? DioExceptionType.connectionError
                : DioExceptionType.receiveTimeout,
          );
        }),
    );

    await expectLater(
      client.get<Object?>('/v1/ledger/entries', {}),
      throwsA(
        isA<LyraApiError>()
            .having((e) => e.code, 'code', 'CLIENT_REQUEST_TIMEOUT')
            .having((e) => e.retryable, 'retryable', isTrue),
      ),
    );
    expect(requests, 2);
  });

  for (final type in [
    DioExceptionType.transformTimeout,
    DioExceptionType.badCertificate,
    DioExceptionType.cancel,
    DioExceptionType.unknown,
  ]) {
    test('GET ${type.name} 不自动恢复', () async {
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
            throw DioException(requestOptions: options, type: type);
          }),
      );

      await expectLater(
        client.get<Object?>('/v1/ledger/entries', {}),
        throwsA(isA<LyraApiError>()),
      );
      expect(requests, 1);
    });
  }

  for (final scenario in [
    (503, 'SERVICE_UNAVAILABLE'),
    (429, 'RATE_LIMITED'),
    (400, 'INVALID_CURSOR'),
  ]) {
    test('HTTP ${scenario.$1} ${scenario.$2} 不自动恢复', () async {
      final store = MemoryCredentialStore();
      await store.write(_snapshot);
      var requests = 0;
      final client = LyraApiClient(
        serverUrl: _snapshot.serverUrl,
        deviceId: _snapshot.deviceId,
        credentialStore: store,
        tokenRefresher: () async => null,
        dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
          ..httpClientAdapter = FakeLyraAdapter((_) async {
            requests++;
            return _httpErrorResponse(scenario.$1, scenario.$2);
          }),
      );

      await expectLater(
        client.get<Object?>('/v1/ledger/entries', {'cursor': 'opaque'}),
        throwsA(
          isA<LyraApiError>()
              .having((e) => e.code, 'code', scenario.$2)
              .having((e) => e.statusCode, 'status', scenario.$1),
        ),
      );
      expect(requests, 1);
    });
  }

  test('401 后的 GET 可用一次共享传输恢复预算', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requests = 0;
    var refreshes = 0;
    final authorizations = <Object?>[];
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async {
        refreshes++;
        return 'fresh-token';
      },
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests++;
          authorizations.add(options.headers['authorization']);
          if (requests == 1) return _response(401);
          if (requests == 2) {
            throw DioException(
              requestOptions: options,
              type: DioExceptionType.connectionError,
            );
          }
          return _response(200);
        }),
    );

    expect(
      (await client.get<Object?>('/v1/ledger/entries', {})).statusCode,
      200,
    );
    expect(requests, 3);
    expect(refreshes, 1);
    expect(authorizations, [
      'Bearer expired-token',
      'Bearer fresh-token',
      'Bearer fresh-token',
    ]);
  });

  test('401认证重放与初次GET共享传输恢复预算且只刷新一次', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requests = 0;
    var refreshes = 0;
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async {
        refreshes++;
        return 'fresh-token';
      },
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests++;
          if (requests == 1) {
            throw DioException(
              requestOptions: options,
              type: DioExceptionType.connectionError,
            );
          }
          if (requests == 2) return _response(401);
          throw DioException(
            requestOptions: options,
            type: DioExceptionType.receiveTimeout,
          );
        }),
    );

    await expectLater(
      client.get<Object?>('/v1/ledger/entries', {}),
      throwsA(
        isA<LyraApiError>()
            .having((e) => e.code, 'code', 'CLIENT_REQUEST_TIMEOUT')
            .having((e) => e.retryable, 'retryable', isTrue),
      ),
    );
    expect(requests, 3);
    expect(refreshes, 1);
  });

  test('GET 的认证重放再次遇到401时不重复刷新或重放', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var requests = 0;
    var refreshes = 0;
    var unauthorized = 0;
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async {
        refreshes++;
        return 'fresh-token';
      },
      onUnauthorized: () async => unauthorized++,
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((_) async {
          requests++;
          return _response(401);
        }),
    );

    await expectLater(
      client.get<Object?>('/v1/ledger/entries', {}),
      throwsA(isA<LyraApiError>().having((e) => e.statusCode, 'status', 401)),
    );
    expect(requests, 2);
    expect(refreshes, 1);
    expect(unauthorized, 1);
  });

  test('GET 重试等待期间退出登录后不再发送请求', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final failed = Completer<void>();
    var requests = 0;
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async => null,
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests++;
          failed.complete();
          throw DioException(
            requestOptions: options,
            type: DioExceptionType.connectionError,
          );
        }),
    );
    var completed = false;
    final request = client
        .get<Object?>('/v1/ledger/entries', {})
        .whenComplete(() => completed = true);
    await failed.future;
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(completed, isFalse);
    client.invalidate();

    await expectLater(
      request,
      throwsA(
        isA<LyraApiError>().having(
          (e) => e.code,
          'code',
          'AUTH_SESSION_CHANGED',
        ),
      ),
    );
    expect(requests, 1);
  });

  test('失败诊断日志不包含授权头、查询参数或响应正文', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final logs = <String?>[];
    final previousDebugPrint = debugPrint;
    debugPrint = (message, {wrapWidth}) => logs.add(message);
    addTearDown(() => debugPrint = previousDebugPrint);
    final client = LyraApiClient(
      serverUrl: _snapshot.serverUrl,
      deviceId: _snapshot.deviceId,
      credentialStore: store,
      tokenRefresher: () async => null,
      dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
        ..httpClientAdapter = FakeLyraAdapter(
          (_) async => _httpErrorResponse(
            503,
            'SERVICE_UNAVAILABLE',
            message: 'body-secret',
          ),
        ),
    );

    await expectLater(
      client.get<Object?>('/v1/ledger/entries', {
        'cursor': 'cursor-secret',
        'scope': 'account-secret',
      }),
      throwsA(isA<LyraApiError>()),
    );
    final diagnostic = logs.join('\n');
    expect(diagnostic, contains('LYRA_HTTP_ERROR GET /v1/ledger/entries'));
    expect(diagnostic, contains('status=503 type=badResponse cause=Null'));
    expect(diagnostic, contains('requestId=null'));
    for (final secret in [
      'Bearer expired-token',
      'cursor-secret',
      'account-secret',
      'body-secret',
    ]) {
      expect(diagnostic, isNot(contains(secret)));
    }
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

ResponseBody _httpErrorResponse(
  int status,
  String code, {
  String message = 'failed',
}) => ResponseBody.fromString(
  '{"code":"$code","message":"$message","retryable":false}',
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);
