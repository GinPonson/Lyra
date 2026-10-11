import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../../core/api/lyra_api_client.dart';
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
          .map(
            (event) => AgentEventView.fromJson(event as Map<String, Object?>),
          )
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

/// 连接工厂；生产用真实 WebSocket，测试注入假通道。
typedef AgentChannelFactory = WebSocketChannel Function(
  Uri uri,
  Iterable<String> protocols,
  Map<String, String> headers,
);

WebSocketChannel _defaultChannelFactory(
  Uri uri,
  Iterable<String> protocols,
  Map<String, String> headers,
) {
  return IOWebSocketChannel.connect(
    uri,
    protocols: protocols,
    headers: headers,
  );
}

/// `/v1/events` WebSocket 事件流客户端。
///
/// 连接建立后发送一条 `subscribe` 帧绑定会话与游标；意外断开时按指数退避
/// 自动重连，重连带上游标续传，缺口由上层快照解决。退避计数只在收到服务端
/// `subscribed` 确认帧后重置，连续失败按 [reconnectDelay] 翻倍直至
/// [maxReconnectDelay]。主动 [close] 或 [dispose] 后不再重连。
class AgentLiveEvents {
  AgentLiveEvents({
    required this.serverUrl,
    required this._accessToken,
    required this.onFrame,
    this.onDone,
    this.reconnectDelay = _defaultReconnectDelay,
    this.maxReconnectDelay = _defaultMaxReconnectDelay,
    this.channelFactory = _defaultChannelFactory,
  });

  final String serverUrl;
  String _accessToken;
  String get accessToken => _accessToken;

  /// HTTP 刷新后的令牌用于下次握手，已建立的流不必重建。
  void updateAccessToken(String token) => _accessToken = token;
  final void Function(AgentLiveFrame frame) onFrame;

  /// 意外断开时的通知回调；重连由客户端内部完成，回调只用于上层提示。
  final void Function()? onDone;

  /// 首次重连等待时间；每次连续失败翻倍直至 [maxReconnectDelay]。
  final Duration reconnectDelay;
  final Duration maxReconnectDelay;
  final AgentChannelFactory channelFactory;

  static const Duration _defaultReconnectDelay = Duration(seconds: 1);
  static const Duration _defaultMaxReconnectDelay = Duration(seconds: 30);

  WebSocketChannel? _channel;
  Timer? _reconnectTimer;
  bool _disposed = false;
  bool _closedByUser = false;
  bool _reconnectPending = false;
  int _reconnectAttempt = 0;
  int? _lastSequence;
  String? _sessionId;

  /// 当前订阅游标；发送 subscribe 帧后随事件递增。
  int? get lastSequence => _lastSequence;

  bool get isConnected => _channel != null;

  /// 建立连接并发送订阅帧；[afterSequence] 为已消费的最大事件序号。
  ///
  /// 已在线时只更新游标不重复建连；等待重连期间调用会取消定时器立即重连。
  void subscribe({required String sessionId, required int afterSequence}) {
    if (_disposed) {
      return;
    }
    _closedByUser = false;
    _reconnectPending = false;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _reconnectAttempt = 0;
    _sessionId = sessionId;
    _lastSequence = afterSequence;
    _connect(sessionId, afterSequence);
  }

  void _connect(String sessionId, int afterSequence) {
    if (_disposed || _channel != null) {
      return;
    }
    // 事件流必须与 HTTP 调用落在同一个部署前缀上：服务端可能挂在反向代理的
    // 子路径（如 https://host/lyra），而 replace(path:) 会把整个前缀替换成
    // /v1/events，使连接永远打不开。这里保留基路径、只追加事件路由。
    final base = Uri.parse(serverUrl);
    final wsUri = base.replace(
      scheme: base.scheme == 'https' ? 'wss' : 'ws',
      path: '${base.path.replaceFirst(RegExp(r'/+$'), '')}/v1/events',
    );
    // 握手必须携带协议版本头与 Bearer 令牌;服务端在升级前校验两者。
    final channel = channelFactory(wsUri, const <String>[], <String, String>{
      'authorization': 'Bearer $accessToken',
      'x-lyra-protocol-version': kLyraProtocolVersion,
    });
    _channel = channel;
    // 连接失败会同时从 ready 与 stream 两个入口报错；ready 在此显式吞掉，
    // 断开重连统一由 stream 的 onError/onDone 驱动，避免未处理异步异常。
    unawaited(channel.ready.catchError((Object error) {}));
    channel.stream.listen(
      _handleMessage,
      onDone: _handleDisconnect,
      onError: (Object error) => _handleDisconnect(),
      cancelOnError: true,
    );
    // subscribe 帧在连接建立后由底层队列发送。
    channel.sink.add(
      jsonEncode(<String, Object?>{
        'type': 'subscribe',
        'sessionId': sessionId,
        'afterSequence': afterSequence,
      }),
    );
  }

  /// 断开处理：主动关闭不重连；意外断开按指数退避自动重连。
  ///
  /// onError 与 onDone 可能对同一次断开先后触发，用 [_reconnectPending]
  /// 保证只排一次重连。
  void _handleDisconnect() {
    _channel = null;
    if (_disposed || _closedByUser || _reconnectPending) {
      return;
    }
    final sessionId = _sessionId;
    if (sessionId == null) {
      return;
    }
    onDone?.call();
    final attempt = math.min(_reconnectAttempt, 20);
    final delay = reconnectDelay * (1 << attempt) > maxReconnectDelay
        ? maxReconnectDelay
        : reconnectDelay * (1 << attempt);
    _reconnectAttempt += 1;
    _reconnectPending = true;
    _reconnectTimer = Timer(delay, () {
      _reconnectPending = false;
      if (_disposed || _closedByUser || _channel != null) {
        return;
      }
      // 用最新游标重连；服务端据此续传缓冲或要求快照。
      _connect(sessionId, _lastSequence ?? 0);
    });
  }

  /// 主动关闭：不再自动重连；后续 subscribe 可恢复。
  void close() {
    _closedByUser = true;
    _reconnectPending = false;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _channel?.sink.close();
    _channel = null;
  }

  /// 销毁：关闭连接且不再重连。
  void dispose() {
    _disposed = true;
    _reconnectPending = false;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _channel?.sink.close();
    _channel = null;
  }

  void _handleMessage(Object? message) {
    final frame = parseAgentLiveFrame(message);
    if (frame is AgentSubscribedFrame) {
      // 服务端确认订阅成功：重置退避计数。
      _reconnectAttempt = 0;
    }
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
      event: AgentEventView.fromJson(json['event'] as Map<String, Object?>),
    ),
    'error' => AgentErrorFrame(
      code: json['code'] as String? ?? 'UNKNOWN',
      message: json['message'] as String? ?? '',
    ),
    _ => const AgentUnknownFrame(),
  };
}
