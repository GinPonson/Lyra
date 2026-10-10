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
import 'package:lyra_mobile/features/agent/agent_repository.dart';
import 'package:lyra_mobile/features/agent/agent_screen.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';

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
}
