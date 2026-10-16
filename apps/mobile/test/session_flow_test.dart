import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';
import 'package:lyra_mobile/features/auth/auth_workspace.dart';

import 'support/faulty_stores.dart';

/// 按请求路径返回固定响应的假 HTTP 适配器；不触达真实网络。
class FakeLyraAdapter implements HttpClientAdapter {
  FakeLyraAdapter(this.handler);

  final Future<ResponseBody> Function(RequestOptions options) handler;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    return handler(options);
  }

  @override
  void close({bool force = false}) {}
}

const String _loginResponse = '''
{
  "status": "complete",
  "user": {
    "userId": "user-1",
    "displayName": "Owner",
    "status": "active",
    "role": "member",
    "identity": {"provider":"google", "subject":"test-user", "email":"owner@example.com"}
  },
  "deviceId": "d1000000-0000-4000-8000-000000000001",
  "displayName": "Test phone",
  "accessToken": "access-token-1",
  "accessTokenExpiresAt": "2026-09-02T10:15:00.000Z",
  "refreshToken": "refresh-token-1",
  "refreshTokenExpiresAt": "2026-10-02T10:00:00.000Z"
}
''';

const String _summaryResponse = '''
{
  "metadata": {
    "accountScope": {"kind": "all"},
    "accountIds": ["a2000000-0000-4000-8000-000000000001"],
    "currencyGroups": [{"currency": "USD", "accountIds": ["a2000000-0000-4000-8000-000000000001"]}],
    "aggregateCurrency": "USD",
    "ledgerVersion": 3,
    "asOf": "2026-09-02T10:00:00.000Z",
    "fetchedAt": "2026-09-02T10:00:00.000Z"
  },
  "data": {
    "aggregateCurrency": "USD",
    "groups": [{
      "currency": "USD",
      "accountIds": ["a2000000-0000-4000-8000-000000000001"],
      "data": {
        "netDeposits": "1000.00",
        "realizedPnlTotal": "12.34",
        "realizedPnlStock": "10.00",
        "realizedPnlOption": "2.34",
        "totalFees": "1.00",
        "dividends": "0.65",
        "interest": "0.10",
        "taxes": "0",
        "cashBalance": "500.00",
        "openPositions": 1,
        "transactionCount": 3
      }
    }]
  }
}
''';

/// 反复向下拖拽总览滚动视图直到目标构建并滚入可视区；sliver 懒构建需要触发。
Future<void> scrollUntilVisible(WidgetTester tester, Finder target) async {
  for (int attempt = 0; attempt < 15; attempt++) {
    if (tester.any(target)) {
      await tester.ensureVisible(target);
      await tester.pumpAndSettle();
      return;
    }
    await tester.drag(find.byType(CustomScrollView), const Offset(0, -260));
    await tester.pumpAndSettle();
  }
  throw StateError('滚动后仍未找到目标组件');
}

void main() {
  testWidgets('Google 浏览器授权成功后展示真实账本汇总并保存安全凭据', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final store = MemoryCredentialStore();
    String? codeChallenge;
    var exchangeCount = 0;
    final openedUrls = <Uri>[];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          configuredServerUrlProvider.overrideWithValue(
            'http://127.0.0.1:3141',
          ),
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              agentSessionStore: MemoryAgentSessionStore(),
              openAuthorizationUrl: (url) async {
                openedUrls.add(url);
                return true;
              },
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = FakeLyraAdapter((
                    RequestOptions options,
                  ) async {
                    final path = options.uri.path;
                    expect(options.headers['x-lyra-protocol-version'], '3');
                    if (path == '/v1/auth/providers') {
                      return _response(
                        jsonEncode({
                          'providers': [
                            {
                              'id': 'google',
                              'name': 'Google',
                              'available': true,
                            },
                            {
                              'id': 'linuxdo',
                              'name': 'Linux.do',
                              'available': false,
                            },
                            {
                              'id': 'github',
                              'name': 'GitHub',
                              'available': false,
                            },
                          ],
                        }),
                        200,
                      );
                    }
                    if (path == '/v1/auth/google/start') {
                      final body = _requestBody(options);
                      codeChallenge = body['codeChallenge'] as String;
                      expect(
                        codeChallenge,
                        matches(RegExp(r'^[A-Za-z0-9_-]{43}$')),
                      );
                      expect(body['codeChallengeMethod'], 'S256');
                      expect(
                        body['publicKeyPem'],
                        startsWith('-----BEGIN PUBLIC KEY-----'),
                      );
                      expect(body.containsKey('codeVerifier'), isFalse);
                      return _response(_startResponse(), 201);
                    }
                    if (path == '/v1/auth/google/exchange') {
                      final body = _requestBody(options);
                      final verifier = body['codeVerifier'] as String;
                      expect(verifier, matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
                      final hash = await Sha256().hash(utf8.encode(verifier));
                      expect(
                        base64UrlEncode(hash.bytes).replaceAll('=', ''),
                        codeChallenge,
                      );
                      expect(options.uri.query, isEmpty);
                      exchangeCount++;
                      if (exchangeCount == 1) {
                        return _response('{"status":"pending"}', 202);
                      }
                      return _response(_loginResponse, 200);
                    }
                    if (path == '/v1/ledger/summary') {
                      return ResponseBody.fromString(
                        _summaryResponse,
                        200,
                        headers: <String, List<String>>{
                          Headers.contentTypeHeader: <String>[
                            Headers.jsonContentType,
                          ],
                          'x-request-id': <String>['req-summary'],
                        },
                      );
                    }
                    if (path == '/v1/ledger/accounts') {
                      return _response(
                        jsonEncode({
                          'data': {
                            'rows': [
                              {
                                'id': 'a2000000-0000-4000-8000-000000000001',
                                'displayName': 'Schwab 主账户',
                                'maskedSuffix': '276',
                                'baseCurrency': 'USD',
                                'status': 'active',
                              },
                            ],
                          },
                        }),
                        200,
                      );
                    }
                    return ResponseBody.fromString('unexpected', 404);
                  });
              },
            ),
          ),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();

    // 独立登录页展示三种方式；服务端目录决定哪些按钮可用。
    await scrollUntilVisible(tester, find.text('使用 Google 登录'));
    expect(find.byKey(const ValueKey('login-screen')), findsOneWidget);

    expect(find.widgetWithText(TextField, '服务端地址'), findsNothing);
    expect(find.widgetWithText(TextField, '配对码'), findsNothing);
    await scrollUntilVisible(tester, find.text('使用 Google 登录'));
    await tester.tap(find.text('使用 Google 登录'));
    await tester.pumpAndSettle();
    // 等待提示本身不使用无限动画；明确推进服务端约定的轮询间隔。
    await tester.pump(const Duration(seconds: 2));
    await tester.pumpAndSettle();

    // 已登录状态展示真实汇总：十进制字符串原样展示，不伪造。先滚回顶部。
    await tester.drag(find.byType(CustomScrollView), const Offset(0, 800));
    await tester.pumpAndSettle();
    expect(find.text('全部账号'), findsOneWidget);
    expect(
      find.byKey(const ValueKey<String>('overview-hero-value')),
      findsOneWidget,
    );
    expect(find.text('+12.34'), findsOneWidget);
    expect(find.text('1,000.00'), findsOneWidget);
    expect(find.text('500.00'), findsOneWidget);
    expect(find.text('数据截至 2026年9月2日 10:00:00 UTC'), findsOneWidget);
    expect(find.textContaining('账本 v'), findsNothing);

    // 凭据已进入安全存储抽象。
    final stored = await store.read();
    expect(stored, isNotNull);
    expect(stored?.deviceId, 'd1000000-0000-4000-8000-000000000001');
    expect(stored?.userId, 'user-1');
    expect(stored?.authMethod, 'google');
    expect(stored?.userEmail, 'owner@example.com');
    expect(stored?.googleSubject, 'test-user');
    expect(stored?.devicePrivateKey, isNotEmpty);
    expect(openedUrls.single.host, 'accounts.google.com');
    expect(exchangeCount, 2);
  });
  test('取消授权停止轮询且不保存任何凭据', () async {
    final store = MemoryCredentialStore();
    final exchanged = Completer<void>();
    var exchangeCount = 0;
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path.endsWith('/start')) {
        return _response(_startResponse(), 201);
      }
      exchangeCount++;
      if (!exchanged.isCompleted) exchanged.complete();
      return _response('{"status":"pending"}', 202);
    });
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    final controller = container.read(sessionProvider.notifier);
    final login = controller.signInWithGoogle(
      serverUrl: 'https://lyra.example.com',
      displayName: 'Test',
    );
    await exchanged.future;
    await pumpEventQueue();
    controller.cancelSignIn();
    expect(await login, isFalse);
    await pumpEventQueue();
    expect(exchangeCount, 1);
    expect(await store.read(), isNull);
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
  });

  test('授权被拒绝返回稳定错误且保持未登录', () async {
    final store = MemoryCredentialStore();
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path.endsWith('/start')) {
        return _response(_startResponse(), 201);
      }
      return _response(
        '{"code":"LOGIN_DENIED","message":"该账号未获授权","retryable":false}',
        403,
      );
    });
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    await expectLater(
      container
          .read(sessionProvider.notifier)
          .signInWithGoogle(
            serverUrl: 'https://lyra.example.com',
            displayName: 'Test',
          ),
      throwsA(
        isA<LyraApiError>().having((e) => e.code, 'code', 'LOGIN_DENIED'),
      ),
    );
    expect(await store.read(), isNull);
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
  });

  test('服务端授权事务过期时停止登录，且不开始轮询', () async {
    final store = MemoryCredentialStore();
    var exchanges = 0;
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path.endsWith('/start')) {
        final body = jsonDecode(_startResponse()) as Map<String, Object?>;
        body['expiresAt'] = DateTime.now()
            .subtract(const Duration(minutes: 1))
            .toUtc()
            .toIso8601String();
        return _response(jsonEncode(body), 201);
      }
      exchanges++;
      return _response('{"status":"pending"}', 202);
    });
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    await expectLater(
      container
          .read(sessionProvider.notifier)
          .signInWithGoogle(
            serverUrl: 'https://lyra.example.com',
            displayName: 'Test',
          ),
      throwsA(
        isA<LyraApiError>().having((e) => e.code, 'code', 'LOGIN_EXPIRED'),
      ),
    );
    expect(exchanges, 0);
    expect(await store.read(), isNull);
  });

  test('旧配对凭据在本机失效，清除旧 AI 会话并要求 OAuth 登录', () async {
    final store = MemoryCredentialStore();
    await store.write(
      const CredentialSnapshot(
        serverUrl: 'https://lyra.example.com',
        deviceId: 'old-device',
        accessToken: 'old-access',
        refreshToken: 'old-refresh',
      ),
    );
    final agentStore = MemoryAgentSessionStore();
    await agentStore.write('old-session');
    final container = _sessionContainer(
      store,
      (_) async => throw StateError('旧凭据不能联网'),
      agentStore: agentStore,
    );
    addTearDown(container.dispose);
    expect(
      await container.read(sessionProvider.future),
      isA<SessionSignedOut>(),
    );
    expect(await store.read(), isNull);
    expect(await agentStore.read(), isNull);
  });

  test('旧凭据缺少稳定 userId 时清理会话并要求重新登录', () async {
    final store = MemoryCredentialStore();
    await store.write(
      const CredentialSnapshot(
        serverUrl: 'https://lyra.example.com',
        deviceId: 'legacy-device',
        accessToken: 'legacy-access',
        refreshToken: 'legacy-refresh',
        authMethod: 'google',
        googleSubject: 'test-user',
        userEmail: 'owner@example.com',
      ),
    );
    final agentStore = MemoryAgentSessionStore();
    await agentStore.write('legacy-session');
    final container = _sessionContainer(
      store,
      (_) async => throw StateError('缺少 userId 的凭据不能联网'),
      agentStore: agentStore,
    );
    addTearDown(container.dispose);
    final state = await container.read(sessionProvider.future);
    expect(state, isA<SessionSignedOut>());
    // 不按邮箱或 subject 猜归属：明确提示并要求重新登录，凭据与 Agent 会话一起清理。
    expect((state as SessionSignedOut).message, '登录凭据已更新，请重新登录。');
    expect(await store.read(), isNull);
    expect(await agentStore.read(), isNull);
  });

  test('账号被停用直接结束会话，不进入刷新重试循环', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final agentStore = MemoryAgentSessionStore();
    await agentStore.write('disabled-session');
    var refreshes = 0;
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path == '/v1/devices/tokens') {
        refreshes++;
        return _response(
          '{"code":"USER_DISABLED","message":"该账号已停用","retryable":false}',
          403,
        );
      }
      return _response(
        '{"code":"USER_DISABLED","message":"该账号已停用","retryable":false}',
        403,
      );
    }, agentStore: agentStore);
    addTearDown(container.dispose);
    final state = await container.read(sessionProvider.future);
    expect(state, isA<SessionSignedOut>());
    expect((state as SessionSignedOut).message, '账号已停用，请联系管理员。');
    expect(container.read(sessionProvider.notifier).client, isNull);
    expect(refreshes, 0);
    expect(await store.read(), isNull);
    expect(await agentStore.read(), isNull);
  });

  test('刷新阶段发现停用同样结束会话，不反复刷新', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    var refreshes = 0;
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        return _response(_summaryResponse, 200);
      }
      if (options.uri.path == '/v1/me') return _response(_meResponse(), 200);
      if (options.uri.path == '/v1/devices/tokens') {
        refreshes++;
        return _response(
          '{"code":"USER_DISABLED","message":"该账号已停用","retryable":false}',
          403,
        );
      }
      return _response('{"code":"DEVICE_AUTH_INVALID","message":"expired"}', 401);
    });
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    final controller = container.read(sessionProvider.notifier);
    await expectLater(
      controller.client!.get<Object?>('/private', {}),
      throwsA(
        isA<LyraApiError>().having((e) => e.code, 'code', 'USER_DISABLED'),
      ),
    );
    await pumpEventQueue();
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
    expect((container.read(sessionProvider).value as SessionSignedOut).message,
        '账号已停用，请联系管理员。');
    expect(refreshes, 1);
    expect(await store.read(), isNull);
  });

  test('/me 返回其他 userId 时结束会话并清理凭据', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        return _response(_summaryResponse, 200);
      }
      if (options.uri.path == '/v1/me') {
        return _response(
          jsonEncode({
            'user': {
              'userId': 'another-user',
              'displayName': 'Other',
              'status': 'active',
              'role': 'member',
              'identity': {
                'provider': 'google',
                'subject': 'test-user',
                'email': 'owner@example.com',
              },
            },
          }),
          200,
        );
      }
      return _response('unexpected', 404);
    });
    addTearDown(container.dispose);
    final state = await container.read(sessionProvider.future);
    expect(state, isA<SessionSignedOut>());
    expect((state as SessionSignedOut).message, '登录身份已变化，请重新登录。');
    expect(await store.read(), isNull);
  });

  test('/me 核对通过时保留会话，网络失败不伪装成停用或身份不符', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final verified = _sessionContainer(store, (options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        return _response(_summaryResponse, 200);
      }
      if (options.uri.path == '/v1/me') {
        return _response(
          jsonEncode({
            'user': {
              'userId': 'test-user-id',
              'displayName': 'Owner',
              'status': 'active',
              'role': 'member',
              'identity': {
                'provider': 'google',
                'subject': 'test-user',
                'email': 'owner@example.com',
              },
            },
          }),
          200,
        );
      }
      return _response('unexpected', 404);
    });
    addTearDown(verified.dispose);
    final state = await verified.read(sessionProvider.future);
    expect(state, isA<SessionAuthenticated>());
    expect((state as SessionAuthenticated).snapshot.userId, 'test-user-id');
    expect((await store.read())?.userId, 'test-user-id');

    final offlineStore = MemoryCredentialStore();
    await offlineStore.write(_authenticatedSnapshot);
    final offline = _sessionContainer(offlineStore, (options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        return _response(_summaryResponse, 200);
      }
      if (options.uri.path == '/v1/me') {
        throw DioException(
          requestOptions: options,
          type: DioExceptionType.connectionTimeout,
        );
      }
      return _response('unexpected', 404);
    });
    addTearDown(offline.dispose);
    final offlineState = await offline.read(sessionProvider.future);
    expect(offlineState, isA<SessionAuthenticated>());
    expect((await offlineStore.read())?.userId, 'test-user-id');
  });

  test('A 切换到 B 后，A 的迟到账本响应不能恢复旧会话', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final agentStore = MemoryAgentSessionStore();
    await agentStore.write('session-a');
    final lateSummary = Completer<ResponseBody>();
    var summaries = 0;
    final container = _sessionContainer(store, (options) async {
      final path = options.uri.path;
      if (path == '/v1/ledger/summary') {
        summaries++;
        if (summaries == 1) return _response(_summaryResponse, 200);
        return lateSummary.future;
      }
      if (path == '/v1/auth/google/start') {
        return _response(_startResponse(), 201);
      }
      if (path == '/v1/auth/google/exchange') {
        final body = jsonDecode(_loginResponse) as Map<String, Object?>;
        (body['user']! as Map<String, Object?>)['userId'] = 'user-b';
        body['deviceId'] = 'device-b';
        body['accessToken'] = 'access-b';
        body['refreshToken'] = 'refresh-b';
        return _response(jsonEncode(body), 200);
      }
      return _response('unexpected', 404);
    }, agentStore: agentStore);
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    final controller = container.read(sessionProvider.notifier);
    final late = controller.refreshLedger();
    await pumpEventQueue();
    // 旧会话的读请求仍在飞时切换到 B：旧 client 失效、凭据与 Agent 会话被替换。
    final switched = await controller.signIn(
      provider: AuthProvider.google,
      serverUrl: 'https://lyra.example.com',
      displayName: 'Second',
    );
    expect(switched, isTrue);
    expect((await store.read())?.userId, 'user-b');
    expect((await store.read())?.deviceId, 'device-b');
    // Agent 活跃会话属于上一个登录：切换时必须清理，不能带给 B。
    expect(await agentStore.read(), isNull);
    lateSummary.complete(_response(_summaryResponse, 200));
    expect(await late, isFalse);
    final state = container.read(sessionProvider).value;
    expect(state, isA<SessionAuthenticated>());
    expect((state as SessionAuthenticated).snapshot.userId, 'user-b');
    expect((state).summary, isNull);
  });

  test('刷新令牌被撤销后回到登录页，网络中断不清除有效凭据', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final container = _sessionContainer(store, (options) async {
      return _response('{"code":"DEVICE_AUTH_INVALID","message":"请重新登录"}', 401);
    });
    addTearDown(container.dispose);
    expect(
      await container.read(sessionProvider.future),
      isA<SessionSignedOut>(),
    );
    expect(await store.read(), isNull);

    final offlineStore = MemoryCredentialStore();
    await offlineStore.write(_authenticatedSnapshot);
    final offline = _sessionContainer(
      offlineStore,
      (_) async => _response('unavailable', 503),
    );
    addTearDown(offline.dispose);
    expect(
      await offline.read(sessionProvider.future),
      isA<SessionAuthenticated>(),
    );
    expect(await offlineStore.read(), isNotNull);
  });

  test('恢复中的汇总迟到失败不能在退出后恢复旧登录', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final summaryStarted = Completer<void>();
    final summaryResponse = Completer<ResponseBody>();
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        summaryStarted.complete();
        return summaryResponse.future;
      }
      return ResponseBody.fromString('', 204);
    });
    addTearDown(container.dispose);
    final restoring = container.read(sessionProvider.future);
    await summaryStarted.future;
    await container.read(sessionProvider.notifier).signOut();
    summaryResponse.complete(_response('unavailable', 503));
    await restoring;
    await pumpEventQueue();
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
    expect(container.read(sessionProvider.notifier).client, isNull);
    expect(await store.read(), isNull);
  });

  test('退出等待已轮换的刷新结果撤销远端设备，但不写回迟到凭据', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    final refreshStarted = Completer<void>();
    final refreshResponse = Completer<ResponseBody>();
    var refreshCalls = 0;
    var remotelyRevoked = false;
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        return _response(_summaryResponse, 200);
      }
      if (options.uri.path == '/v1/me') return _response(_meResponse(), 200);
      if (options.uri.path == '/v1/devices/tokens') {
        refreshCalls++;
        if (refreshCalls == 1) {
          refreshStarted.complete();
          return refreshResponse.future;
        }
        return _response(
          '{"code":"DEVICE_CREDENTIAL_REUSED","message":"already rotated"}',
          401,
        );
      }
      if (options.uri.path == '/v1/auth/logout') {
        if (options.headers['authorization'] == 'Bearer late-access') {
          remotelyRevoked = true;
          return ResponseBody.fromString('', 204);
        }
        return _response(
          '{"code":"DEVICE_AUTH_INVALID","message":"expired"}',
          401,
        );
      }
      return _response(
        '{"code":"DEVICE_AUTH_INVALID","message":"expired"}',
        401,
      );
    });
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    final controller = container.read(sessionProvider.notifier);
    final pending = controller.client!.get<Object?>('/private', {});
    final failure = expectLater(pending, throwsA(isA<LyraApiError>()));
    await refreshStarted.future;
    final logout = controller.signOut();
    await pumpEventQueue();
    expect(await store.read(), isNull);
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
    expect(remotelyRevoked, isFalse);
    refreshResponse.complete(
      _response(
        jsonEncode({
          'accessToken': 'late-access',
          'refreshToken': 'late-refresh',
          'accessTokenExpiresAt': '2026-12-01T00:00:00Z',
          'refreshTokenExpiresAt': '2026-12-31T00:00:00Z',
        }),
        200,
      ),
    );
    await failure;
    await logout;
    expect(refreshCalls, 1);
    expect(remotelyRevoked, isTrue);
    expect(await store.read(), isNull);
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
  });

  test('取消中的旧凭据写入先回滚，新登录不会被旧操作清除', () async {
    final store = _DeferredCredentialStore();
    addTearDown(() {
      if (!store.releaseWrite.isCompleted) store.releaseWrite.complete();
    });
    final secondExchange = Completer<void>();
    var starts = 0;
    final container = _sessionContainer(store, (options) async {
      if (options.uri.path.endsWith('/start')) {
        final body = jsonDecode(_startResponse()) as Map<String, Object?>;
        body['loginId'] = 'login-${++starts}';
        return _response(jsonEncode(body), 201);
      }
      final body = jsonDecode(_loginResponse) as Map<String, Object?>;
      if (_requestBody(options)['loginId'] == 'login-2') {
        body['deviceId'] = 'new-device';
        body['accessToken'] = 'new-access';
        body['refreshToken'] = 'new-refresh';
        secondExchange.complete();
      }
      return _response(jsonEncode(body), 200);
    });
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    final controller = container.read(sessionProvider.notifier);
    final first = controller.signInWithGoogle(
      serverUrl: 'https://lyra.example.com',
      displayName: 'First',
    );
    await store.writeStarted.future;
    controller.cancelSignIn();
    final second = controller.signInWithGoogle(
      serverUrl: 'https://lyra.example.com',
      displayName: 'Second',
    );
    await secondExchange.future;
    store.releaseWrite.complete();
    expect(await first, isFalse);
    expect(await second, isTrue);
    final saved = await store.read();
    expect(saved?.deviceId, 'new-device');
    expect(saved?.accessToken, 'new-access');
    expect(saved?.refreshToken, 'new-refresh');
    expect(
      (container.read(sessionProvider).value as SessionAuthenticated)
          .snapshot
          .deviceId,
      'new-device',
    );
  });

  test('两次刷新使用最新 refreshToken，退出同时撤销服务端与本机凭据', () async {
    final store = MemoryCredentialStore();
    await store.write(_authenticatedSnapshot);
    var generation = 1;
    final refreshedWith = <String>[];
    var logoutCalled = false;
    final agentStore = MemoryAgentSessionStore();
    await agentStore.write('active-session');
    final container = _sessionContainer(store, (options) async {
      final path = options.uri.path;
      if (path == '/v1/ledger/summary') return _response(_summaryResponse, 200);
      if (path == '/v1/me') return _response(_meResponse(), 200);
      if (path == '/v1/devices/tokens') {
        refreshedWith.add(_requestBody(options)['refreshToken'] as String);
        generation++;
        return _response(
          jsonEncode({
            'accessToken': 'access-token-$generation',
            'refreshToken': 'refresh-token-$generation',
            'accessTokenExpiresAt': '2026-12-01T00:00:00Z',
            'refreshTokenExpiresAt': '2026-12-31T00:00:00Z',
          }),
          200,
        );
      }
      if (path == '/v1/auth/logout') {
        expect(options.headers['authorization'], 'Bearer access-token-3');
        logoutCalled = true;
        return ResponseBody.fromString('', 204);
      }
      final desired = path == '/first' ? 2 : 3;
      if (options.headers['authorization'] != 'Bearer access-token-$desired') {
        return _response(
          '{"code":"DEVICE_AUTH_INVALID","message":"expired"}',
          401,
        );
      }
      return _response('{}', 200);
    }, agentStore: agentStore);
    addTearDown(container.dispose);
    await container.read(sessionProvider.future);
    final controller = container.read(sessionProvider.notifier);
    await controller.client!.get<Object?>('/first', {});
    await controller.client!.get<Object?>('/second', {});
    expect(refreshedWith, ['refresh-token-1', 'refresh-token-2']);
    expect((await store.read())?.authMethod, 'google');
    expect((await store.read())?.googleSubject, 'test-user');
    await controller.signOut();
    expect(logoutCalled, isTrue);
    expect(await store.read(), isNull);
    expect(await agentStore.read(), isNull);
    expect(controller.client, isNull);
  });

  for (final oldFails in [false, true]) {
    test('汇总并发刷新仅发布最后请求，旧${oldFails ? '失败' : '结果'}不覆盖新版本', () async {
      final store = MemoryCredentialStore();
      await store.write(_authenticatedSnapshot);
      final older = Completer<ResponseBody>();
      final newer = Completer<ResponseBody>();
      var reads = 0;
      final container = _sessionContainer(store, (options) async {
        if (options.uri.path == '/v1/me') return _response(_meResponse(), 200);
        reads++;
        if (reads == 1) return _response(_summaryResponse, 200);
        return reads == 2 ? older.future : newer.future;
      });
      addTearDown(container.dispose);
      await container.read(sessionProvider.future);
      final controller = container.read(sessionProvider.notifier);
      final first = controller.refreshLedger();
      await pumpEventQueue();
      final second = controller.refreshLedger();
      await pumpEventQueue();
      newer.complete(
        _response(
          _summaryResponse.replaceFirst(
            '"ledgerVersion": 3',
            '"ledgerVersion": 5',
          ),
          200,
        ),
      );
      expect(await second, isTrue);
      older.complete(
        oldFails
            ? _response('unavailable', 503)
            : _response(
                _summaryResponse.replaceFirst(
                  '"ledgerVersion": 3',
                  '"ledgerVersion": 4',
                ),
                200,
              ),
      );
      expect(await first, isFalse);
      expect(
        (container.read(sessionProvider).value as SessionAuthenticated)
            .summary!
            .metadata
            .ledgerVersion,
        5,
      );
    });
  }

  testWidgets('安全存储不可读时降级为可操作的未登录状态而不是重试死循环', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final store = FaultyCredentialStore(readFails: true);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              agentSessionStore: MemoryAgentSessionStore(),
            ),
          ),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();

    // 读取失败不再留在 build 的 Future 里；用户看到可自助的登录入口与原因。
    expect(find.byKey(const ValueKey('login-screen')), findsOneWidget);
    await scrollUntilVisible(tester, find.text('暂时无法恢复登录，请重新登录。'));
    expect(find.text('暂时无法恢复登录，请重新登录。'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}

const _authenticatedSnapshot = CredentialSnapshot(
  serverUrl: 'https://lyra.example.com',
  deviceId: 'test-device',
  accessToken: 'access-token-1',
  refreshToken: 'refresh-token-1',
  userId: 'test-user-id',
  authMethod: 'google',
  googleSubject: 'test-user',
);

/// 身份核对响应；与 [_authenticatedSnapshot] 的稳定 userId 保持一致。
String _meResponse({String userId = 'test-user-id'}) => jsonEncode({
  'user': {
    'userId': userId,
    'displayName': 'Owner',
    'status': 'active',
    'role': 'member',
    'identity': {
      'provider': 'google',
      'subject': 'test-user',
      'email': 'owner@example.com',
    },
  },
});

String _startResponse() => jsonEncode({
  'loginId': 'test-login',
  'authorizationUrl':
      'https://accounts.google.com/o/oauth2/v2/auth?state=test-state',
  'expiresAt': DateTime.now()
      .add(const Duration(minutes: 10))
      .toUtc()
      .toIso8601String(),
  'pollIntervalSeconds': 2,
});

ResponseBody _response(String body, int status) => ResponseBody.fromString(
  body,
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);

Map<String, Object?> _requestBody(RequestOptions options) => options.data is Map
    ? (options.data as Map).cast<String, Object?>()
    : (jsonDecode(options.data as String) as Map).cast<String, Object?>();

ProviderContainer _sessionContainer(
  MemoryCredentialStore store,
  Future<ResponseBody> Function(RequestOptions) handler, {
  MemoryAgentSessionStore? agentStore,
}) {
  return ProviderContainer(
    overrides: [
      sessionDependenciesProvider.overrideWithValue(
        SessionDependencies(
          credentialStore: store,
          agentSessionStore: agentStore ?? MemoryAgentSessionStore(),
          openAuthorizationUrl: (_) async => true,
          clientFactory: (url) =>
              Dio(BaseOptions(baseUrl: url))
                ..httpClientAdapter = FakeLyraAdapter(handler),
        ),
      ),
    ],
  );
}

/// 故意阻塞首次安全存储写入，使用户取消与新登录发生在持久化 await 期间。
class _DeferredCredentialStore extends MemoryCredentialStore {
  final writeStarted = Completer<void>();
  final releaseWrite = Completer<void>();
  var writes = 0;

  @override
  Future<void> write(CredentialSnapshot snapshot) async {
    if (++writes == 1) {
      writeStarted.complete();
      await releaseWrite.future;
    }
    await super.write(snapshot);
  }
}
