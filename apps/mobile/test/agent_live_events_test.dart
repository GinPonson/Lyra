import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/agent/agent_live_events.dart';
import 'package:lyra_mobile/features/agent/agent_repository.dart';
import 'package:lyra_mobile/features/agent/agent_screen.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:stream_channel/stream_channel.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

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

/// 基于 [StreamChannelController] 的假 WebSocket 通道。
///
/// `foreign` 端由被测对象读写（本测试通过 local 端注入服务端帧并观察
/// 客户端 subscribe 帧）；close 外端时被测对象看到连接断开。
class ChannelMock extends StreamChannelMixin<dynamic>
    implements WebSocketChannel {
  ChannelMock(this._controller);

  final StreamChannelController<dynamic> _controller;

  @override
  Future<void> get ready => Future<void>.value();

  @override
  int? get closeCode => null;

  @override
  String? get closeReason => null;

  @override
  String? get protocol => null;

  @override
  WebSocketSink get sink => _MockWebSocketSink(_controller.foreign.sink);

  @override
  Stream<dynamic> get stream => _controller.foreign.stream;
}

/// 转发 close 到外端 sink 的假 WebSocket sink。
class _MockWebSocketSink implements WebSocketSink {
  _MockWebSocketSink(this._sink);

  final StreamSink<dynamic> _sink;

  @override
  void add(dynamic data) => _sink.add(data);

  @override
  void addError(Object error, [StackTrace? stackTrace]) =>
      _sink.addError(error, stackTrace);

  @override
  Future<dynamic> addStream(Stream<dynamic> stream) => _sink.addStream(stream);

  @override
  Future<dynamic> close([int? closeCode, String? closeReason]) =>
      _sink.close();

  @override
  Future<dynamic> get done => _sink.done;
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
  String provider = 'genkit',
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
    {"provider": "genkit", "status": "available"}
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
  userId: 'agent-user',
  authMethod: 'google',
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
              provider: 'genkit',
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
      await controller.createSession();
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
          .createSession();
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
      // Agent 恢复等待会话恢复完成（含 /me 身份核对）；显式等待避免依赖事件循环轮数。
      await secondContainer.read(sessionProvider.future);
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
      final credentials = MemoryCredentialStore();
      await credentials.write(_snapshot);
      final repository = AgentRepository(
        client: LyraApiClient(
          serverUrl: 'http://127.0.0.1:3141',
          deviceId: _snapshot.deviceId,
          credentialStore: credentials,
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

  group('AgentLiveEvents 连接地址', () {
    test('保留服务端部署子路径，只切换协议并追加事件路由', () async {
      final uris = <Uri>[];
      WebSocketChannel createChannel(
        Uri uri,
        Iterable<String> protocols,
        Map<String, String> headers,
      ) {
        uris.add(uri);
        return ChannelMock(
          StreamChannelController<dynamic>(allowForeignErrors: false),
        );
      }

      for (final serverUrl in <String>[
        'https://lyra.example.com/ledger/',
        'https://lyra.example.com/ledger',
        'https://lyra.example.com',
        'http://127.0.0.1:3141',
      ]) {
        final live = AgentLiveEvents(
          serverUrl: serverUrl,
          accessToken: 'token-1',
          onFrame: (AgentLiveFrame frame) {},
          channelFactory: createChannel,
        );
        live.subscribe(
          sessionId: 'a1000000-0000-4000-8000-000000000001',
          afterSequence: 0,
        );
        await pumpEventQueue();
        live.dispose();
      }

      // 子路径必须原样保留：HTTP 请求走 https://host/ledger/v1/...，
      // 事件流若指向 /v1/events 会永远连不上。
      expect(uris.map((uri) => uri.toString()).toList(), <String>[
        'wss://lyra.example.com/ledger/v1/events',
        'wss://lyra.example.com/ledger/v1/events',
        'wss://lyra.example.com/v1/events',
        'ws://127.0.0.1:3141/v1/events',
      ]);
    });
  });

  group('AgentLiveEvents 重连退避', () {
    test('意外断开按指数退避自动重连并带最新游标', () async {
      final channels = <StreamChannelController<dynamic>>[];
      final connectionTokens = <String?>[];
      final subscribeFrames = <Map<String, Object?>>[];
      WebSocketChannel createChannel(
        Uri uri,
        Iterable<String> protocols,
        Map<String, String> headers,
      ) {
        expect(headers['x-lyra-protocol-version'], '3');
        connectionTokens.add(headers['authorization']);
        final controller = StreamChannelController<dynamic>(
          allowForeignErrors: false,
        );
        channels.add(controller);
        // 捕获 subscribe 帧。
        controller.local.stream.listen((message) {
          subscribeFrames.add(jsonDecode(message) as Map<String, Object?>);
        });
        return ChannelMock(controller);
      }

      final received = <AgentLiveFrame>[];
      final live = AgentLiveEvents(
        serverUrl: 'http://127.0.0.1:3141',
        accessToken: 'token-1',
        onFrame: received.add,
        reconnectDelay: const Duration(milliseconds: 10),
        maxReconnectDelay: const Duration(milliseconds: 40),
        channelFactory: createChannel,
      );
      addTearDown(live.dispose);
      live.subscribe(sessionId: 'a1000000-0000-4000-8000-000000000001', afterSequence: 5);
      expect(channels, hasLength(1));
      await pumpEventQueue();

      // 首次连接确认后推送事件 6，随后意外断开。
      channels[0].local.sink.add(jsonEncode(<String, Object?>{
        'type': 'subscribed', 'mode': 'events', 'events': <Object?>[],
      }));
      channels[0].local.sink.add(jsonEncode(<String, Object?>{
        'type': 'event',
        'event': <String, Object?>{
          'type': 'message.delta',
          'sessionId': 'a1000000-0000-4000-8000-000000000001',
          'messageId': 'm1',
          'sequence': 6,
          'occurredAt': '2026-09-05T10:00:06.000Z',
          'payload': <String, Object?>{'text': 'A'},
        },
      }));
      await pumpEventQueue();
      expect(live.lastSequence, 6);

      // 模拟服务器断开。
      live.updateAccessToken('token-2');
      await channels[0].local.sink.close();
      await pumpEventQueue();
      expect(live.isConnected, isFalse);
      expect(subscribeFrames, hasLength(1));

      // 退避后自动重连：第二通道出现，subscribe 帧带最新游标 6。
      await Future<void>.delayed(const Duration(milliseconds: 30));
      await pumpEventQueue();
      expect(channels, hasLength(2));
      expect(connectionTokens.last, 'Bearer token-2');
      expect(subscribeFrames, hasLength(2));
      expect(subscribeFrames[1]['afterSequence'], 6);

      // 重连确认后游标推进。
      channels[1].local.sink.add(jsonEncode(<String, Object?>{
        'type': 'subscribed', 'mode': 'events', 'events': <Object?>[],
      }));
      await pumpEventQueue();
      expect(received.whereType<AgentSubscribedFrame>(), hasLength(2));
    });

    test('主动 close 不触发重连，dispose 后完全静默', () async {
      final channels = <StreamChannelController<dynamic>>[];
      WebSocketChannel createChannel(
        Uri uri,
        Iterable<String> protocols,
        Map<String, String> headers,
      ) {
        final controller = StreamChannelController<dynamic>(
          allowForeignErrors: false,
        );
        channels.add(controller);
        return ChannelMock(controller);
      }

      final live = AgentLiveEvents(
        serverUrl: 'http://127.0.0.1:3141',
        accessToken: 'token-1',
        onFrame: (AgentLiveFrame frame) {},
        reconnectDelay: const Duration(milliseconds: 5),
        maxReconnectDelay: const Duration(milliseconds: 20),
        channelFactory: createChannel,
      );
      addTearDown(live.dispose);
      live.subscribe(sessionId: 'a1000000-0000-4000-8000-000000000001', afterSequence: 0);
      await pumpEventQueue();
      expect(channels, hasLength(1));

      // 主动关闭：断开不排重连。
      live.close();
      await pumpEventQueue();
      await Future<void>.delayed(const Duration(milliseconds: 20));
      await pumpEventQueue();
      expect(channels, hasLength(1));
      expect(live.isConnected, isFalse);

      // dispose 后 subscribe 无效果。
      live.dispose();
      live.subscribe(sessionId: 'a1000000-0000-4000-8000-000000000001', afterSequence: 0);
      await pumpEventQueue();
      expect(channels, hasLength(1));
    });

    test('连续失败退避封顶且 subscribed 确认后重置', () async {
      final channels = <StreamChannelController<dynamic>>[];
      final delays = <Duration>[];
      WebSocketChannel createChannel(
        Uri uri,
        Iterable<String> protocols,
        Map<String, String> headers,
      ) {
        final controller = StreamChannelController<dynamic>(
          allowForeignErrors: false,
        );
        channels.add(controller);
        return ChannelMock(controller);
      }

      final received = <AgentLiveFrame>[];
      final live = AgentLiveEvents(
        serverUrl: 'http://127.0.0.1:3141',
        accessToken: 'token-1',
        onFrame: received.add,
        reconnectDelay: const Duration(milliseconds: 10),
        maxReconnectDelay: const Duration(milliseconds: 25),
        channelFactory: createChannel,
      );
      addTearDown(live.dispose);
      live.subscribe(sessionId: 'a1000000-0000-4000-8000-000000000001', afterSequence: 0);

      // 连续三次断开：等待三段重连时间，全部封顶在 25ms。
      for (var round = 0; round < 3; round++) {
        final index = channels.length - 1;
        await pumpEventQueue();
        await channels[index].local.sink.close();
        await Future<void>.delayed(const Duration(milliseconds: 40));
        await pumpEventQueue();
        expect(channels.length, round + 2, reason: 'round $round 应重连');
      }

      // 第三次重连确认后计数重置：再断开仍按基础延迟快速重连。
      final lastIndex = channels.length - 1;
      channels[lastIndex].local.sink.add(jsonEncode(<String, Object?>{
        'type': 'subscribed', 'mode': 'events', 'events': <Object?>[],
      }));
      await pumpEventQueue();
      expect(received.whereType<AgentSubscribedFrame>(), hasLength(1));
      expect(delays, isEmpty);
    });
  });

}
