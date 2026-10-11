import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/security/credential_store.dart';
import '../ledger/ledger_workspace.dart';
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
class AgentCreating extends AgentUiState {
  const AgentCreating();
}

class AgentSessionActive extends AgentUiState {
  const AgentSessionActive({
    required this.availability,
    required this.session,
    required this.turnInProgress,
    this.accountLabel,
    this.pendingMessage,
    this.streamingText,
    this.errorMessage,
    this.toolCalls = const <AgentToolCallView>[],
  });

  final AgentAvailabilitySnapshotView availability;
  final AgentSessionView session;
  final bool turnInProgress;
  final String? accountLabel;

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
      accountLabel: accountLabel,
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
    // 身份变化即释放旧连接和会话，不能把前一个登录的数据带给下一个登录。
    ref.watch(
      sessionProvider.select((value) {
        final session = value.value;
        return session is SessionAuthenticated
            ? '${session.snapshot.serverUrl}/${session.snapshot.deviceId}'
            : null;
      }),
    );
    _repository = null;
    _live?.dispose();
    _live = null;
    ref.listen(sessionProvider, (previous, next) {
      final current = next.value;
      if (current is SessionAuthenticated) {
        _live?.updateAccessToken(current.snapshot.accessToken);
      }
    });
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
    // 等待会话控制器完成登录恢复；未登录则无从恢复。
    final sessionState = await ref.read(sessionProvider.future);
    if (sessionState is! SessionAuthenticated) {
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
    final credentialStore = ref
        .read(sessionDependenciesProvider)
        .credentialStore;
    final sessionId = await withCredentialMutation(credentialStore, store.read);
    if (sessionId == null || sessionId.isEmpty || state is! AgentIdle) {
      return;
    }
    final AgentSessionView session;
    try {
      session = await repository.getSession(sessionId);
    } on LyraApiError {
      // 会话已关闭或不存在：清掉本地 ID，回到创建视图。
      await withCredentialMutation(credentialStore, () async {
        if (ref.mounted && _repository == repository) await store.clear();
      });
      return;
    }
    if (!ref.mounted || _repository != repository || state is! AgentIdle) {
      return;
    }
    try {
      final availability = await repository.availability();
      if (!ref.mounted || _repository != repository || state is! AgentIdle) {
        return;
      }
      state = AgentSessionActive(
        availability: availability,
        session: session,
        turnInProgress:
            session.status == 'running' || session.status == 'cancelling',
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
    if (paired is! SessionAuthenticated) {
      return;
    }
    _lastSequence = 0;
    // 意外断开由 AgentLiveEvents 按指数退避自动重连（游标续传），
    // 控制器保留句柄，下一回合会带最新游标重新订阅。
    _live = AgentLiveEvents(
      serverUrl: paired.snapshot.serverUrl,
      accessToken: paired.snapshot.accessToken,
      onFrame: handleFrame,
    );
    _live!.subscribe(sessionId: sessionId, afterSequence: _lastSequence);
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
        if (event.sessionId != current.session.id) return;
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
        } else if (event.type == 'message.completed' ||
            event.type == 'message.failed' ||
            event.type == 'session.completed') {
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
      if (!ref.mounted || _repository != repository) {
        return;
      }
      final latest = state;
      if (latest is! AgentSessionActive ||
          latest.session.id != current.session.id) {
        return;
      }
      final running =
          session.status == 'running' || session.status == 'cancelling';
      state = latest.copyWith(
        session: session,
        turnInProgress: running,
        clearPendingMessage: !running,
        clearStreamingText: true,
      );
      // 权威快照变化后同步工具调用列表。
      unawaited(_refreshToolCalls());
    } on LyraApiError {
      // 快照刷新失败保持现有状态；下一帧或下次回合会再触发。
    }
  }

  /// 加载供应商可用性；未登录时保持 idle。
  Future<void> loadAvailability() async {
    final repository = _resolve();
    if (repository == null) {
      return;
    }
    try {
      await repository.availability();
      if (!ref.mounted || _repository != repository) return;
      // 成功则从错误态回到创建视图;可用性只决定创建按钮。
      if (state is AgentError) {
        state = const AgentIdle();
      }
    } on LyraApiError catch (error) {
      if (!ref.mounted || _repository != repository) return;
      state = AgentError('${error.message}（${error.code}）');
    }
  }

  /// 新对话读取当前页面账户范围；已有对话始终沿用自己的范围。
  Future<void> createSession() async {
    final current = state;
    if (current is AgentCreating ||
        (current is AgentSessionActive && current.turnInProgress)) {
      return;
    }
    final repository = _resolve();
    if (repository == null) {
      state = const AgentError('请先登录 Lyra');
      return;
    }
    final scope = ref.read(accountScopeProvider);
    state = const AgentCreating();
    try {
      final availability = await repository.availability();
      final session = await repository.createSession(
        accountScopeKind: scope.id == 'all' ? 'all' : 'account',
        accountId: scope.id == 'all' ? null : scope.id,
      );
      if (!ref.mounted || _repository != repository) return;
      state = AgentSessionActive(
        availability: availability,
        accountLabel: scope.label,
        session: session,
        turnInProgress: false,
      );
      await withCredentialMutation(
        ref.read(sessionDependenciesProvider).credentialStore,
        () async {
          if (_isCurrentSession(repository, session.id) &&
              ref.read(sessionProvider).value is SessionAuthenticated) {
            await ref.read(agentSessionStoreProvider).write(session.id);
          }
        },
      );
      if (!ref.mounted || _repository != repository) return;
      _subscribeLive(session.id);
      await _refreshToolCalls();
    } on LyraApiError catch (error) {
      if (!ref.mounted || _repository != repository) return;
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
      if (!ref.mounted || _repository != repository) {
        return;
      }
      final latest = state;
      if (latest is! AgentSessionActive ||
          latest.session.id != current.session.id) {
        return;
      }
      state = latest.copyWith(toolCalls: calls);
    } on LyraApiError {
      // 工具调用列表是增强信息；拉取失败保持现状。
    }
  }

  /// 发送用户回合；乐观上屏用户消息，终态快照替换为规范消息。
  Future<void> sendTurn(String content) async {
    final current = state;
    final repository = _repository;
    if (repository == null ||
        current is! AgentSessionActive ||
        current.turnInProgress ||
        current.session.status != 'idle') {
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
      final result = await repository.sendTurn(
        sessionId: current.session.id,
        content: content,
      );
      if (!_isCurrentSession(repository, current.session.id)) return;
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
      if (!_isCurrentSession(repository, current.session.id)) return;
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
    final repository = _repository;
    if (repository == null ||
        current is! AgentSessionActive ||
        !current.turnInProgress) {
      return;
    }
    try {
      final session = await repository.cancelTurn(current.session.id);
      if (!_isCurrentSession(repository, current.session.id)) return;
      state = current.copyWith(
        session: session,
        turnInProgress: false,
        clearPendingMessage: true,
        clearStreamingText: true,
      );
    } on LyraApiError catch (error) {
      if (!_isCurrentSession(repository, current.session.id)) return;
      state = current.copyWith(errorMessage: '${error.message}（${error.code}）');
    }
  }

  bool _isCurrentSession(AgentRepository repository, String sessionId) {
    if (!ref.mounted || _repository != repository) return false;
    final current = state;
    return current is AgentSessionActive && current.session.id == sessionId;
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
/// 未登录或加载失败不伪造金融内容；回合运行中显示乐观用户消息与思考标记。
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
    final paired = sessionState.value is SessionAuthenticated;

    // 作为推入的二级页运行,需要自备 Scaffold 提供 Material 上下文。
    return Scaffold(
      backgroundColor: LyraColors.canvas,
      appBar: !paired || state is AgentError
          ? AppBar(title: const Text('AI 分析'))
          : null,
      body: SafeArea(
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
        description: '登录后即可查询仓位、绩效与压力分析。',
        accent: LyraColors.accent,
      );
    }
    if (state is AgentError) {
      return _InfoCard(
        icon: LyraIcons.agent,
        title: 'AI 助手暂不可用',
        description: state.message,
        accent: LyraColors.accent,
        actionLabel: '重试',
        onAction: () =>
            ref.read(agentControllerProvider.notifier).loadAvailability(),
      );
    }
    if (state is AgentSessionActive) {
      return _SessionView(state: state);
    }
    return _CreateView(creating: state is AgentCreating);
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
    if (widget.state.turnInProgress || widget.state.session.status != 'idle') {
      return;
    }
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
    final canSend = !state.turnInProgress && session.status == 'idle';
    final controller = ref.read(agentControllerProvider.notifier);
    final selectedScope = ref.watch(accountScopeProvider);
    final scopeId = session.accountScopeKind == 'all'
        ? 'all'
        : session.accountId;
    final scopeLabel = session.accountScopeKind == 'all'
        ? '全部账号'
        : state.accountLabel ??
              (selectedScope.id == scopeId
                  ? selectedScope.label
                  : '账号 ${session.accountId}');
    return Column(
      children: <Widget>[
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 8, 8, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Row(
                children: <Widget>[
                  const BackButton(),
                  Expanded(
                    child: Text(
                      'Lyra 助手',
                      style: Theme.of(context).textTheme.headlineSmall
                          ?.copyWith(fontSize: 22, fontWeight: FontWeight.w500),
                    ),
                  ),
                  IconButton(
                    tooltip: '新对话',
                    onPressed: state.turnInProgress
                        ? null
                        : controller.createSession,
                    icon: const Icon(Icons.add_comment_outlined),
                  ),
                ],
              ),
              const SizedBox(height: 4),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 10,
                      runSpacing: 6,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        Text(
                          '本次对话：$scopeLabel',
                          key: const ValueKey('agent-session-scope'),
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                        _StatusChip(session: session),
                      ],
                    ),
                    if (selectedScope.id != scopeId) ...[
                      const SizedBox(height: 4),
                      Text(
                        '页面已选择${selectedScope.label}；新对话将使用该范围。',
                        style: Theme.of(context).textTheme.bodySmall
                            ?.copyWith(color: LyraColors.muted),
                      ),
                    ],
                    const SizedBox(height: 4),
                    Text(
                      '账本版本 ${session.ledgerVersion} · 截止 ${session.asOf}',
                      style: Theme.of(context).textTheme.bodySmall
                          ?.copyWith(color: LyraColors.muted),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            children: <Widget>[
              if (session.messages.isEmpty &&
                  state.pendingMessage == null &&
                  state.streamingText == null)
                _SessionEmptyState(
                  onSuggestion: (String suggestion) {
                    _inputController.text = suggestion;
                    _inputController.selection = TextSelection.fromPosition(
                      TextPosition(offset: suggestion.length),
                    );
                  },
                ),
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
                  child: _ThinkingDots(),
                ),
              if (state.errorMessage != null)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    state.errorMessage!,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                ),
              if (state.toolCalls.isNotEmpty) ...<Widget>[
                const SizedBox(height: 8),
                Text('工具调用', style: Theme.of(context).textTheme.titleSmall),
                const SizedBox(height: 6),
                for (final call in state.toolCalls) _ToolCallTile(call: call),
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
                    enabled: canSend,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    onSubmitted: (String value) => _send(),
                    decoration: InputDecoration(
                      hintText: session.status == 'closed'
                          ? '请新建对话继续分析'
                          : '询问持仓、绩效或风险…',
                      fillColor: LyraColors.surface,
                      isDense: true,
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                state.turnInProgress
                    ? IconButton(
                        onPressed: () => controller.cancelTurn(),
                        icon: const Icon(LyraIcons.dismiss),
                        tooltip: '取消回合',
                      )
                    : ValueListenableBuilder<TextEditingValue>(
                        valueListenable: _inputController,
                        builder:
                            (BuildContext context, TextEditingValue value, _) {
                              final bool hasInput = value.text
                                  .trim()
                                  .isNotEmpty;
                              return _SendButton(
                                enabled: canSend && hasInput,
                                onTap: canSend && hasInput ? _send : null,
                              );
                            },
                      ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

/// 一个 Lyra 助手；模型和运行方式由服务端统一配置。
class _CreateView extends ConsumerWidget {
  const _CreateView({required this.creating});
  final bool creating;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(accountScopeProvider);
    return ListView(
      padding: const EdgeInsets.fromLTRB(8, 4, 20, 40),
      children: <Widget>[
        Row(
          children: <Widget>[
            const BackButton(),
            const SizedBox(width: 4),
            Expanded(
              child: Text(
                'Lyra 助手',
                style: Theme.of(context).textTheme.headlineLarge?.copyWith(
                  fontSize: 22,
                  fontWeight: FontWeight.w500,
                  letterSpacing: 0,
                ),
              ),
            ),
          ],
        ),
        const Padding(
          padding: EdgeInsets.only(left: 60),
          child: Text(
            '从记录中，发现值得追问的细节。',
            style: TextStyle(color: LyraColors.muted, fontSize: 14),
          ),
        ),
        const SizedBox(height: 24),
        Padding(
          padding: const EdgeInsets.only(left: 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              Text(
                '分析范围：${scope.label}',
                style: Theme.of(context).textTheme.bodyMedium,
              ),
              const SizedBox(height: 12),
              FilledButton.icon(
                onPressed: creating
                    ? null
                    : () => ref
                          .read(agentControllerProvider.notifier)
                          .createSession(),
                icon: creating
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: LyraColors.inkSecondary,
                        ),
                      )
                    : const Icon(LyraIcons.agent, size: 20),
                label: Text(creating ? '正在创建…' : '开始分析'),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// 未登录与错误态共用的信息卡片。
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
        // 空状态只展示当前状态与下一步操作。
        Padding(
          padding: const EdgeInsets.only(top: 96),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.center,
            children: <Widget>[
              Container(
                width: 52,
                height: 52,
                alignment: Alignment.center,
                decoration: const BoxDecoration(
                  color: LyraColors.accentTint,
                  shape: BoxShape.circle,
                ),
                child: Icon(icon, color: accent, size: 24),
              ),
              const SizedBox(height: 18),
              Text(
                title,
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 8),
              Text(
                description,
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyMedium
                    ?.copyWith(color: LyraColors.muted, height: 1.5),
              ),
              if (actionLabel != null) ...<Widget>[
                const SizedBox(height: 20),
                FilledButton(onPressed: onAction, child: Text(actionLabel!)),
              ],
            ],
          ),
        ),
      ],
    );
  }
}

/// 空会话引导:不伪造对话内容,只提供可点的示例问题。
class _SessionEmptyState extends StatelessWidget {
  const _SessionEmptyState({required this.onSuggestion});

  final ValueChanged<String> onSuggestion;

  static const List<String> _suggestions = <String>[
    '我的持仓概览',
    '本月已实现损益',
    '即将到期的期权',
  ];

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 64),
      child: Column(
        children: <Widget>[
          Container(
            width: 52,
            height: 52,
            alignment: Alignment.center,
            decoration: const BoxDecoration(
              color: LyraColors.surfaceStrong,
              shape: BoxShape.circle,
            ),
            child: const Icon(
              LyraIcons.agent,
              size: 24,
              color: LyraColors.accent,
            ),
          ),
          const SizedBox(height: 16),
          Text('开始提问', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 6),
          Text(
            '助手根据本次对话的账户范围查询和分析账本。',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodySmall
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 20),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            alignment: WrapAlignment.center,
            children: <Widget>[
              for (final String suggestion in _suggestions)
                GestureDetector(
                  onTap: () => onSuggestion(suggestion),
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 14,
                      vertical: 9,
                    ),
                    decoration: BoxDecoration(
                      color: LyraColors.surface,
                      borderRadius: BorderRadius.circular(999),
                      border: Border.all(color: LyraColors.hairlineStrong),
                    ),
                    child: Text(
                      suggestion,
                      style: const TextStyle(
                        fontSize: 13,
                        fontWeight: FontWeight.w500,
                        color: LyraColors.ink,
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ],
      ),
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
          color: isUser ? LyraColors.accent : LyraColors.surface,
          borderRadius: BorderRadius.circular(16),
          border: isUser ? null : Border.all(color: LyraColors.hairline),
        ),
        child: Text(
          isStreaming ? '$content▍' : content,
          style: TextStyle(
            color: isUser ? LyraColors.onAccent : LyraColors.ink,
            height: 1.6,
          ),
        ),
      ),
    );
  }
}

/// 发送按钮:主色圆形实体按钮;输入为空时禁用并降低对比。
class _SendButton extends StatelessWidget {
  const _SendButton({required this.enabled, this.onTap});

  final bool enabled;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: '发送',
      child: InkWell(
        onTap: onTap,
        customBorder: const CircleBorder(),
        child: Container(
          width: 44,
          height: 44,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: enabled ? LyraColors.accent : LyraColors.surfaceStrong,
            shape: BoxShape.circle,
          ),
          child: Icon(
            LyraIcons.send,
            size: 20,
            color: enabled ? LyraColors.onAccent : LyraColors.mutedSoft,
          ),
        ),
      ),
    );
  }
}

/// "正在思考"指示器:三点错相呼吸,替代通用 spinner。
class _ThinkingDots extends StatefulWidget {
  const _ThinkingDots();

  @override
  State<_ThinkingDots> createState() => _ThinkingDotsState();
}

class _ThinkingDotsState extends State<_ThinkingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      children: <Widget>[
        Text(
          '正在思考',
          style: Theme.of(context).textTheme.bodyMedium
              ?.copyWith(color: LyraColors.muted),
        ),
        const SizedBox(width: 6),
        for (final int index in <int>[0, 1, 2])
          FadeTransition(
            opacity: Tween<double>(begin: 0.25, end: 1).animate(
              CurvedAnimation(
                parent: _controller,
                curve: Interval(
                  index * 0.22,
                  (index * 0.22 + 0.55).clamp(0.0, 1.0),
                  curve: Curves.easeInOut,
                ),
              ),
            ),
            child: Container(
              width: 4,
              height: 4,
              margin: const EdgeInsets.symmetric(horizontal: 2),
              decoration: const BoxDecoration(
                color: LyraColors.muted,
                shape: BoxShape.circle,
              ),
            ),
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
        color: LyraColors.surface,
        border: Border.all(color: LyraColors.hairlineStrong),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: Theme.of(context).textTheme.labelSmall
            ?.copyWith(color: LyraColors.ink, letterSpacing: 0),
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
        color: LyraColors.surfaceSubtle,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: LyraColors.hairline),
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
