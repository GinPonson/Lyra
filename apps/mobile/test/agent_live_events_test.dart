import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/agent/agent_live_events.dart';
import 'package:lyra_mobile/features/agent/agent_repository.dart';
import 'package:lyra_mobile/features/agent/agent_screen.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';

/// 按请求路径返回固定响应的假 HTTP 适配器；不触达真实网络。
class FakeFrameAdapter implements HttpClientAdapter {
  FakeFrameAdapter(this.handler);

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

ResponseBody _jsonResponse(String body, int status) {
  return ResponseBody.fromString(
    body,
    status,
    headers: <String, List<String>>{
      Headers.contentTypeHeader: <String>[Headers.jsonContentType],
    },
  );
}

Map<String, Object?> _sessionBody({
  required int revision,
  String provider = 'claude',
  required List<Object?> messages,
}) {
  return <String, Object?>{
    'session': <String, Object?>{
      'id': 'a1000000-0000-4000-8000-000000000001',
      'provider': provider,
      'status': 'idle',
      'accountScope': <String, Object?>{'kind': 'all'},
      'ledgerVersion': 1,
      'asOf': '2026-09-05T10:00:00.000Z',
      'revision': revision,
      'messages': messages,
    },
  };
}

const String _availabilityJson = '''
{
  "providers": [
    {"provider": "claude", "status": "available", "version": "2.1.233"},
    {"provider": "pi", "status": "unavailable", "reason": "VERSION_NOT_ALLOWED"},
    {"provider": "opencode", "status": "available", "version": "1.18.21"}
  ]
}
''';

const String _summaryJson = '''
{"metadata":{"accountScope":{"kind":"all"},"accountIds":[],"currencyGroups":[],
"aggregateCurrency":null,"ledgerVersion":1,"asOf":"2026-09-05T10:00:00.000Z",
"fetchedAt":"2026-09-05T10:00:00.000Z"},"data":{"aggregateCurrency":null,"groups":[]}}
''';

const CredentialSnapshot _snapshot = CredentialSnapshot(
  serverUrl: 'http://127.0.0.1:3141',
  deviceId: 'd1000000-0000-4000-8000-000000000001',
  accessToken: 'access-token-1',
  refreshToken: 'refresh-token-1',
);

AgentEventView _event(int sequence, String type, {String? text}) {
  return AgentEventView(
    type: type,
    sessionId: 'a1000000-0000-4000-8000-000000000001',
    messageId: 'm1000000-0000-4000-8000-000000000002',
    sequence: sequence,
    payload: text == null
        ? <String, Object?>{}
        : <String, Object?>{'text': text},
  );
}

void main() {
  group('parseAgentLiveFrame', () {
    test('解析 subscribed 续传帧与事件帧', () {
      final subscribed = parseAgentLiveFrame(jsonEncode(<String, Object?>{
        'type': 'subscribed',
        'mode': 'events',
        'events': <Map<String, Object?>>[
          <String, Object?>{
            'type': 'message.delta',
            'sessionId': 'a1000000-0000-4000-8000-000000000001',
            'messageId': 'm1000000-0000-4000-8000-000000000002',
            'sequence': 4,
            'occurredAt': '2026-09-05T10:00:04.000Z',
            'payload': <String, Object?>{'text': 'USD '},
          },
        ],
      })) as AgentSubscribedFrame;
      expect(subscribed.mode, 'events');
      expect(subscribed.events.single.sequence, 4);
      expect(subscribed.events.single.payload['text'], 'USD ');

      final event = parseAgentLiveFrame(jsonEncode(<String, Object?>{
        'type': 'event',
        'event': <String, Object?>{
          'type': 'session.completed',
          'sessionId': 'a1000000-0000-4000-8000-000000000001',
          'messageId': null,
          'sequence': 9,
          'occurredAt': '2026-09-05T10:00:09.000Z',
          'payload': <String, Object?>{},
        },
      })) as AgentEventFrame;
      expect(event.event.type, 'session.completed');
      expect(event.event.sequence, 9);
    });

    test('snapshot_required、error 与未知帧各自归类', () {
      final snapshotRequired =
          parseAgentLiveFrame(jsonEncode(<String, Object?>{
        'type': 'subscribed',
        'mode': 'snapshot_required',
        'reason': 'SEQUENCE_GAP_EXCEEDS_BUFFER',
      })) as AgentSubscribedFrame;
      expect(snapshotRequired.mode, 'snapshot_required');
      expect(snapshotRequired.reason, 'SEQUENCE_GAP_EXCEEDS_BUFFER');

      final error = parseAgentLiveFrame(jsonEncode(<String, Object?>{
        'type': 'error',
        'code': 'INVALID_ARGUMENT',
        'message': 'WebSocket 订阅帧无效',
      })) as AgentErrorFrame;
      expect(error.code, 'INVALID_ARGUMENT');
      expect(error.message, 'WebSocket 订阅帧无效');

      expect(parseAgentLiveFrame('not-json'), isA<AgentUnknownFrame>());
      expect(
        parseAgentLiveFrame(jsonEncode(<String, Object?>{
          'type': 'future-frame',
        })),
        isA<AgentUnknownFrame>(),
      );
    });
  });

  group('AgentController.handleFrame', () {
    late ProviderContainer container;
    late AgentController controller;

    setUp(() async {
      final adapter = FakeFrameAdapter((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/agents') {
          return _jsonResponse(_availabilityJson, 200);
        }
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryJson, 200);
        }
        if (path == '/v1/agent/sessions') {
          return _jsonResponse(
            jsonEncode(_sessionBody(revision: 1, messages: const <Object?>[])),
            201,
          );
        }
        if (path.endsWith('/sessions/a1000000-0000-4000-8000-000000000001')) {
          return _jsonResponse(
            jsonEncode(_sessionBody(
              revision: 7,
              provider: 'claude',
              messages: <Object?>[
                <String, Object?>{
                  'id': 'm1000000-0000-4000-8000-000000000002',
                  'role': 'assistant',
                  'content': 'USD 现金余额为 12,345.67。',
                  'createdAt': '2026-09-05T10:00:02.000Z',
                },
              ],
            )),
            200,
          );
        }
        return _jsonResponse('unexpected', 404);
      });
      final store = MemoryCredentialStore();
      await store.write(_snapshot);
      container = ProviderContainer(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              agentSessionStore: MemoryAgentSessionStore(),
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = adapter;
              },
            ),
          ),
        ],
      );
      await container.read(sessionProvider.future);
      // autoDispose 无监听会被立刻回收；测试期间保持 provider 存活。
      container.listen(
        agentControllerProvider,
        (AgentUiState? previous, AgentUiState next) {},
      );
      controller = container.read(agentControllerProvider.notifier);
      await controller.createSession('claude');
    });

    tearDown(() {
      container.dispose();
    });

    test('delta 累积实时预览，terminal 帧清空并触发快照刷新', () async {
      controller.handleFrame(
        AgentEventFrame(event: _event(1, 'message.delta', text: 'USD ')),
      );
      controller.handleFrame(
        AgentEventFrame(event: _event(2, 'message.delta', text: '12,345.67')),
      );
      expect(
        (controller.state as AgentSessionActive).streamingText,
        'USD 12,345.67',
      );

      controller.handleFrame(
        AgentEventFrame(event: _event(3, 'session.completed')),
      );
      // terminal 帧清空流式预览并重取快照（假适配器返回 revision 7）。
      await pumpEventQueue();
      final refreshed = controller.state as AgentSessionActive;
      expect(refreshed.streamingText, isNull);
      expect(refreshed.session.revision, 7);
      expect(refreshed.session.messages.single.content, 'USD 现金余额为 12,345.67。');
    });

    test('重复事件忽略；缺口触发快照刷新后游标仍可继续推进', () async {
      controller.handleFrame(
        AgentEventFrame(event: _event(1, 'message.delta', text: 'A')),
      );
      controller.handleFrame(
        AgentEventFrame(event: _event(1, 'message.delta', text: 'A')),
      );
      expect(
        (controller.state as AgentSessionActive).streamingText,
        'A',
      );

      // 缺口：3 > 1+1；快照刷新后预览清空、游标保持 1。
      controller.handleFrame(
        AgentEventFrame(event: _event(3, 'message.delta', text: 'B')),
      );
      await pumpEventQueue();
      final refreshed = controller.state as AgentSessionActive;
      expect(refreshed.streamingText, isNull);
      expect(refreshed.session.revision, 7);

      // 缺口后游标未越位：顺序到达的 seq 2 继续累积。
      controller.handleFrame(
        AgentEventFrame(event: _event(2, 'message.delta', text: 'C')),
      );
      expect(
        (controller.state as AgentSessionActive).streamingText,
        'C',
      );
    });

    test('snapshot_required 帧触发快照刷新', () async {
      controller.handleFrame(
        const AgentSubscribedFrame(mode: 'snapshot_required'),
      );
      await pumpEventQueue();
      expect(
        (controller.state as AgentSessionActive).session.revision,
        7,
      );
    });
  });

  group('会话恢复', () {
    test('新建会话持久化 ID；新容器经服务端快照恢复会话视图', () async {
      final store = MemoryAgentSessionStore();
      final adapter = FakeFrameAdapter((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/agents') {
          return _jsonResponse(_availabilityJson, 200);
        }
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryJson, 200);
        }
        if (path == '/v1/agent/sessions') {
          return _jsonResponse(
            jsonEncode(_sessionBody(revision: 1, messages: const <Object?>[])),
            201,
          );
        }
        if (path.endsWith('/sessions/a1000000-0000-4000-8000-000000000001')) {
          return _jsonResponse(
            jsonEncode(_sessionBody(
              revision: 2,
              messages: <Object?>[
                <String, Object?>{
                  'id': 'm1000000-0000-4000-8000-000000000001',
                  'role': 'user',
                  'content': '上一次的提问',
                  'createdAt': '2026-09-05T10:00:01.000Z',
                },
              ],
            )),
            200,
          );
        }
        return _jsonResponse('unexpected', 404);
      });
      final credentialStore = MemoryCredentialStore();
      await credentialStore.write(_snapshot);

      // 第一个容器：创建会话并断言 ID 已持久化。
      final firstContainer = ProviderContainer(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: credentialStore,
              agentSessionStore: store,
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = adapter;
              },
            ),
          ),
        ],
      );
      addTearDown(firstContainer.dispose);
      firstContainer.listen(
        agentControllerProvider,
        (AgentUiState? previous, AgentUiState next) {},
      );
      await firstContainer.read(sessionProvider.future);
      await firstContainer
          .read(agentControllerProvider.notifier)
          .createSession('claude');
      expect(
        await store.read(),
        'a1000000-0000-4000-8000-000000000001',
      );
      firstContainer.dispose();

      // 第二个容器：模拟 App 重启，进入 Agent 页自动恢复。
      final secondContainer = ProviderContainer(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: credentialStore,
              agentSessionStore: store,
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = adapter;
              },
            ),
          ),
        ],
      );
      addTearDown(secondContainer.dispose);
      secondContainer.listen(
        agentControllerProvider,
        (AgentUiState? previous, AgentUiState next) {},
      );
      await pumpEventQueue();
      final restored = secondContainer.read(agentControllerProvider);
      expect(restored, isA<AgentSessionActive>());
      final session = (restored as AgentSessionActive).session;
      expect(session.id, 'a1000000-0000-4000-8000-000000000001');
      expect(session.messages.single.content, '上一次的提问');
    });

    test('服务端会话不存在时清除本地 ID 并回到创建视图', () async {
      final store = MemoryAgentSessionStore();
      await store.write('a1000000-0000-4000-8000-000000000001');
      final adapter = FakeFrameAdapter((RequestOptions options) async {
        final path = options.uri.path;
        if (path == '/v1/ledger/summary') {
          return _jsonResponse(_summaryJson, 200);
        }
        if (path.endsWith('/sessions/a1000000-0000-4000-8000-000000000001')) {
          return _jsonResponse(
            jsonEncode(<String, Object?>{
              'code': 'AGENT_SESSION_STATE_CONFLICT',
              'message': 'Agent 会话不存在或已关闭',
            }),
            404,
          );
        }
        return _jsonResponse('unexpected', 404);
      });
      final credentialStore = MemoryCredentialStore();
      await credentialStore.write(_snapshot);
      final container = ProviderContainer(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: credentialStore,
              agentSessionStore: store,
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = adapter;
              },
            ),
          ),
        ],
      );
      addTearDown(container.dispose);
      container.listen(
        agentControllerProvider,
        (AgentUiState? previous, AgentUiState next) {},
      );
      await pumpEventQueue();
      expect(
        container.read(agentControllerProvider),
        isA<AgentIdle>(),
      );
      expect(await store.read(), isNull);
    });
  });

  group('工具调用公开列表', () {
    test('仓库层解析 tool-calls 路由响应', () async {
      final adapter = FakeFrameAdapter((RequestOptions options) async {
        expect(
          options.uri.path,
          '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001/tool-calls',
        );
        return _jsonResponse(
          jsonEncode(<String, Object?>{
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
                'toolName': 'transactions',
                'status': 'started',
                'argumentsSummary': <String, Object?>{'accountScope': 'all'},
                'resultSummary': null,
                'ledgerVersion': 1,
                'durationMs': null,
                'errorCode': null,
                'startedAt': '2026-09-01T09:00:02.000Z',
                'completedAt': null,
              },
              <String, Object?>{
                'id': 'c1000000-0000-4000-8000-000000000003',
                'messageId': 'm1000000-0000-4000-8000-000000000002',
                'ordinal': 3,
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
          }),
          200,
        );
      });
      final repository = AgentRepository(
        client: LyraApiClient(
          serverUrl: 'http://127.0.0.1:3141',
          credentialStore: MemoryCredentialStore(),
          tokenRefresher: () async => null,
          dio: Dio(BaseOptions(baseUrl: 'http://127.0.0.1:3141'))
            ..httpClientAdapter = adapter,
        ),
      );
      final calls = await repository.toolCalls(
        'a1000000-0000-4000-8000-000000000001',
      );
      expect(calls, hasLength(3));
      expect(calls[0].toolName, 'accounts');
      expect(calls[0].argumentsSummary, <String, Object?>{
        'accountScope': 'all',
      });
      expect(calls[0].resultSummary, <String, Object?>{'count': 3});
      expect(calls[0].durationMs, 42);
      expect(calls[1].status, 'started');
      expect(calls[1].resultSummary, isNull);
      expect(calls[1].durationMs, isNull);
      expect(calls[2].errorCode, 'AGENT_TOOL_EXECUTION_FAILED');
    });
  });

  group('回合边界切换', () {
    test('仓库层发送 switch 请求并解析新供应商快照', () async {
      final adapter = FakeFrameAdapter((RequestOptions options) async {
        expect(
          options.uri.path,
          '/v1/agent/sessions/a1000000-0000-4000-8000-000000000001/switch',
        );
        final body = options.data is Map
            ? (options.data as Map).cast<String, Object?>()
            : jsonDecode(options.data.toString()) as Map<String, Object?>;
        expect(body['provider'], 'opencode');
        return _jsonResponse(
          jsonEncode(_sessionBody(
            revision: 2,
            provider: 'opencode',
            messages: <Object?>[
              <String, Object?>{
                'id': 'm1000000-0000-4000-8000-000000000003',
                'role': 'system',
                'content': '已切换到 OpenCode。上下文已交接，历史结论仅供参考。',
                'createdAt': '2026-09-05T10:00:05.000Z',
              },
            ],
          )),
          200,
        );
      });
      final repository = AgentRepository(
        client: LyraApiClient(
          serverUrl: 'http://127.0.0.1:3141',
          credentialStore: MemoryCredentialStore(),
          tokenRefresher: () async => null,
          dio: Dio(BaseOptions(baseUrl: 'http://127.0.0.1:3141'))
            ..httpClientAdapter = adapter,
        ),
      );
      final switched = await repository.switchSession(
        sessionId: 'a1000000-0000-4000-8000-000000000001',
        targetProvider: 'opencode',
      );
      expect(switched.provider, 'opencode');
      expect(switched.revision, 2);
      expect(switched.messages.single.role, 'system');
    });

    test('目标不可用返回稳定 409 与错误信息', () async {
      final adapter = FakeFrameAdapter((RequestOptions options) async {
        return _jsonResponse(
          jsonEncode(<String, Object?>{
            'code': 'AGENT_UNAVAILABLE',
            'message': '目标 Agent 当前不可用',
          }),
          409,
        );
      });
      final repository = AgentRepository(
        client: LyraApiClient(
          serverUrl: 'http://127.0.0.1:3141',
          credentialStore: MemoryCredentialStore(),
          tokenRefresher: () async => null,
          dio: Dio(BaseOptions(baseUrl: 'http://127.0.0.1:3141'))
            ..httpClientAdapter = adapter,
        ),
      );
      await expectLater(
        repository.switchSession(
          sessionId: 'a1000000-0000-4000-8000-000000000001',
          targetProvider: 'pi',
        ),
        throwsA(
          isA<LyraApiError>()
              .having(
                (LyraApiError error) => error.code,
                'code',
                'AGENT_UNAVAILABLE',
              )
              .having(
                (LyraApiError error) => error.statusCode,
                'statusCode',
                409,
              ),
        ),
      );
    });
  });
}
