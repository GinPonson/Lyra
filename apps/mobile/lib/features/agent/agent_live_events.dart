import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

import 'agent_repository.dart';

/// WebSocket 事件面的单帧视图；只保留 App 逻辑需要的判别信息。
sealed class AgentLiveFrame {
  const AgentLiveFrame();
}

/// 订阅确认：缓冲续传事件或要求重取快照。
class AgentSubscribedFrame extends AgentLiveFrame {
  const AgentSubscribedFrame({
    required this.mode,
    this.events = const <AgentEventView>[],
    this.reason,
  });

  final String mode;
  final List<AgentEventView> events;
  final String? reason;

  factory AgentSubscribedFrame.fromJson(Map<String, Object?> json) {
    final rawEvents = json['events'] as List<Object?>? ?? const <Object?>[];
    return AgentSubscribedFrame(
      mode: json['mode'] as String,
      events: rawEvents
          .map((event) =>
              AgentEventView.fromJson(event as Map<String, Object?>))
          .toList(growable: false),
      reason: json['reason'] as String?,
    );
  }
}

/// 实时持久化事件。
class AgentEventFrame extends AgentLiveFrame {
  const AgentEventFrame({required this.event});

  final AgentEventView event;
}

/// 服务端错误帧（订阅帧无效等）。
class AgentErrorFrame extends AgentLiveFrame {
  const AgentErrorFrame({required this.code, required this.message});

  final String code;
  final String message;
}

/// 无法识别的帧形态；保守忽略而不是崩溃。
class AgentUnknownFrame extends AgentLiveFrame {
  const AgentUnknownFrame();
}

/// `/v1/events` WebSocket 事件流客户端。
///
/// 连接建立后发送一条 `subscribe` 帧绑定会话与游标；断开时通过 [onDone]
/// 通知上层重连。序号缺口不在此补造：上层收到缺口必须重取规范快照。
class AgentLiveEvents {
  AgentLiveEvents({
    required this.serverUrl,
    required this.accessToken,
    required this.onFrame,
    this.onDone,
  });

  final String serverUrl;
  final String accessToken;
  final void Function(AgentLiveFrame frame) onFrame;
  final void Function()? onDone;

  WebSocketChannel? _channel;
  bool _disposed = false;
  int? _lastSequence;

  /// 当前订阅游标；发送 subscribe 帧后递增。
  int? get lastSequence => _lastSequence;

  bool get isConnected => _channel != null;

  /// 建立连接并发送订阅帧；[afterSequence] 为已消费的最大事件序号。
  void subscribe({
    required String sessionId,
    required int afterSequence,
  }) {
    if (_disposed) {
      return;
    }
    _lastSequence = afterSequence;
    final wsUri = Uri.parse(serverUrl).replace(
      scheme: serverUrl.startsWith('https') ? 'wss' : 'ws',
      path: '/v1/events',
    );
    final channel = WebSocketChannel.connect(
      wsUri,
      protocols: <String>['bearer', accessToken],
    );
    _channel = channel;
    channel.stream.listen(
      _handleMessage,
      onDone: () {
        _channel = null;
        if (!_disposed) {
          onDone?.call();
        }
      },
      onError: (Object error) {
        _channel = null;
        if (!_disposed) {
          onDone?.call();
        }
      },
      cancelOnError: true,
    );
    // subscribe 帧在连接建立后由底层队列发送。
    channel.sink.add(jsonEncode(<String, Object?>{
      'type': 'subscribe',
      'sessionId': sessionId,
      'afterSequence': afterSequence,
    }));
  }

  /// 关闭连接；不再触发 onDone。
  void dispose() {
    _disposed = true;
    _channel?.sink.close();
    _channel = null;
  }

  void _handleMessage(Object? message) {
    final frame = parseAgentLiveFrame(message);
    if (frame is AgentEventFrame) {
      _lastSequence = frame.event.sequence;
    }
    onFrame(frame);
  }
}

/// 把事件面原始帧解析为类型化视图；无法识别的帧保守忽略。
AgentLiveFrame parseAgentLiveFrame(Object? message) {
  Map<String, Object?> json;
  try {
    json = jsonDecode(message as String) as Map<String, Object?>;
  } on Object {
    return const AgentUnknownFrame();
  }
  return switch (json['type']) {
    'subscribed' => AgentSubscribedFrame.fromJson(json),
    'event' => AgentEventFrame(
        event: AgentEventView.fromJson(
          json['event'] as Map<String, Object?>,
        ),
      ),
    'error' => AgentErrorFrame(
        code: json['code'] as String? ?? 'UNKNOWN',
        message: json['message'] as String? ?? '',
      ),
    _ => const AgentUnknownFrame(),
  };
}
