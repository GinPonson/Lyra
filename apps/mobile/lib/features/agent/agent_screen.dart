import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../agent/agent_live_events.dart';
import '../agent/agent_repository.dart';
import '../agent/agent_session_store.dart';

/// Agent 页状态：可用性 → 会话 → 回合。
sealed class AgentUiState {
  const AgentUiState();
}

/// 尚未加载可用性快照。
class AgentIdle extends AgentUiState {
  const AgentIdle();
}

/// 会话进行中：包含可用性、当前会话与回合运行标记。
class AgentSessionActive extends AgentUiState {
  const AgentSessionActive({
    required this.availability,
    required this.session,
    required this.turnInProgress,
    this.pendingMessage,
    this.streamingText,
    this.errorMessage,
    this.toolCalls = const <AgentToolCallView>[],
  });

  final AgentAvailabilitySnapshotView availability;
  final AgentSessionView session;
  final bool turnInProgress;

  /// 回合运行中已上屏的乐观用户消息；终态快照到达后被规范消息替代。
  final String? pendingMessage;

  /// 事件流 delta 累积的助手实时预览；快照到达后清空。
  final String? streamingText;
  final String? errorMessage;

  /// 会话内工具调用公开列表；按开始时间稳定排序。
  final List<AgentToolCallView> toolCalls;

  AgentSessionActive copyWith({
    AgentSessionView? session,
    bool? turnInProgress,
    String? pendingMessage,
    bool clearPendingMessage = false,
    String? streamingText,
    bool clearStreamingText = false,
    String? errorMessage,
    bool clearError = false,
    List<AgentToolCallView>? toolCalls,
  }) {
    return AgentSessionActive(
      availability: availability,
      session: session ?? this.session,
      turnInProgress: turnInProgress ?? this.turnInProgress,
      pendingMessage: clearPendingMessage
          ? null
          : (pendingMessage ?? this.pendingMessage),
      streamingText: clearStreamingText
          ? null
          : (streamingText ?? this.streamingText),
      errorMessage: clearError ? null : (errorMessage ?? this.errorMessage),
      toolCalls: toolCalls ?? this.toolCalls,
    );
  }
}

/// 可用性加载失败。
class AgentError extends AgentUiState {
  const AgentError(this.message);

  final String message;
}

/// Agent 页控制器：会话生命周期与回合交互。
class AgentController extends Notifier<AgentUiState> {
  AgentRepository? _repository;
  AgentLiveEvents? _live;
  int _lastSequence = 0;

  AgentRepository? _resolve() {
    if (_repository != null) {
      return _repository;
    }
    final client = ref.read(sessionProvider.notifier).client;
    if (client == null) {
      return null;
    }
    return _repository = AgentRepository(client: client);
  }

  @override
  AgentUiState build() {
    ref.onDispose(() {
      _live?.dispose();
      _live = null;
    });
    // 进入 Agent 页时尝试恢复上次活跃会话；失败静默回到创建视图。
    Future<void>.microtask(_restoreLastSession);
    return const AgentIdle();
  }

  /// 读取本地会话 ID 并用服务端权威快照恢复会话视图。
  Future<void> _restoreLastSession() async {
    if (state is! AgentIdle) {
      return;
    }
    // 等待会话控制器完成配对恢复；未配对则无从恢复。
    final sessionState = await ref.read(sessionProvider.future);
    if (sessionState is! SessionPaired) {
      return;
    }
    if (state is! AgentIdle) {
      return;
    }
    final repository = _resolve();
    if (repository == null) {
      return;
    }
    final store = ref.read(agentSessionStoreProvider);
    final sessionId = await store.read();
    if (sessionId == null || sessionId.isEmpty || state is! AgentIdle) {
      return;
    }
    final AgentSessionView session;
    try {
      session = await repository.getSession(sessionId);
    } on LyraApiError {
      // 会话已关闭或不存在：清掉本地 ID，回到创建视图。
      await store.clear();
      return;
    }
    if (!ref.mounted || state is! AgentIdle) {
      return;
    }
    try {
      final availability = await repository.availability();
      if (!ref.mounted || state is! AgentIdle) {
        return;
      }
      state = AgentSessionActive(
        availability: availability,
        session: session,
        turnInProgress: false,
      );
      _subscribeLive(session.id);
      await _refreshToolCalls();
    } on LyraApiError {
      // 可用性暂不可得：保留本地 ID，下次进入再试。
      return;
    }
  }

  /// 订阅会话事件流；游标从 0 开始，续传与实时事件统一进入帧处理。
  void _subscribeLive(String sessionId) {
    _live?.dispose();
    _live = null;
    final sessionState = ref.read(sessionProvider);
    final paired = sessionState.value;
    if (paired is! SessionPaired) {
      return;
    }
    _lastSequence = 0;
    AgentLiveEvents? live;
    live = AgentLiveEvents(
      serverUrl: paired.snapshot.serverUrl,
      accessToken: paired.snapshot.accessToken,
      onFrame: handleFrame,
      onDone: () {
        // 连接断开：保留游标，等待下一次回合或手动交互时重订阅。
        if (identical(_live, live)) {
          _live = null;
        }
      },
    );
    _live = live;
    live.subscribe(sessionId: sessionId, afterSequence: _lastSequence);
  }

  /// 统一事件帧处理：delta 实时预览，缺口或要求时重取权威快照。
  ///
  /// 公开为事件入口以便测试直接注入帧序列。
  void handleFrame(AgentLiveFrame frame) {
    final current = state;
    if (current is! AgentSessionActive) {
      return;
    }
    switch (frame) {
      case AgentEventFrame(:final event):
        if (event.sequence <= _lastSequence) {
          return; // 重复事件：忽略。
        }
        if (event.sequence > _lastSequence + 1) {
          _refreshSnapshot('sequence-gap');
          return;
        }
        _lastSequence = event.sequence;
        if (event.type == 'message.delta') {
          final text = event.payload['text'];
          if (text is String && text.isNotEmpty) {
            state = current.copyWith(
              streamingText: (current.streamingText ?? '') + text,
            );
          }
        } else if (event.type == 'message.completed'
            || event.type == 'message.failed'
            || event.type == 'session.completed') {
          state = current.copyWith(clearStreamingText: true);
          _refreshSnapshot('terminal-${event.type}');
        }
      case AgentSubscribedFrame(:final mode):
        if (mode == 'snapshot_required') {
          _refreshSnapshot('subscribed-snapshot');
        }
      case AgentErrorFrame():
        state = current.copyWith(
          errorMessage: '${frame.message}（${frame.code}）',
        );
      case AgentUnknownFrame():
        break;
    }
  }

  /// 从服务端重取权威会话快照；事件流只负责提示，快照永远是唯一事实。
  Future<void> _refreshSnapshot(String reason) async {
    final repository = _resolve();
    final current = state;
    if (repository == null || current is! AgentSessionActive) {
      return;
    }
    try {
      final session = await repository.getSession(current.session.id);
      if (!ref.mounted) {
        return;
      }
      final latest = state;
      if (latest is! AgentSessionActive) {
        return;
      }
      state = latest.copyWith(
        session: session,
        clearStreamingText: true,
      );
      // 权威快照变化后同步工具调用列表。
      unawaited(_refreshToolCalls());
    } on LyraApiError {
      // 快照刷新失败保持现有状态；下一帧或下次回合会再触发。
    }
  }

  /// 加载供应商可用性；未配对时保持 idle。
  Future<void> loadAvailability() async {
    final repository = _resolve();
    if (repository == null) {
      return;
    }
    try {
      await repository.availability();
      // 可用性只决定创建按钮；真正失败在创建时以稳定错误码暴露。
    } on LyraApiError catch (error) {
      state = AgentError('${error.message}（${error.code}）');
    }
  }

  /// 创建新会话（scope=all）；供应商不可用或账本冲突以稳定错误码显示。
  Future<void> createSession(String provider) async {
    final repository = _resolve();
    if (repository == null) {
      state = const AgentError('请先完成设备配对');
      return;
    }
    try {
      final availability = await repository.availability();
      final session = await repository.createSession(
        provider: provider,
        accountScopeKind: 'all',
      );
      state = AgentSessionActive(
        availability: availability,
        session: session,
        turnInProgress: false,
      );
      await ref.read(agentSessionStoreProvider).write(session.id);
      _subscribeLive(session.id);
      await _refreshToolCalls();
    } on LyraApiError catch (error) {
      state = AgentError('${error.message}（${error.code}）');
    }
  }

  /// 拉取会话工具调用公开列表；失败不阻塞会话流。
  Future<void> _refreshToolCalls() async {
    final repository = _repository;
    final current = state;
    if (repository == null || current is! AgentSessionActive) {
      return;
    }
    try {
      final calls = await repository.toolCalls(current.session.id);
      if (!ref.mounted) {
        return;
      }
      final latest = state;
      if (latest is! AgentSessionActive) {
        return;
      }
      state = latest.copyWith(toolCalls: calls);
    } on LyraApiError {
      // 工具调用列表是增强信息；拉取失败保持现状。
    }
  }

  /// 在回合边界把会话切换到目标供应商；运行中或目标不可用返回稳定 409。
  Future<void> switchProvider(String targetProvider) async {
    final current = state;
    if (current is! AgentSessionActive || current.turnInProgress) {
      return;
    }
    if (current.session.provider == targetProvider) {
      return;
    }
    try {
      final session = await _repository!.switchSession(
        sessionId: current.session.id,
        targetProvider: targetProvider,
      );
      state = current.copyWith(
        session: session,
        clearError: true,
      );
    } on LyraApiError catch (error) {
      state = current.copyWith(
        errorMessage: '${error.message}（${error.code}）',
      );
    }
  }

  /// 发送用户回合；乐观上屏用户消息，终态快照替换为规范消息。
  Future<void> sendTurn(String content) async {
    final current = state;
    if (current is! AgentSessionActive || current.turnInProgress) {
      return;
    }
    // 回合窗口重连事件流，保证本回合增量不缺。
    if (_live == null) {
      _subscribeLive(current.session.id);
    } else {
      _live!.subscribe(
        sessionId: current.session.id,
        afterSequence: _lastSequence,
      );
    }
    state = current.copyWith(
      turnInProgress: true,
      pendingMessage: content,
      clearError: true,
    );
    try {
      final result = await _repository!.sendTurn(
        sessionId: current.session.id,
        content: content,
      );
      final maxSequence = result.events.isEmpty
          ? _lastSequence
          : result.events.map((event) => event.sequence).reduce(math.max);
      _lastSequence = math.max(_lastSequence, maxSequence);
      state = current.copyWith(
        session: result.session,
        turnInProgress: false,
        clearPendingMessage: true,
        clearStreamingText: true,
      );
      await _refreshToolCalls();
    } on LyraApiError catch (error) {
      state = current.copyWith(
        turnInProgress: false,
        clearPendingMessage: true,
        clearStreamingText: true,
        errorMessage: '${error.message}（${error.code}）',
      );
    }
  }

  /// 取消运行中的回合。
  Future<void> cancelTurn() async {
    final current = state;
    if (current is! AgentSessionActive || !current.turnInProgress) {
      return;
    }
    try {
      final session = await _repository!.cancelTurn(current.session.id);
      state = current.copyWith(
        session: session,
        turnInProgress: false,
        clearPendingMessage: true,
        clearStreamingText: true,
      );
    } on LyraApiError catch (error) {
      state = current.copyWith(
        errorMessage: '${error.message}（${error.code}）',
      );
    }
  }
}

/// Agent 页 Provider（进入页面时创建，离开销毁）。
final agentControllerProvider =
    NotifierProvider.autoDispose<AgentController, AgentUiState>(
  AgentController.new,
);

/// Agent 活跃会话 ID 持久化；由会话依赖注入，测试可覆盖。
final Provider<AgentSessionStore> agentSessionStoreProvider =
    Provider<AgentSessionStore>((ref) {
  return ref.watch(sessionDependenciesProvider).agentSessionStore ??
      const SecureAgentSessionStore();
});

/// Agent 页：供应商可用状态、会话消息流与回合输入。
///
/// 未配对或加载失败不伪造金融内容；回合运行中显示乐观用户消息与思考标记。
class AgentScreen extends ConsumerStatefulWidget {
  const AgentScreen({super.key});

  @override
  ConsumerState<AgentScreen> createState() => _AgentScreenState();
}

class _AgentScreenState extends ConsumerState<AgentScreen> {
  @override
  Widget build(BuildContext context) {
    final state = ref.watch(agentControllerProvider);
    final sessionState = ref.watch(sessionProvider);
    final paired = sessionState.value is SessionPaired;

    return ColoredBox(
      color: LyraColors.black,
      child: SafeArea(
        bottom: false,
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 920),
            child: _buildBody(context, state, paired),
          ),
        ),
      ),
    );
  }

  Widget _buildBody(BuildContext context, AgentUiState state, bool paired) {
    if (!paired) {
      return _InfoCard(
        icon: LyraIcons.agent,
        title: '未连接服务端',
        description: '完成设备配对后即可与只读金融 Agent 对话。',
        accent: LyraColors.brandBright,
      );
    }
    if (state is AgentError) {
      return _InfoCard(
        icon: LyraIcons.agent,
        title: 'Agent 暂不可用',
        description: state.message,
        accent: LyraColors.brandBright,
        actionLabel: '重试',
        onAction: () => ref.read(agentControllerProvider.notifier).loadAvailability(),
      );
    }
    if (state is AgentSessionActive) {
      return _SessionView(state: state);
    }
    return const _CreateView();
  }
}

/// 会话进行中的消息流与输入区。
class _SessionView extends ConsumerStatefulWidget {
  const _SessionView({required this.state});

  final AgentSessionActive state;

  @override
  ConsumerState<_SessionView> createState() => _SessionViewState();
}

class _SessionViewState extends ConsumerState<_SessionView> {
  final TextEditingController _inputController = TextEditingController();

  @override
  void dispose() {
    _inputController.dispose();
    super.dispose();
  }

  void _send() {
    final content = _inputController.text.trim();
    if (content.isEmpty) {
      return;
    }
    _inputController.clear();
    ref.read(agentControllerProvider.notifier).sendTurn(content);
  }

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    final session = state.session;
    final controller = ref.read(agentControllerProvider.notifier);
    return Column(
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 30, 20, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                'AGENT / READ ONLY',
                style: Theme.of(context).textTheme.labelSmall
                    ?.copyWith(color: LyraColors.brandBright),
              ),
              const SizedBox(height: 6),
              Row(
                children: <Widget>[
                  Expanded(
                    child: Text(
                      'Agent · ${_providerLabel(session.provider)}',
                      style: Theme.of(context).textTheme.headlineSmall,
                    ),
                  ),
                  _StatusChip(session: session),
                  const SizedBox(width: 8),
                  _SwitchMenu(state: state),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                '账本版本 ${session.ledgerVersion} · 截止 ${session.asOf}',
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(color: LyraColors.muted),
              ),
            ],
          ),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            children: <Widget>[
              for (final message in session.messages)
                _MessageBubble(
                  role: message.role,
                  content: message.content,
                  isFailed: message.status == 'failed',
                ),
              if (state.pendingMessage != null)
                _MessageBubble(role: 'user', content: state.pendingMessage!),
              if (state.streamingText != null)
                _MessageBubble(
                  role: 'assistant',
                  content: state.streamingText!,
                  isStreaming: true,
                ),
              if (state.turnInProgress && state.streamingText == null)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 10),
                  child: Row(
                    children: <Widget>[
                      SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                      SizedBox(width: 10),
                      Text('正在思考…'),
                    ],
                  ),
                ),
              if (state.errorMessage != null)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    state.errorMessage!,
                    style: TextStyle(color: Theme.of(context).colorScheme.error),
                  ),
                ),
              if (state.toolCalls.isNotEmpty) ...<Widget>[
                const SizedBox(height: 8),
                Text(
                  '工具调用',
                  style: Theme.of(context).textTheme.titleSmall,
                ),
                const SizedBox(height: 6),
                for (final call in state.toolCalls)
                  _ToolCallTile(call: call),
              ],
              const SizedBox(height: 12),
            ],
          ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 14),
            child: Row(
              children: <Widget>[
                Expanded(
                  child: TextField(
                    controller: _inputController,
                    enabled: !state.turnInProgress,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (String value) => _send(),
                    decoration: const InputDecoration(
                      hintText: '询问持仓、绩效或风险…',
                      border: OutlineInputBorder(),
                      isDense: true,
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                state.turnInProgress
                    ? IconButton(
                        onPressed: () => controller.cancelTurn(),
                        icon: const Icon(LyraIcons.disclosure),
                        tooltip: '取消回合',
                      )
                    : IconButton(
                        onPressed: _send,
                        icon: const Icon(LyraIcons.agent),
                        tooltip: '发送',
                      ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

/// 会话创建视图：固定展示三个供应商入口（与设计一致的固定顺序）。
class _CreateView extends ConsumerWidget {
  const _CreateView();

  static const List<(String, String)> _providers = <(String, String)>[
    ('claude', 'Claude'),
    ('pi', 'Pi'),
    ('opencode', 'OpenCode'),
  ];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 30, 20, 40),
      children: <Widget>[
        Text(
          'AGENT / READ ONLY',
          style: Theme.of(context).textTheme.labelSmall
              ?.copyWith(color: LyraColors.brandBright),
        ),
        const SizedBox(height: 14),
        Text('Agent', style: Theme.of(context).textTheme.headlineLarge),
        const SizedBox(height: 14),
        Text(
          'Claude、Pi 与 OpenCode 共用只读金融工具，并在回合边界显式切换。'
          'Agent 只能查询经过账户范围和账本版本校验的数据。',
          style: Theme.of(context).textTheme.bodyLarge
              ?.copyWith(color: LyraColors.muted, height: 1.55),
        ),
        const SizedBox(height: 28),
        for (final (provider, label) in _providers)
          Container(
            margin: const EdgeInsets.only(bottom: 12),
            child: OutlinedButton.icon(
              onPressed: () => ref
                  .read(agentControllerProvider.notifier)
                  .createSession(provider),
              icon: const Icon(LyraIcons.agent),
              label: Text('开始 $label 会话'),
            ),
          ),
      ],
    );
  }
}

/// 未配对与错误态共用的信息卡片。
class _InfoCard extends StatelessWidget {
  const _InfoCard({
    required this.icon,
    required this.title,
    required this.description,
    required this.accent,
    this.actionLabel,
    this.onAction,
  });

  final IconData icon;
  final String title;
  final String description;
  final Color accent;
  final String? actionLabel;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 30, 20, 40),
      children: <Widget>[
        Container(
          padding: const EdgeInsets.all(24),
          decoration: BoxDecoration(
            color: LyraColors.elevated,
            borderRadius: BorderRadius.circular(32),
            border: Border.all(color: LyraColors.divider),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Container(
                width: 52,
                height: 52,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: accent.withValues(alpha: 0.18),
                  shape: BoxShape.circle,
                ),
                child: Icon(icon, color: accent),
              ),
              const SizedBox(height: 24),
              Text(title, style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 8),
              Text(
                description,
                style: Theme.of(context).textTheme.bodyMedium
                    ?.copyWith(color: LyraColors.muted),
              ),
              if (actionLabel != null) ...<Widget>[
                const SizedBox(height: 16),
                OutlinedButton(onPressed: onAction, child: Text(actionLabel!)),
              ],
            ],
          ),
        ),
      ],
    );
  }
}

/// 单条会话消息气泡。
class _MessageBubble extends StatelessWidget {
  const _MessageBubble({
    required this.role,
    required this.content,
    this.isFailed = false,
    this.isStreaming = false,
  });

  final String role;
  final String content;
  final bool isFailed;
  final bool isStreaming;

  @override
  Widget build(BuildContext context) {
    final isUser = role == 'user';
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 6),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        constraints: const BoxConstraints(maxWidth: 560),
        decoration: BoxDecoration(
          color: isUser ? LyraColors.brandPrimary : LyraColors.elevated,
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: isStreaming ? LyraColors.brandBright : LyraColors.divider,
          ),
        ),
        child: Text(
          isStreaming ? '$content▍' : content,
          style: TextStyle(
            color: isUser ? Colors.white : LyraColors.ink,
            height: 1.5,
          ),
        ),
      ),
    );
  }
}

/// 回合边界切换菜单：仅 idle 且目标为其他供应商时可用。
class _SwitchMenu extends ConsumerWidget {
  const _SwitchMenu({required this.state});

  final AgentSessionActive state;

  static const List<(String, String)> _providers = <(String, String)>[
    ('claude', 'Claude'),
    ('pi', 'Pi'),
    ('opencode', 'OpenCode'),
  ];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final canSwitch = !state.turnInProgress
        && state.session.status == 'idle';
    return PopupMenuButton<String>(
      tooltip: '切换 Agent',
      enabled: canSwitch,
      icon: const Icon(LyraIcons.disclosure),
      onSelected: (String target) => ref
          .read(agentControllerProvider.notifier)
          .switchProvider(target),
      itemBuilder: (BuildContext context) => <PopupMenuEntry<String>>[
        for (final (provider, label) in _providers)
          if (provider != state.session.provider)
            PopupMenuItem<String>(
              value: provider,
              child: Text('切换到 $label'),
            ),
      ],
    );
  }
}

/// 会话状态芯片：idle / running / closed / cancelling。
class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.session});

  final AgentSessionView session;

  @override
  Widget build(BuildContext context) {
    final label = switch (session.status) {
      'running' => '回合中',
      'cancelling' => '取消中',
      'closed' => '已关闭',
      _ => '空闲',
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: LyraColors.elevated,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: LyraColors.divider),
      ),
      child: Text(
        label,
        style: Theme.of(context).textTheme.labelSmall,
      ),
    );
  }
}

/// 单条工具调用的折叠卡片；只显示公开摘要，不伪造结果。
class _ToolCallTile extends StatelessWidget {
  const _ToolCallTile({required this.call});

  final AgentToolCallView call;

  @override
  Widget build(BuildContext context) {
    final statusLabel = switch (call.status) {
      'started' => '运行中',
      'completed' => '完成',
      'failed' => '失败',
      'cancelled' => '已取消',
      'rejected' => '被拒绝',
      _ => call.status,
    };
    final arguments = call.argumentsSummary.entries
        .map((entry) => '${entry.key}: ${entry.value}')
        .join('，');
    final result = call.resultSummary?.entries
        .map((entry) => '${entry.key}: ${entry.value}')
        .join('，');
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: LyraColors.elevated,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: LyraColors.divider),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              Expanded(
                child: Text(
                  call.toolName,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              Text(
                call.durationMs == null
                    ? statusLabel
                    : '$statusLabel · ${call.durationMs}ms',
                style: Theme.of(context).textTheme.labelSmall
                    ?.copyWith(color: LyraColors.muted),
              ),
            ],
          ),
          if (arguments.isNotEmpty) ...<Widget>[
            const SizedBox(height: 4),
            Text(
              '参数：$arguments',
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: LyraColors.muted),
            ),
          ],
          if (result != null && result.isNotEmpty) ...<Widget>[
            const SizedBox(height: 2),
            Text(
              '结果：$result',
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: LyraColors.muted),
            ),
          ],
          if (call.errorCode != null) ...<Widget>[
            const SizedBox(height: 2),
            Text(
              call.errorCode!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
        ],
      ),
    );
  }
}

/// 供应商显示名映射。
String _providerLabel(String provider) {
  return switch (provider) {
    'claude' => 'Claude',
    'pi' => 'Pi',
    'opencode' => 'OpenCode',
    _ => provider,
  };
}
