import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/agent/agent_live_events.dart';
import 'package:lyra_mobile/features/agent/agent_repository.dart';
import 'package:lyra_mobile/features/agent/agent_screen.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';

import 'support/faulty_stores.dart';

/// 按请求路径返回固定响应的假 HTTP 适配器；不触达真实网络。
class FakeAgentAdapter implements HttpClientAdapter {
  FakeAgentAdapter(this.handler);

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

const String _availabilityResponse = '''
{
  "providers": [
    {"provider": "genkit", "status": "available"}
  ]
}
''';

String _sessionResponse({
  required String status,
  required int revision,
  required List<Map<String, Object?>> messages,
}) {
  return jsonEncode(<String, Object?>{
    'session': <String, Object?>{
      'id': 'a1000000-0000-4000-8000-000000000001',
      'provider': 'genkit',
      'status': status,
      'accountScope': <String, Object?>{'kind': 'all'},
      'ledgerVersion': 1,
      'asOf': '2026-09-05T10:00:00.000Z',
      'revision': revision,
      'messages': messages,
    },
  });
}

String _turnResponse() {
  return jsonEncode(<String, Object?>{
    'session': <String, Object?>{
      'id': 'a1000000-0000-4000-8000-000000000001',
      'provider': 'genkit',
      'status': 'idle',
      'accountScope': <String, Object?>{'kind': 'all'},
      'ledgerVersion': 1,
      'asOf': '2026-09-05T10:00:00.000Z',
      'revision': 3,
      'messages': <Map<String, Object?>>[
        <String, Object?>{
          'id': 'm1000000-0000-4000-8000-000000000001',
          'role': 'user',
          'content': '我的现金余额是多少？',
          'createdAt': '2026-09-05T10:00:01.000Z',
        },
        <String, Object?>{
          'id': 'm1000000-0000-4000-8000-000000000002',
          'role': 'assistant',
          'content': 'USD 现金余额为 12,345.67。',
          'createdAt': '2026-09-05T10:00:02.000Z',
        },
      ],
    },
    'events': <Map<String, Object?>>[
      <String, Object?>{
        'type': 'session.started',
        'sessionId': 'a1000000-0000-4000-8000-000000000001',
        'messageId': null,
        'sequence': 1,
        'occurredAt': '2026-09-05T10:00:01.000Z',
        'payload': <String, Object?>{},
      },
      <String, Object?>{
        'type': 'message.delta',
        'sessionId': 'a1000000-0000-4000-8000-000000000001',
        'messageId': 'm1000000-0000-4000-8000-000000000002',
        'sequence': 2,
        'occurredAt': '2026-09-05T10:00:02.000Z',
        'payload': <String, Object?>{'text': 'USD '},
      },
      <String, Object?>{
        'type': 'session.completed',
        'sessionId': 'a1000000-0000-4000-8000-000000000001',
        'messageId': null,
        'sequence': 3,
        'occurredAt': '2026-09-05T10:00:03.000Z',
        'payload': <String, Object?>{},
      },
    ],
  });
}

/// 会话工具调用公开列表：一条完成、一条失败。
String _toolCallsResponse() {
  return jsonEncode(<String, Object?>{
    'toolCalls': <Map<String, Object?>>[
      <String, Object?>{
        'id': 'c1000000-0000-4000-8000-000000000001',
        'messageId': 'm1000000-0000-4000-8000-000000000002',
        'ordinal': 1,
        'toolName': 'accounts',
        'status': 'completed',
        'argumentsSummary': <String, Object?>{'accountScope': 'all'},
        'resultSummary': <String, Object?>{'count': 3},
        'ledgerVersion': 1,
        'durationMs': 42,
        'errorCode': null,
        'startedAt': '2026-09-01T09:00:01.000Z',
        'completedAt': '2026-09-01T09:00:01.042Z',
      },
      <String, Object?>{
        'id': 'c1000000-0000-4000-8000-000000000002',
        'messageId': 'm1000000-0000-4000-8000-000000000002',
        'ordinal': 2,
        'toolName': 'summary',
        'status': 'failed',
        'argumentsSummary': <String, Object?>{},
        'resultSummary': null,
        'ledgerVersion': 1,
        'durationMs': 15,
        'errorCode': 'AGENT_TOOL_EXECUTION_FAILED',
        'startedAt': '2026-09-01T09:00:03.000Z',
        'completedAt': '2026-09-01T09:00:03.015Z',
      },
    ],
  });
}

const String _createdSessionJson = '''
{
  "session": {
    "id": "a1000000-0000-4000-8000-000000000001",
    "provider": "genkit",
    "status": "idle",
    "accountScope": {"kind": "all"},
    "ledgerVersion": 1,
    "asOf": "2026-09-05T10:00:00.000Z",
    "revision": 1,
    "messages": []
  }
}
''';

const String _summaryResponse = '''
{"metadata":{"accountScope":{"kind":"all"},"accountIds":[],"currencyGroups":[],
"aggregateCurrency":null,"ledgerVersion":1,"asOf":"2026-09-05T10:00:00.000Z",
"fetchedAt":"2026-09-05T10:00:00.000Z"},"data":{"aggregateCurrency":null,"groups":[]}}
''';

ResponseBody _jsonResponse(String body, int status) {
  return ResponseBody.fromString(
    body,
    status,
    headers: <String, List<String>>{
      Headers.contentTypeHeader: <String>[Headers.jsonContentType],
    },
  );
}

const CredentialSnapshot _snapshot = CredentialSnapshot(
  serverUrl: 'http://127.0.0.1:3141',
  deviceId: 'd1000000-0000-4000-8000-000000000001',
  accessToken: 'access-token-1',
  refreshToken: 'refresh-token-1',
  authMethod: 'google',
);

/// Dio 请求体可能是 JSON 字符串或已编码的 Map；统一解析为 JSON 对象。
Map<String, Object?> _decodeBody(RequestOptions options) {
  final data = options.data;
  if (data is Map) {
    return data.cast<String, Object?>();
  }
  if (data is String && data.isNotEmpty) {
    return jsonDecode(data) as Map<String, Object?>;
  }
  return const <String, Object?>{};
}

Future<ResponseBody> _routeAgentRequest(RequestOptions options) async {
  final path = options.uri.path;
  if (path == '/v1/agents') {
    return _jsonResponse(_availabilityResponse, 200);
  }
  if (path == '/v1/ledger/summary') {
    return _jsonResponse(_summaryResponse, 200);
  }
  if (path == '/v1/agent/sessions') {
    final body = _decodeBody(options);
    expect(body['provider'], 'genkit');
    expect((body['accountScope'] as Map<String, Object?>)['kind'], 'all');
    return _jsonResponse(_createdSessionJson, 201);
  }
  if (path == '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001') {
    return _jsonResponse(
      _sessionResponse(
        status: 'idle',
        revision: 2,
        messages: <Map<String, Object?>>[],
      ),
      200,
    );
  }
  if (path.endsWith('/messages')) {
    final body = _decodeBody(options);
    expect(body['content'], '我的现金余额是多少？');
    return _jsonResponse(_turnResponse(), 200);
  }
  if (path.endsWith('/tool-calls')) {
    return _jsonResponse(_toolCallsResponse(), 200);
  }
  if (path.endsWith('/cancel')) {
    return _jsonResponse(
      _sessionResponse(
        status: 'idle',
        revision: 4,
        messages: <Map<String, Object?>>[
          <String, Object?>{
            'id': 'm1000000-0000-4000-8000-000000000001',
            'role': 'user',
            'content': '我的现金余额是多少？',
            'createdAt': '2026-09-05T10:00:01.000Z',
          },
        ],
      ),
      200,
    );
  }
  return _jsonResponse('unexpected', 404);
}

void main() {
  test('AgentRepository 解析可用性、会话创建、回合与取消响应', () async {
    final requestedPaths = <String>[];
    final dio = Dio(BaseOptions(baseUrl: 'http://127.0.0.1:3141'))
      ..httpClientAdapter = FakeAgentAdapter((RequestOptions options) async {
        requestedPaths.add(options.uri.path);
        return _routeAgentRequest(options);
      });
    final credentials = MemoryCredentialStore();
    await credentials.write(_snapshot);
    final repository = AgentRepository(
      client: LyraApiClient(
        serverUrl: 'http://127.0.0.1:3141',
        deviceId: _snapshot.deviceId,
        credentialStore: credentials,
        tokenRefresher: () async => null,
        dio: dio,
      ),
    );

    final availability = await repository.availability();
    expect(availability.items, hasLength(1));
    expect(availability.items[0].provider, 'genkit');
    expect(availability.items[0].available, isTrue);

    final session = await repository.createSession(accountScopeKind: 'all');
    expect(session.id, 'a1000000-0000-4000-8000-000000000001');
    expect(session.status, 'idle');
    expect(session.accountScopeKind, 'all');
    expect(session.messages, isEmpty);

    final restored = await repository.getSession(session.id);
    expect(restored.id, session.id);

    final turn = await repository.sendTurn(
      sessionId: session.id,
      content: '我的现金余额是多少？',
    );
    expect(turn.session.revision, 3);
    expect(turn.session.messages, hasLength(2));
    expect(turn.session.messages.last.role, 'assistant');
    expect(turn.events.map((event) => event.sequence).toList(), <int>[1, 2, 3]);
    expect(turn.events[1].type, 'message.delta');
    expect(turn.events[1].payload['text'], 'USD ');

    final cancelled = await repository.cancelTurn(session.id);
    expect(cancelled.status, 'idle');
    expect(cancelled.revision, 4);

    expect(requestedPaths, <String>[
      '/v1/agents',
      '/v1/agent/sessions',
      '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001',
      '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001/messages',
      '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001/cancel',
    ]);
  });

  testWidgets('Agent 页完成会话创建与回合问答', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final store = MemoryCredentialStore();
    await store.write(_snapshot);

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              agentSessionStore: MemoryAgentSessionStore(),
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = FakeAgentAdapter(_routeAgentRequest);
              },
            ),
          ),
        ],
        child: const MaterialApp(home: Scaffold(body: AgentScreen())),
      ),
    );
    await tester.pumpAndSettle();

    // 只有一个助手入口，不提供运行引擎切换。
    expect(find.text('开始分析'), findsOneWidget);
    expect(find.text('开始 Pi 会话'), findsNothing);
    expect(find.text('开始 OpenCode 会话'), findsNothing);
    await tester.tap(find.text('开始分析'));
    await tester.pumpAndSettle();

    // 会话视图出现，发送回合。
    expect(find.text('Lyra 助手'), findsOneWidget);
    expect(find.text('空闲'), findsOneWidget);
    await tester.enterText(find.byType(TextField), '我的现金余额是多少？');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pumpAndSettle();

    // 终态快照渲染用户与助手消息。
    expect(find.text('我的现金余额是多少？'), findsOneWidget);
    expect(find.text('USD 现金余额为 12,345.67。'), findsOneWidget);

    // 回合结束后拉取并渲染工具调用公开列表。
    await tester.pumpAndSettle();
    expect(find.text('工具调用'), findsOneWidget);
    expect(find.text('accounts'), findsOneWidget);
    expect(find.text('完成 · 42ms'), findsOneWidget);
    expect(find.text('summary'), findsOneWidget);
    expect(find.text('AGENT_TOOL_EXECUTION_FAILED'), findsOneWidget);
  });

  testWidgets('新对话使用页面账户范围，切换页面不会修改已有会话', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final scopes = <Map<String, Object?>>[];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              agentSessionStore: MemoryAgentSessionStore(),
              clientFactory: (serverUrl) => Dio(BaseOptions(baseUrl: serverUrl))
                ..httpClientAdapter = FakeAgentAdapter((options) async {
                  if (options.uri.path == '/v1/agent/sessions') {
                    final body = _decodeBody(options);
                    expect(body['provider'], 'genkit');
                    final scope = (body['accountScope'] as Map)
                        .cast<String, Object?>();
                    scopes.add(scope);
                    final response =
                        jsonDecode(_createdSessionJson) as Map<String, dynamic>;
                    response['session']['accountScope'] = scope;
                    return _jsonResponse(jsonEncode(response), 201);
                  }
                  return _routeAgentRequest(options);
                }),
            ),
          ),
        ],
        child: const MaterialApp(home: Scaffold(body: AgentScreen())),
      ),
    );
    await tester.pumpAndSettle();
    final container = ProviderScope.containerOf(
      tester.element(find.byType(AgentScreen)),
    );
    container.read(accountScopeProvider.notifier).select('account-1', '嘉信账号');
    await tester.pumpAndSettle();
    expect(find.text('分析范围：嘉信账号'), findsOneWidget);
    await tester.tap(find.text('开始分析'));
    await tester.pumpAndSettle();
    expect(scopes, [
      <String, Object?>{'kind': 'account', 'account': 'account-1'},
    ]);
    expect(find.text('本次对话：嘉信账号'), findsOneWidget);
    expect(find.byTooltip('切换 Agent'), findsNothing);

    container.read(accountScopeProvider.notifier).select('all', '全部账号');
    await tester.pumpAndSettle();
    expect(scopes, hasLength(1));
    expect(find.text('本次对话：嘉信账号'), findsOneWidget);
    expect(find.textContaining('新对话将使用该范围'), findsOneWidget);
    await tester.tap(find.byTooltip('新对话'));
    await tester.pumpAndSettle();
    expect(scopes.last, <String, Object?>{'kind': 'all'});
    expect(find.text('本次对话：全部账号'), findsOneWidget);
  });

  testWidgets('未登录时 Agent 页不提供会话入口', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: MemoryCredentialStore(),
              agentSessionStore: MemoryAgentSessionStore(),
            ),
          ),
        ],
        child: const MaterialApp(home: Scaffold(body: AgentScreen())),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('未连接服务端'), findsOneWidget);
    expect(find.text('开始分析'), findsNothing);
  });

  /// 构建已完成登录恢复的 Agent 控制器容器；调用方负责 dispose（已挂 tearDown）。
  Future<({ProviderContainer container, AgentController controller})> harness(
    Future<ResponseBody> Function(RequestOptions) route, {
    CredentialStore? credentialStore,
    AgentSessionStore? agentSessionStore,
  }) async {
    final store = credentialStore ?? MemoryCredentialStore();
    if (credentialStore == null) await store.write(_snapshot);
    final container = ProviderContainer(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(
          SessionDependencies(
            credentialStore: store,
            agentSessionStore: agentSessionStore ?? MemoryAgentSessionStore(),
            clientFactory: (String serverUrl) {
              return Dio(BaseOptions(baseUrl: serverUrl))
                ..httpClientAdapter = FakeAgentAdapter(route);
            },
          ),
        ),
      ],
    );
    addTearDown(container.dispose);
    // autoDispose 无监听会被立刻回收；测试期间保持 provider 存活。
    container.listen(
      agentControllerProvider,
      (AgentUiState? previous, AgentUiState next) {},
    );
    await container.read(sessionProvider.future);
    return (
      container: container,
      controller: container.read(agentControllerProvider.notifier),
    );
  }

  group('Agent 页失败必须可见且可重试', () {
    test('创建会话响应形态不符时进入可重试错误态，不卡在创建中', () async {
      var malformed = true;
      final it = await harness((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/agents') {
          return _jsonResponse(_availabilityResponse, 200);
        }
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryResponse, 200);
        }
        if (path == '/v1/agent/sessions') {
          // session.id 是数字：解析在异步边界抛 TypeError，不能逃逸成未处理异常。
          return malformed
              ? _jsonResponse('{"session": {"id": 7}}', 201)
              : _jsonResponse(_createdSessionJson, 201);
        }
        if (path.endsWith('/tool-calls')) {
          return _jsonResponse('{"toolCalls": []}', 200);
        }
        return _jsonResponse('unexpected', 404);
      });

      await it.controller.createSession();
      final failed = it.container.read(agentControllerProvider);
      expect(failed, isA<AgentError>());
      expect((failed as AgentError).message, contains('响应处理失败'));

      // 错误态提供重试路径：可用性恢复后回到创建视图，并可重新建立会话。
      malformed = false;
      await it.controller.loadAvailability();
      expect(it.container.read(agentControllerProvider), isA<AgentIdle>());
      await it.controller.createSession();
      expect(
        it.container.read(agentControllerProvider),
        isA<AgentSessionActive>(),
      );
    });

    test('回合响应形态不符时清除进行中标记并显示错误，可再次发送', () async {
      var malformedTurn = false;
      final it = await harness((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/agents') {
          return _jsonResponse(_availabilityResponse, 200);
        }
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryResponse, 200);
        }
        if (path == '/v1/agent/sessions') {
          return _jsonResponse(_createdSessionJson, 201);
        }
        if (path.endsWith('/messages')) {
          return malformedTurn
              ? _jsonResponse('{"session": {"id": 7}, "events": []}', 200)
              : _jsonResponse(_turnResponse(), 200);
        }
        if (path.endsWith('/tool-calls')) {
          return _jsonResponse(_toolCallsResponse(), 200);
        }
        return _jsonResponse('unexpected', 404);
      });

      await it.controller.createSession();
      expect(
        it.container.read(agentControllerProvider),
        isA<AgentSessionActive>(),
      );

      malformedTurn = true;
      await it.controller.sendTurn('我的现金余额是多少？');
      final failed =
          it.container.read(agentControllerProvider) as AgentSessionActive;
      expect(failed.turnInProgress, isFalse);
      expect(failed.errorMessage, contains('响应处理失败'));

      // 回合锁已解除：输入恢复后重发同一问题并拿到终态快照。
      malformedTurn = false;
      await it.controller.sendTurn('我的现金余额是多少？');
      final recovered =
          it.container.read(agentControllerProvider) as AgentSessionActive;
      expect(recovered.turnInProgress, isFalse);
      expect(recovered.errorMessage, isNull);
      expect(recovered.session.messages.last.content, 'USD 现金余额为 12,345.67。');
    });

    test('本机会话 ID 写入失败时不卡在创建中，恢复后可重试成功', () async {
      final sessionStore = FaultyAgentSessionStore(writeFails: true);
      final it = await harness((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/agents') {
          return _jsonResponse(_availabilityResponse, 200);
        }
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryResponse, 200);
        }
        if (path == '/v1/agent/sessions') {
          return _jsonResponse(_createdSessionJson, 201);
        }
        if (path.endsWith('/tool-calls')) {
          return _jsonResponse('{"toolCalls": []}', 200);
        }
        return _jsonResponse('unexpected', 404);
      }, agentSessionStore: sessionStore);

      await it.controller.createSession();
      final failed = it.container.read(agentControllerProvider);
      expect(failed, isA<AgentError>());
      expect((failed as AgentError).message, contains('响应处理失败'));

      sessionStore.writeFails = false;
      await it.controller.loadAvailability();
      await it.controller.createSession();
      expect(
        it.container.read(agentControllerProvider),
        isA<AgentSessionActive>(),
      );
      expect(await sessionStore.read(), isNotNull);
    });

    test('权威快照刷新失败在会话内可见，不产生未处理异步异常', () async {
      var malformedSnapshot = false;
      final it = await harness((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/agents') {
          return _jsonResponse(_availabilityResponse, 200);
        }
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryResponse, 200);
        }
        if (path == '/v1/agent/sessions') {
          return _jsonResponse(_createdSessionJson, 201);
        }
        if (path == '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001') {
          return malformedSnapshot
              ? _jsonResponse('{"session": {"id": 7}}', 200)
              : _jsonResponse(
                  _sessionResponse(
                    status: 'idle',
                    revision: 2,
                    messages: const <Map<String, Object?>>[],
                  ),
                  200,
                );
        }
        if (path.endsWith('/tool-calls')) {
          return _jsonResponse('{"toolCalls": []}', 200);
        }
        return _jsonResponse('unexpected', 404);
      });

      await it.controller.createSession();
      malformedSnapshot = true;
      // 终态帧触发权威快照刷新；畸形响应必须在会话内提示而不是逃逸。
      it.controller.handleFrame(
        const AgentEventFrame(
          event: AgentEventView(
            type: 'session.completed',
            sessionId: 'a1000000-0000-4000-8000-000000000001',
            messageId: null,
            sequence: 1,
            payload: <String, Object?>{},
          ),
        ),
      );
      await pumpEventQueue();

      final failed =
          it.container.read(agentControllerProvider) as AgentSessionActive;
      expect(failed.errorMessage, contains('响应处理失败'));
      expect(failed.turnInProgress, isFalse);
    });
  });

  testWidgets('创建会话失败显示可重试错误卡片，重试后恢复创建入口', (
    WidgetTester tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var malformed = true;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              agentSessionStore: MemoryAgentSessionStore(),
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = FakeAgentAdapter((options) async {
                    if (options.uri.path == '/v1/agent/sessions' && malformed) {
                      return _jsonResponse('{"session": {"id": 7}}', 201);
                    }
                    return _routeAgentRequest(options);
                  });
              },
            ),
          ),
        ],
        child: const MaterialApp(home: Scaffold(body: AgentScreen())),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('开始分析'));
    await tester.pumpAndSettle();

    // 不再停在“正在创建…”且按钮禁用；给出可重试的错误卡片。
    expect(find.text('正在创建…'), findsNothing);
    expect(find.text('AI 助手暂不可用'), findsOneWidget);
    expect(find.text('重试'), findsOneWidget);

    malformed = false;
    await tester.tap(find.text('重试'));
    await tester.pumpAndSettle();
    expect(find.text('开始分析'), findsOneWidget);
    await tester.tap(find.text('开始分析'));
    await tester.pumpAndSettle();
    expect(find.text('Lyra 助手'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
