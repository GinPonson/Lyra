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
    {"provider": "claude", "status": "available", "version": "2.1.233"},
    {"provider": "pi", "status": "unavailable", "reason": "VERSION_NOT_ALLOWED"},
    {"provider": "opencode", "status": "available", "version": "1.18.21"}
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
      'provider': 'claude',
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
      'provider': 'claude',
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

/// 回合边界切换后的会话快照：同会话 ID、revision 递增、边界 system 消息。
String _switchedSessionJson(String target) {
  return jsonEncode(<String, Object?>{
    'session': <String, Object?>{
      'id': 'a1000000-0000-4000-8000-000000000001',
      'provider': target,
      'status': 'idle',
      'accountScope': <String, Object?>{'kind': 'all'},
      'ledgerVersion': 1,
      'asOf': '2026-09-05T10:00:05.000Z',
      'revision': 4,
      'messages': <Map<String, Object?>>[
        <String, Object?>{
          'id': 'm1000000-0000-4000-8000-000000000003',
          'role': 'system',
          'content': '已切换到 ${_providerLabelOf(target)}。上下文已交接，历史结论仅供参考。',
          'createdAt': '2026-09-05T10:00:05.000Z',
        },
      ],
    },
  });
}

String _providerLabelOf(String provider) {
  return switch (provider) {
    'claude' => 'Claude',
    'pi' => 'Pi',
    'opencode' => 'OpenCode',
    _ => provider,
  };
}

const String _createdSessionJson = '''
{
  "session": {
    "id": "a1000000-0000-4000-8000-000000000001",
    "provider": "claude",
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

Future<ResponseBody> _routeAgentRequest(
  RequestOptions options, {
  String switchTarget = 'opencode',
}) async {
  final path = options.uri.path;
  if (path == '/v1/agents') {
    return _jsonResponse(_availabilityResponse, 200);
  }
  if (path == '/v1/ledger/summary') {
    return _jsonResponse(_summaryResponse, 200);
  }
  if (path.endsWith('/switch')) {
    return _jsonResponse(_switchedSessionJson(switchTarget), 200);
  }
  if (path == '/v1/agent/sessions') {
    final body = _decodeBody(options);
    expect(body['provider'], 'claude');
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
    final repository = AgentRepository(
      client: LyraApiClient(
        serverUrl: 'http://127.0.0.1:3141',
        credentialStore: MemoryCredentialStore(),
        tokenRefresher: () async => null,
        dio: dio,
      ),
    );

    final availability = await repository.availability();
    expect(availability.items, hasLength(3));
    expect(availability.items[0].provider, 'claude');
    expect(availability.items[0].available, isTrue);
    expect(availability.items[0].version, '2.1.233');
    expect(availability.items[1].available, isFalse);
    expect(availability.items[1].unavailableReason, 'VERSION_NOT_ALLOWED');

    final session = await repository.createSession(
      provider: 'claude',
      accountScopeKind: 'all',
    );
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

    // 创建会话：点 Claude 入口。
    expect(find.text('开始 Claude 会话'), findsOneWidget);
    expect(find.text('开始 Pi 会话'), findsOneWidget);
    expect(find.text('开始 OpenCode 会话'), findsOneWidget);
    await tester.tap(find.text('开始 Claude 会话'));
    await tester.pumpAndSettle();

    // 会话视图出现，发送回合。
    expect(find.text('Agent · Claude'), findsOneWidget);
    expect(find.text('空闲'), findsOneWidget);
    await tester.enterText(find.byType(TextField), '我的现金余额是多少？');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    await tester.pumpAndSettle();

    // 终态快照渲染用户与助手消息。
    expect(find.text('我的现金余额是多少？'), findsOneWidget);
    expect(find.text('USD 现金余额为 12,345.67。'), findsOneWidget);
  });

  testWidgets('idle 会话可切换供应商并渲染边界消息', (WidgetTester tester) async {
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

    await tester.tap(find.text('开始 Claude 会话'));
    await tester.pumpAndSettle();

    // 打开切换菜单并选择 OpenCode。
    await tester.tap(find.byTooltip('切换 Agent'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('切换到 OpenCode'));
    await tester.pumpAndSettle();

    // 切换后：同会话、新供应商、边界 system 消息上屏。
    expect(find.text('Agent · OpenCode'), findsOneWidget);
    expect(
      find.text('已切换到 OpenCode。上下文已交接，历史结论仅供参考。'),
      findsOneWidget,
    );
  });

  testWidgets('未配对时 Agent 页不提供会话入口', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      const ProviderScope(
        child: MaterialApp(home: Scaffold(body: AgentScreen())),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('未连接服务端'), findsOneWidget);
    expect(find.textContaining('开始 Claude 会话'), findsNothing);
  });
}
