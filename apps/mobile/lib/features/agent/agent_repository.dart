import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

/// `GET /v1/agents` 的单个供应商可用状态。
class AgentAvailabilityView {
  const AgentAvailabilityView({
    required this.provider,
    required this.available,
    this.version,
    this.unavailableReason,
  });

  final String provider;
  final bool available;
  final String? version;
  final String? unavailableReason;

  factory AgentAvailabilityView.fromJson(Map<String, Object?> json) {
    final status = json['status'] as String;
    final reason = json['reason'] as String?;
    if (status == 'available') {
      return AgentAvailabilityView(
        provider: json['provider'] as String,
        available: true,
        version: json['version'] as String?,
      );
    }
    return AgentAvailabilityView(
      provider: json['provider'] as String,
      available: false,
      unavailableReason: reason,
    );
  }
}

/// `GET /v1/agents` 可用性快照：输出 Lyra 助手的服务端可用状态。
class AgentAvailabilitySnapshotView {
  const AgentAvailabilitySnapshotView({required this.items});

  final List<AgentAvailabilityView> items;

  factory AgentAvailabilitySnapshotView.fromJson(Map<String, Object?> json) {
    final raw = json['providers'] as List<Object?>? ?? const <Object?>[];
    return AgentAvailabilitySnapshotView(
      items: raw
          .map(
            (item) =>
                AgentAvailabilityView.fromJson(item as Map<String, Object?>),
          )
          .toList(growable: false),
    );
  }
}

/// App 可展示的规范 Agent 消息；不包含隐藏推理或供应商协议状态。
class AgentMessageView {
  const AgentMessageView({
    required this.id,
    required this.role,
    required this.content,
    required this.createdAt,
    this.status,
    this.errorCode,
  });

  final String id;
  final String role;
  final String content;
  final String createdAt;
  final String? status;
  final String? errorCode;

  factory AgentMessageView.fromJson(Map<String, Object?> json) {
    return AgentMessageView(
      id: json['id'] as String,
      role: json['role'] as String,
      content: json['content'] as String,
      createdAt: json['createdAt'] as String,
      status: json['status'] as String?,
      errorCode: json['errorCode'] as String?,
    );
  }
}

/// App 权威会话快照；与 `PublicAgentSession` 投影字段一一对应。
class AgentSessionView {
  const AgentSessionView({
    required this.id,
    required this.provider,
    required this.status,
    required this.accountScopeKind,
    required this.accountId,
    required this.ledgerVersion,
    required this.asOf,
    required this.revision,
    required this.messages,
  });

  final String id;
  final String provider;
  final String status;
  final String accountScopeKind;
  final String? accountId;
  final int ledgerVersion;
  final String asOf;
  final int revision;
  final List<AgentMessageView> messages;

  factory AgentSessionView.fromJson(Map<String, Object?> json) {
    final scope = json['accountScope'] as Map<String, Object?>;
    final rawMessages = json['messages'] as List<Object?>? ?? const <Object?>[];
    return AgentSessionView(
      id: json['id'] as String,
      provider: json['provider'] as String,
      status: json['status'] as String,
      accountScopeKind: scope['kind'] as String,
      accountId: scope['account'] as String?,
      ledgerVersion: (json['ledgerVersion'] as num).toInt(),
      asOf: json['asOf'] as String,
      revision: (json['revision'] as num).toInt(),
      messages: rawMessages
          .map(
            (message) =>
                AgentMessageView.fromJson(message as Map<String, Object?>),
          )
          .toList(growable: false),
    );
  }
}

/// 会话内单条工具调用的安全公开视图；不含内部授权判定与会话 ID。
class AgentToolCallView {
  const AgentToolCallView({
    required this.id,
    required this.messageId,
    required this.ordinal,
    required this.toolName,
    required this.status,
    required this.argumentsSummary,
    required this.resultSummary,
    required this.ledgerVersion,
    required this.durationMs,
    required this.errorCode,
    required this.startedAt,
    required this.completedAt,
  });

  final String id;
  final String messageId;
  final int ordinal;
  final String toolName;
  final String status;
  final Map<String, Object?> argumentsSummary;
  final Map<String, Object?>? resultSummary;
  final int ledgerVersion;
  final int? durationMs;
  final String? errorCode;
  final String startedAt;
  final String? completedAt;

  factory AgentToolCallView.fromJson(Map<String, Object?> json) {
    Map<String, Object?>? parseSummary(Object? raw) {
      if (raw == null) {
        return null;
      }
      return (raw as Map).cast<String, Object?>();
    }

    return AgentToolCallView(
      id: json['id'] as String,
      messageId: json['messageId'] as String,
      ordinal: (json['ordinal'] as num).toInt(),
      toolName: json['toolName'] as String,
      status: json['status'] as String,
      argumentsSummary:
          parseSummary(json['argumentsSummary']) ?? const <String, Object?>{},
      resultSummary: parseSummary(json['resultSummary']),
      ledgerVersion: (json['ledgerVersion'] as num).toInt(),
      durationMs: json['durationMs'] == null
          ? null
          : (json['durationMs'] as num).toInt(),
      errorCode: json['errorCode'] as String?,
      startedAt: json['startedAt'] as String,
      completedAt: json['completedAt'] as String?,
    );
  }
}

/// Agent 数据层：可用性快照、会话创建、读取、回合与取消。
///
/// 复用 [LyraApiClient] 的协议头、令牌注入与 401 刷新重放；响应解析只消费
/// 服务端稳定投影字段，未知形态明确失败而不是猜测。
class AgentRepository {
  AgentRepository({required this._client});

  final LyraApiClient _client;

  /// 读取 Lyra 助手的可用状态快照。
  Future<AgentAvailabilitySnapshotView> availability() async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/agents',
      const <String, String>{},
    );
    return AgentAvailabilitySnapshotView.fromJson(_data(response));
  }

  /// 创建新会话；供应商不可用或账本冲突时抛出稳定错误。
  Future<AgentSessionView> createSession({
    required String accountScopeKind,
    String? accountId,
  }) async {
    final response = await _client.post<Map<String, Object?>>(
      '/v1/agent/sessions',
      <String, Object?>{
        'provider': 'genkit',
        'accountScope': <String, Object?>{
          'kind': accountScopeKind,
          if (accountScopeKind == 'account') 'account': accountId,
        },
      },
    );
    return AgentSessionView.fromJson(
      _data(response)['session'] as Map<String, Object?>,
    );
  }

  /// 读取会话权威快照；用于重连与恢复。
  Future<AgentSessionView> getSession(String sessionId) async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/agent/sessions/$sessionId',
      const <String, String>{},
    );
    return AgentSessionView.fromJson(
      _data(response)['session'] as Map<String, Object?>,
    );
  }

  /// 发送用户回合；阻塞到终态后返回最终快照与统一事件。
  Future<AgentTurnResultView> sendTurn({
    required String sessionId,
    required String content,
  }) async {
    // 服务端回合上限可配置到 10 分钟；事件流期间仍允许用户主动取消。
    final response = await _client.execute<Map<String, Object?>>(
      (dio) => dio.post<Map<String, Object?>>(
        '/v1/agent/sessions/$sessionId/messages',
        data: <String, Object?>{'content': content},
        options: Options(receiveTimeout: const Duration(minutes: 11)),
      ),
    );
    final body = _data(response);
    final rawEvents = body['events'] as List<Object?>? ?? const <Object?>[];
    return AgentTurnResultView(
      session: AgentSessionView.fromJson(
        body['session'] as Map<String, Object?>,
      ),
      events: rawEvents
          .map(
            (event) => AgentEventView.fromJson(event as Map<String, Object?>),
          )
          .toList(growable: false),
    );
  }

  /// 取消运行中的回合；非 running 状态返回稳定 409。
  Future<AgentSessionView> cancelTurn(String sessionId) async {
    final response = await _client.post<Map<String, Object?>>(
      '/v1/agent/sessions/$sessionId/cancel',
      <String, Object?>{},
    );
    return AgentSessionView.fromJson(
      _data(response)['session'] as Map<String, Object?>,
    );
  }

  /// 读取会话内工具调用公开列表；按开始时间稳定排序。
  Future<List<AgentToolCallView>> toolCalls(String sessionId) async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/agent/sessions/$sessionId/tool-calls',
      const <String, String>{},
    );
    final raw =
        _data(response)['toolCalls'] as List<Object?>? ?? const <Object?>[];
    return raw
        .map((call) => AgentToolCallView.fromJson(call as Map<String, Object?>))
        .toList(growable: false);
  }

  Map<String, Object?> _data(Response<Map<String, Object?>> response) {
    final body = response.data;
    if (body == null) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: 'Agent 响应格式无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return body;
  }
}

/// 一次回合的最终快照与统一事件序。
class AgentTurnResultView {
  const AgentTurnResultView({required this.session, required this.events});

  final AgentSessionView session;
  final List<AgentEventView> events;
}

/// 统一事件信封的客户端视图；只保留 App 需要展示的字段。
class AgentEventView {
  const AgentEventView({
    required this.type,
    required this.sessionId,
    required this.messageId,
    required this.sequence,
    required this.payload,
  });

  final String type;
  final String sessionId;
  final String? messageId;
  final int sequence;
  final Map<String, Object?> payload;

  factory AgentEventView.fromJson(Map<String, Object?> json) {
    return AgentEventView(
      type: json['type'] as String,
      sessionId: json['sessionId'] as String,
      messageId: json['messageId'] as String?,
      sequence: (json['sequence'] as num).toInt(),
      payload:
          (json['payload'] as Map<String, Object?>? ??
          const <String, Object?>{}),
    );
  }
}
