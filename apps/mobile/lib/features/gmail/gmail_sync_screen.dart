import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_error_message.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_widgets.dart';
import '../accounts/account_repository.dart';
import '../accounts/account_sheet.dart';
import '../accounts/account_workspace.dart';
import '../market/market_history_workspace.dart';
import '../market/market_underlying_quote_workspace.dart';
import '../market/market_workspace.dart';
import '../overview/login_panel.dart';
import '../profile/profile_widgets.dart';
import 'gmail_models.dart';
import 'gmail_workspace.dart';

/// 管理当前用户 Gmail 授权、嘉信邮件扫描范围与证券账户同步。
class GmailSyncScreen extends ConsumerStatefulWidget {
  const GmailSyncScreen({super.key});

  @override
  ConsumerState<GmailSyncScreen> createState() => _GmailSyncScreenState();
}

class _GmailSyncScreenState extends ConsumerState<GmailSyncScreen> {
  LyraApiClient? _observedClient;
  CancelToken? _cancelToken;
  int _operationGeneration = 0;
  bool _busy = false;
  bool _refreshing = false;
  String? _busyMessage;
  String? _pageError;
  String? _selectedAccountId;
  final Map<String, bool> _rescanModes = <String, bool>{};
  final Map<String, _GmailScanRange> _scanRanges = <String, _GmailScanRange>{};
  final Map<String, DateTime> _scanDates = <String, DateTime>{};
  final Map<String, GmailSyncResult> _results = <String, GmailSyncResult>{};
  final Map<String, String> _actionErrors = <String, String>{};

  @override
  void initState() {
    super.initState();
    _observedClient = ref.read(sessionProvider.notifier).client;
  }

  @override
  void dispose() {
    _operationGeneration++;
    _cancelToken?.cancel('Gmail 页面已关闭');
    super.dispose();
  }

  /// 丢弃旧登录身份的页面选择与迟到操作结果。
  void _observeClient(LyraApiClient? client) {
    if (identical(client, _observedClient)) return;
    _operationGeneration++;
    _cancelToken?.cancel('Lyra 登录状态已变化');
    _cancelToken = null;
    _observedClient = client;
    setState(() {
      _busy = false;
      _refreshing = false;
      _busyMessage = null;
      _pageError = null;
      _selectedAccountId = null;
      _rescanModes.clear();
      _scanRanges.clear();
      _scanDates.clear();
      _results.clear();
      _actionErrors.clear();
    });
  }

  /// 判断异步动作仍属于当前页面及其启动时的 Lyra 会话。
  bool _isActive(LyraApiClient client, int generation) =>
      mounted &&
      generation == _operationGeneration &&
      identical(client, ref.read(sessionProvider.notifier).client);

  /// 启动一次不会被页面按钮重复触发的 Gmail 操作。
  ({int generation, CancelToken cancelToken})? _beginOperation({
    required String message,
  }) {
    final client = ref.read(gmailAuthenticatedClientProvider);
    if (_busy ||
        _refreshing ||
        client == null ||
        !identical(client, _observedClient)) {
      return null;
    }
    final generation = ++_operationGeneration;
    final cancelToken = _cancelToken = CancelToken();
    setState(() {
      _busy = true;
      _busyMessage = message;
      _pageError = null;
    });
    return (generation: generation, cancelToken: cancelToken);
  }

  /// 重新读取连接与账户，保留当前已确认的扫描日期。
  Future<void> _refresh(LyraApiClient client) async {
    if (_busy || _refreshing) return;
    setState(() {
      _refreshing = true;
      _pageError = null;
    });
    try {
      await Future.wait<Object?>([
        ref.refresh(gmailConnectionsProvider(client).future),
        ref.refresh(brokerageAccountsProvider.future),
      ]);
    } on Object catch (error) {
      if (!mounted ||
          !identical(client, ref.read(gmailAuthenticatedClientProvider))) {
        return;
      }
      setState(() => _pageError = _errorMessage(error));
    } finally {
      if (mounted &&
          identical(client, ref.read(gmailAuthenticatedClientProvider))) {
        setState(() => _refreshing = false);
      }
    }
  }

  /// 创建 Gmail 授权事务并等待系统授权窗口返回后的服务端确认。
  Future<void> _authorize() async {
    final action = _beginOperation(message: '正在连接 Gmail…');
    final client = _observedClient;
    if (action == null || client == null) return;
    try {
      await ref
          .read(gmailRepositoryProvider)
          .authorize(cancelToken: action.cancelToken);
      if (!_isActive(client, action.generation)) return;
      ref.invalidate(gmailConnectionsProvider(client));
    } on Object catch (error) {
      if (!_isActive(client, action.generation)) return;
      setState(() => _pageError = _errorMessage(error));
    } finally {
      if (_isActive(client, action.generation)) {
        setState(() {
          _busy = false;
          _busyMessage = null;
        });
        _cancelToken = null;
      }
    }
  }

  /// 让用户选择真实日历日期；取消日期选择时保留此前的范围确认。
  Future<void> _chooseScanDate(GmailConnection connection) async {
    final today = DateUtils.dateOnly(DateTime.now());
    final previous = _scanDates[connection.id];
    final selected = await showDatePicker(
      context: context,
      initialDate: previous ?? today,
      firstDate: DateTime(2004, 1, 1),
      lastDate: today,
      helpText: '选择扫描起始日期',
      cancelText: '取消',
      confirmText: '确认日期',
      initialEntryMode: DatePickerEntryMode.calendar,
    );
    if (selected == null || !mounted) return;
    setState(() {
      _scanRanges[connection.id] = _GmailScanRange.customDate;
      _scanDates[connection.id] = DateUtils.dateOnly(selected);
      _actionErrors.remove(connection.id);
    });
  }

  /// 记录用户明确选择全部嘉信邮件，并清除可能存在的自定义日期。
  void _selectAllScanRange(GmailConnection connection) {
    setState(() {
      _scanRanges[connection.id] = _GmailScanRange.all;
      _scanDates.remove(connection.id);
      _actionErrors.remove(connection.id);
    });
  }

  /// 为 active 连接启用显式重扫模式，供用户修复导入顺序或账户映射后重试。
  void _startRescan(GmailConnection connection) {
    setState(() {
      _rescanModes[connection.id] = true;
      _scanRanges.remove(connection.id);
      _scanDates.remove(connection.id);
      _actionErrors.remove(connection.id);
    });
  }

  /// 退出重扫模式，恢复不带扫描范围的增量同步。
  void _cancelRescan(GmailConnection connection) {
    setState(() {
      _rescanModes.remove(connection.id);
      _scanRanges.remove(connection.id);
      _scanDates.remove(connection.id);
    });
  }

  /// 把该连接的邮件同步到用户选中的既有证券账户并刷新当前账本读模型。
  Future<void> _sync(
    GmailConnection connection,
    BrokerageAccount? account,
  ) async {
    final scanRange = _scanRanges[connection.id];
    final selectedDate = _scanDates[connection.id];
    final rescanMode =
        connection.status.requiresRescanRange ||
        (connection.status == GmailConnectionStatus.active &&
            _rescanModes[connection.id] == true);
    if (account == null) {
      setState(() => _actionErrors[connection.id] = '请先选择一个现有证券账户。');
      return;
    }
    if (rescanMode &&
        (scanRange == null ||
            (scanRange == _GmailScanRange.customDate &&
                selectedDate == null))) {
      setState(() {
        _actionErrors[connection.id] = '请先选择并确认扫描范围。';
      });
      return;
    }
    final action = _beginOperation(message: '正在同步…');
    final client = _observedClient;
    if (action == null || client == null) return;
    final container = ProviderScope.containerOf(context, listen: false);
    setState(() {
      // 上次响应不能证明本次写入结果，避免失败后仍显示旧成功统计。
      _results.remove(connection.id);
      _actionErrors.remove(connection.id);
    });
    try {
      final result = await ref
          .read(gmailRepositoryProvider)
          .sync(
            connectionId: connection.id,
            explicitAccountId: account.id,
            status: connection.status,
            scanAll: rescanMode ? scanRange == _GmailScanRange.all : null,
            confirmedRescanAfterDate:
                scanRange == _GmailScanRange.customDate && selectedDate != null
                ? _localDate(selectedDate)
                : null,
            cancelToken: action.cancelToken,
          );
      if (!_isActive(client, action.generation)) return;
      setState(() {
        _results[connection.id] = result;
        _actionErrors.remove(connection.id);
        // 下次首次扫描或重扫必须由用户重新确认扫描范围。
        _rescanModes.remove(connection.id);
        _scanRanges.remove(connection.id);
        _scanDates.remove(connection.id);
      });
    } on Object catch (error) {
      if (!_isActive(client, action.generation)) return;
      final uncertain = error is LyraApiError && error.statusCode == 0;
      setState(() {
        _actionErrors[connection.id] = uncertain
            ? '同步请求没有收到服务端确认，账本是否已更新尚不确定。请刷新连接状态后，再决定是否显式重试。'
            : _errorMessage(error);
      });
    } finally {
      if (_isActive(client, action.generation)) {
        // 服务端逐封提交，后续校验失败时前面的交易可能已经入账。
        // 两种结局均刷新同一批读模型；这里只发读取请求，不重放同步。
        ref.invalidate(gmailConnectionsProvider(client));
        ref.invalidate(marketPositionsProvider);
        ref.invalidate(marketHistoryProvider);
        ref.invalidate(marketUnderlyingQuoteProvider);
        try {
          await refreshLedgerWorkspace(container: container, client: client);
        } on Object {
          // 保留连接旁的真实写入错误，另行报告读取失败，避免覆盖同步结果。
          if (_isActive(client, action.generation)) {
            setState(() => _pageError = '账本刷新失败，请稍后手动刷新。');
          }
        }
      }
      if (_isActive(client, action.generation)) {
        setState(() {
          _busy = false;
          _busyMessage = null;
        });
        _cancelToken = null;
      }
    }
  }

  /// 请求用户确认撤销 Google 授权，成功后再从当前连接列表移除。
  Future<void> _disconnect(GmailConnection connection) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('断开 ${connection.googleEmail}？'),
        content: const Text('将撤销该邮箱的只读授权，并停止后续邮件同步。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('保留连接'),
          ),
          FilledButton(
            key: const ValueKey('gmail-disconnect-confirm'),
            onPressed: () => Navigator.pop(context, true),
            child: const Text('断开并撤销'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    final action = _beginOperation(message: '正在断开 Gmail…');
    final client = _observedClient;
    if (action == null || client == null) return;
    try {
      await ref
          .read(gmailRepositoryProvider)
          .disconnect(
            connectionId: connection.id,
            cancelToken: action.cancelToken,
          );
      if (!_isActive(client, action.generation)) return;
      ref.invalidate(gmailConnectionsProvider(client));
      setState(() {
        _results.remove(connection.id);
        _actionErrors.remove(connection.id);
        _rescanModes.remove(connection.id);
        _scanRanges.remove(connection.id);
        _scanDates.remove(connection.id);
      });
    } on Object catch (error) {
      if (!_isActive(client, action.generation)) return;
      setState(() => _actionErrors[connection.id] = _errorMessage(error));
    } finally {
      if (_isActive(client, action.generation)) {
        setState(() {
          _busy = false;
          _busyMessage = null;
        });
        _cancelToken = null;
      }
    }
  }

  /// 将 OAuth 取消、同步业务错误与网络错误转为安全且可执行的提示。
  String _errorMessage(Object error) {
    if (error is FormatException) {
      return 'Gmail 响应格式异常，请更新 App 与服务端后重试。';
    }
    if (error is LyraApiError) {
      if (error.code == 'LOGIN_CANCELLED') {
        return '已取消 Gmail 授权，当前邮箱连接保持不变。';
      }
      final gmailMessage = _gmailErrorMessage(error.code);
      if (gmailMessage != null) return gmailMessage;
      return switch (error.code) {
        _ when error.code.startsWith('GMAIL_') => 'Gmail 操作暂时无法完成，请刷新后重试。',
        _ => lyraErrorMessage(error, fallback: 'Gmail 操作暂时无法完成，请重试。'),
      };
    }
    return lyraErrorMessage(error, fallback: 'Gmail 操作暂时无法完成，请重试。');
  }

  /// 呈现账户、连接、日期确认和同步结果，并让状态随当前会话切换。
  @override
  Widget build(BuildContext context) {
    ref.listen(sessionProvider, (previous, next) {
      _observeClient(ref.read(sessionProvider.notifier).client);
    });
    final session = ref.watch(sessionProvider);
    final client = ref.watch(gmailAuthenticatedClientProvider);
    final connections = client == null
        ? null
        : ref.watch(gmailConnectionsProvider(client));
    final accounts = client == null
        ? null
        : ref.watch(brokerageAccountsProvider);
    final activeAccount = accounts?.value;
    final effectiveAccountId = _effectiveAccountId(activeAccount);
    final selectedAccount = activeAccount == null
        ? null
        : _accountById(activeAccount, effectiveAccountId);

    return ProfileSubpage(
      title: 'Gmail 同步',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text(
            '授权 Gmail 只读权限，Lyra 仅筛选嘉信成交确认邮件。',
            style: TextStyle(color: LyraColors.inkSecondary, height: 1.6),
          ),
          const SizedBox(height: 14),
          if (session.isLoading)
            const LinearProgressIndicator(minHeight: 2)
          else if (session.value is! SessionAuthenticated)
            const LoginPanel()
          else ...[
            _RefreshHeader(
              refreshing: _refreshing,
              busy: _busy,
              onRefresh: client == null ? null : () => _refresh(client),
            ),
            if (_busyMessage != null) ...[
              const SizedBox(height: 4),
              Text(
                _busyMessage!,
                style: const TextStyle(color: LyraColors.muted, height: 1.5),
              ),
            ],
            if (_pageError != null) ...[
              const SizedBox(height: 8),
              LyraCard(child: ProfileNotice(message: _pageError!)),
            ],
            const SizedBox(height: 8),
            _AccountSelector(
              accounts: accounts,
              selectedAccountId: effectiveAccountId,
              enabled: !_busy && !_refreshing,
              onChanged: (id) => setState(() => _selectedAccountId = id),
              onCreate: () async {
                await showCreateAccountSheet(context);
                if (mounted) ref.invalidate(brokerageAccountsProvider);
              },
            ),
            const SizedBox(height: 16),
            connections == null
                ? const LoginPanel()
                : connections.when(
                    loading: () => const Padding(
                      padding: EdgeInsets.symmetric(vertical: 24),
                      child: LinearProgressIndicator(minHeight: 2),
                    ),
                    error: (error, _) => LyraCard(
                      child: ProfileNotice(
                        message: lyraErrorMessage(
                          error,
                          fallback: '暂时无法读取 Gmail 连接，请重新加载。',
                        ),
                        onRetry: client == null || _refreshing
                            ? null
                            : () => _refresh(client),
                        retryLabel: '重新加载',
                      ),
                    ),
                    data: (connectionList) =>
                        _connectionContent(connectionList, selectedAccount),
                  ),
          ],
        ],
      ),
    );
  }

  /// 返回当前账户选择；仅有一个可用证券账户时自动预选它。
  String? _effectiveAccountId(List<BrokerageAccount>? accounts) {
    if (accounts == null || accounts.isEmpty) return null;
    if (accounts.length == 1) return accounts.single.id;
    if (accounts.any((account) => account.id == _selectedAccountId)) {
      return _selectedAccountId;
    }
    return null;
  }

  /// 查找已经由当前账户列表验证过的被选账户。
  BrokerageAccount? _accountById(
    List<BrokerageAccount> accounts,
    String? accountId,
  ) {
    if (accountId == null) return null;
    for (final account in accounts) {
      if (account.id == accountId) return account;
    }
    return null;
  }

  /// 显示 OAuth 未配置提示、连接入口或当前用户 Gmail 连接卡片。
  Widget _connectionContent(
    GmailConnectionList data,
    BrokerageAccount? selectedAccount,
  ) {
    if (!data.configured) {
      return const LyraCard(
        child: ProfileNotice(message: 'Gmail 邮件连接暂不可用，请稍后再试。'),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final connection in data.rows) ...[
          _GmailConnectionCard(
            key: ValueKey('gmail-connection-${connection.id}'),
            connection: connection,
            account: selectedAccount,
            rescanMode: _rescanModes[connection.id] == true,
            scanRange: _scanRanges[connection.id],
            scanDate: _scanDates[connection.id],
            result: _results[connection.id],
            error: _actionErrors[connection.id],
            busy: _busy || _refreshing,
            onStartRescan: () => _startRescan(connection),
            onCancelRescan: () => _cancelRescan(connection),
            onSelectAll: () => _selectAllScanRange(connection),
            onChooseDate: () => _chooseScanDate(connection),
            onSync: () => _sync(connection, selectedAccount),
            onReconnect: _authorize,
            onDisconnect: () => _disconnect(connection),
          ),
          const SizedBox(height: 12),
        ],
        if (data.rows.isEmpty ||
            data.rows.every(
              (connection) =>
                  connection.status == GmailConnectionStatus.disconnected,
            ))
          LyraCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const ProfileNotice(message: '尚未连接 Gmail。授权后只读取嘉信成交确认邮件。'),
                const SizedBox(height: 8),
                FilledButton.icon(
                  key: const ValueKey('gmail-connect'),
                  onPressed: _busy ? null : _authorize,
                  icon: const Icon(LyraIcons.gmail, size: 18),
                  label: const Text('连接 Gmail'),
                ),
              ],
            ),
          ),
        if (data.rows.isNotEmpty &&
            data.rows.any(
              (connection) =>
                  connection.status != GmailConnectionStatus.disconnected,
            ))
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              key: const ValueKey('gmail-add-connection'),
              onPressed: _busy ? null : _authorize,
              icon: const Icon(LyraIcons.addAccount, size: 18),
              label: const Text('添加 Gmail 邮箱'),
            ),
          ),
      ],
    );
  }
}

/// 用户在首次扫描或显式重扫时选择的范围类型。
enum _GmailScanRange { all, customDate }

/// 展示连接刷新操作和当前读取状态。
class _RefreshHeader extends StatelessWidget {
  const _RefreshHeader({
    required this.refreshing,
    required this.busy,
    required this.onRefresh,
  });

  final bool refreshing;
  final bool busy;
  final VoidCallback? onRefresh;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Expanded(
        child: Text(
          'Gmail 连接',
          style: Theme.of(context).textTheme.titleMedium
              ?.copyWith(fontWeight: FontWeight.w500),
        ),
      ),
      TextButton.icon(
        key: const ValueKey('gmail-refresh'),
        onPressed: busy || refreshing ? null : onRefresh,
        icon: refreshing
            ? const SizedBox.square(
                dimension: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Icon(LyraIcons.sync, size: 18),
        label: const Text('刷新'),
      ),
    ],
  );
}

/// 允许用户选择已存在证券账户，并复用既有账户创建入口。
class _AccountSelector extends StatelessWidget {
  const _AccountSelector({
    required this.accounts,
    required this.selectedAccountId,
    required this.enabled,
    required this.onChanged,
    required this.onCreate,
  });

  final AsyncValue<List<BrokerageAccount>>? accounts;
  final String? selectedAccountId;
  final bool enabled;
  final ValueChanged<String?> onChanged;
  final VoidCallback onCreate;

  @override
  Widget build(BuildContext context) => LyraCard(
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          '同步到证券账户',
          style: Theme.of(context).textTheme.titleSmall
              ?.copyWith(fontWeight: FontWeight.w500),
        ),
        const SizedBox(height: 6),
        const Text(
          '同步只写入你选定的账户，不会新建账户。',
          style: TextStyle(color: LyraColors.muted, height: 1.5),
        ),
        const SizedBox(height: 12),
        if (accounts == null)
          const ProfileNotice(message: '登录后选择已有证券账户。')
        else
          accounts!.when(
            loading: () => const LinearProgressIndicator(minHeight: 2),
            error: (error, _) => ProfileNotice(
              message: lyraErrorMessage(error, fallback: '暂时无法读取证券账户。'),
            ),
            data: (rows) => rows.isEmpty
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const ProfileNotice(message: '还没有证券账户，创建后才能同步。'),
                      const SizedBox(height: 8),
                      OutlinedButton.icon(
                        key: const ValueKey('gmail-create-account'),
                        onPressed: enabled ? onCreate : null,
                        icon: const Icon(LyraIcons.addAccount, size: 18),
                        label: const Text('新增证券账户'),
                      ),
                    ],
                  )
                : DropdownButtonFormField<String>(
                    key: const ValueKey('gmail-account-selector'),
                    initialValue: selectedAccountId,
                    isExpanded: true,
                    itemHeight: null,
                    decoration: const InputDecoration(labelText: '现有证券账户'),
                    items: [
                      for (final account in rows)
                        DropdownMenuItem<String>(
                          value: account.id,
                          child: Text(
                            '${account.displayName} · 尾号 ${account.maskedSuffix}',
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                    ],
                    onChanged: enabled ? onChanged : null,
                  ),
          ),
      ],
    ),
  );
}

/// 展示单个 Google 邮箱的真实状态、扫描范围和同步摘要。
class _GmailConnectionCard extends StatelessWidget {
  const _GmailConnectionCard({
    super.key,
    required this.connection,
    required this.account,
    required this.rescanMode,
    required this.scanRange,
    required this.scanDate,
    required this.result,
    required this.error,
    required this.busy,
    required this.onStartRescan,
    required this.onCancelRescan,
    required this.onSelectAll,
    required this.onChooseDate,
    required this.onSync,
    required this.onReconnect,
    required this.onDisconnect,
  });

  final GmailConnection connection;
  final BrokerageAccount? account;
  final bool rescanMode;
  final _GmailScanRange? scanRange;
  final DateTime? scanDate;
  final GmailSyncResult? result;
  final String? error;
  final bool busy;
  final VoidCallback onStartRescan;
  final VoidCallback onCancelRescan;
  final VoidCallback onSelectAll;
  final VoidCallback onChooseDate;
  final VoidCallback onSync;
  final VoidCallback onReconnect;
  final VoidCallback onDisconnect;

  @override
  Widget build(BuildContext context) {
    final status = connection.status;
    final canSync =
        (status == GmailConnectionStatus.active && !rescanMode) ||
        ((status.requiresRescanRange || rescanMode) &&
            (scanRange == _GmailScanRange.all ||
                (scanRange == _GmailScanRange.customDate && scanDate != null)));
    return LyraCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Padding(
                padding: EdgeInsets.only(top: 1),
                child: Icon(
                  LyraIcons.gmail,
                  size: 20,
                  color: LyraColors.inkSecondary,
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SelectableText(
                      connection.googleEmail,
                      style: Theme.of(context).textTheme.titleSmall
                          ?.copyWith(fontWeight: FontWeight.w500),
                    ),
                    const SizedBox(height: 5),
                    Text(
                      '${status.label} · ${_lastSyncLabel(connection.lastSyncAt)}',
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: LyraColors.inkSecondary,
                        height: 1.5,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          if (connection.lastErrorCode != null) ...[
            const SizedBox(height: 8),
            Text(
              _connectionError(connection.lastErrorCode!),
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: LyraColors.muted, height: 1.5),
            ),
          ],
          if (status.requiresRescanRange || rescanMode) ...[
            const SizedBox(height: 12),
            Text(
              status == GmailConnectionStatus.pendingInitialScan
                  ? '首次扫描范围'
                  : '重新扫描范围',
              style: Theme.of(context).textTheme.labelLarge
                  ?.copyWith(fontWeight: FontWeight.w500),
            ),
            const SizedBox(height: 6),
            const Text(
              '可扫描全部嘉信成交邮件，或自选起始日期。',
              style: TextStyle(color: LyraColors.muted, height: 1.5),
            ),
            const SizedBox(height: 8),
            if (scanRange == _GmailScanRange.all)
              FilledButton.tonal(
                key: ValueKey('gmail-scan-all-${connection.id}'),
                onPressed: busy ? null : onSelectAll,
                child: const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(LyraIcons.confirm, size: 18),
                    SizedBox(width: 8),
                    Flexible(child: Text('全部嘉信成交邮件（推荐）· 已选择')),
                  ],
                ),
              )
            else
              OutlinedButton.icon(
                key: ValueKey('gmail-scan-all-${connection.id}'),
                onPressed: busy ? null : onSelectAll,
                icon: const Icon(LyraIcons.gmail, size: 18),
                label: const Text('全部嘉信成交邮件（推荐）'),
              ),
            const SizedBox(height: 6),
            if (scanRange == _GmailScanRange.customDate && scanDate != null)
              FilledButton.tonal(
                key: ValueKey('gmail-date-${connection.id}'),
                onPressed: busy ? null : onChooseDate,
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(LyraIcons.confirm, size: 18),
                    const SizedBox(width: 8),
                    Flexible(child: Text('自 ${_chineseDate(scanDate!)} 起扫描')),
                  ],
                ),
              )
            else
              OutlinedButton.icon(
                key: ValueKey('gmail-date-${connection.id}'),
                onPressed: busy ? null : onChooseDate,
                icon: const Icon(Icons.calendar_today_outlined, size: 17),
                label: const Text('自定义起始日期'),
              ),
          ],
          if (account == null &&
              status != GmailConnectionStatus.disconnected) ...[
            const SizedBox(height: 8),
            const Text(
              '请先选择已有证券账户后再同步。',
              style: TextStyle(color: LyraColors.muted, height: 1.5),
            ),
          ],
          if (result != null) ...[
            const SizedBox(height: 12),
            _SyncResultPanel(result: result!),
          ],
          if (error != null) ...[
            const SizedBox(height: 8),
            Text(
              error!,
              style: const TextStyle(color: LyraColors.negative, height: 1.5),
            ),
          ],
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (canSync)
                FilledButton.icon(
                  key: ValueKey('gmail-sync-${connection.id}'),
                  onPressed: busy || account == null ? null : onSync,
                  icon: const Icon(LyraIcons.sync, size: 18),
                  label: Text(_syncButtonLabel(status, scanRange, rescanMode)),
                ),
              if (status == GmailConnectionStatus.active && !rescanMode)
                TextButton.icon(
                  key: ValueKey('gmail-rescan-${connection.id}'),
                  onPressed: busy ? null : onStartRescan,
                  icon: const Icon(LyraIcons.sync, size: 18),
                  label: const Text('重新扫描'),
                ),
              if (status == GmailConnectionStatus.active && rescanMode)
                TextButton(
                  key: ValueKey('gmail-cancel-rescan-${connection.id}'),
                  onPressed: busy ? null : onCancelRescan,
                  child: const Text('取消重扫'),
                ),
              if (connection.lastErrorCode == 'GMAIL_AUTH_REVOKED')
                OutlinedButton.icon(
                  key: ValueKey('gmail-reconnect-${connection.id}'),
                  onPressed: busy ? null : onReconnect,
                  icon: const Icon(LyraIcons.gmail, size: 18),
                  label: const Text('重新授权'),
                ),
              if (status != GmailConnectionStatus.disconnected)
                TextButton.icon(
                  key: ValueKey('gmail-disconnect-${connection.id}'),
                  onPressed: busy ? null : onDisconnect,
                  icon: const Icon(Icons.link_off_outlined, size: 18),
                  label: const Text('断开连接'),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

/// 显示服务端按真实账本交易去重后的同步结果。
class _SyncResultPanel extends StatelessWidget {
  const _SyncResultPanel({required this.result});

  final GmailSyncResult result;

  @override
  Widget build(BuildContext context) => LyraCard(
    inset: true,
    padding: const EdgeInsets.all(14),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '同步结果',
          style: Theme.of(context).textTheme.labelLarge
              ?.copyWith(fontWeight: FontWeight.w500),
        ),
        const SizedBox(height: 6),
        Text('新增交易 ${result.insertedTransactions} 笔'),
        Text('已存在跳过 ${result.existingTransactions} 笔'),
        Text('邮件解析失败 ${result.rejectedMessages} 封'),
        Text(
          '账户映射冲突 '
          '${result.conflictMessages - result.replayOrderConflictMessages} 封',
        ),
        Text('同日成交顺序冲突 ${result.replayOrderConflictMessages} 封'),
        if (result.truncated ||
            result.connectionStatus == GmailConnectionStatus.needsResync) ...[
          const SizedBox(height: 6),
          const Text(
            '本次扫描未完成，部分邮件可能尚未处理。请刷新连接状态后，重新选择扫描范围。',
            style: TextStyle(color: LyraColors.accent, height: 1.5),
          ),
        ],
        if (result.replayOrderConflictMessages > 0) ...[
          const SizedBox(height: 6),
          const Text(
            '部分同日成交顺序无法确认，请导入包含该日全部成交的嘉信 CSV。',
            style: TextStyle(color: LyraColors.accent, height: 1.5),
          ),
        ],
      ],
    ),
  );
}

/// 解释可供页面恢复或用户重授权的稳定 Gmail 业务错误码。
String _connectionError(String code) =>
    _gmailErrorMessage(code) ?? 'Gmail 连接状态需要处理，请刷新后重试。';

/// 按稳定错误码选择动作和连接状态共用的提示，不展示原始响应。
///
/// [code] 是服务端协议错误码；返回可执行的中文提示，未知码返回 null，交给上游处理。
String? _gmailErrorMessage(String code) => switch (code) {
  'GMAIL_HISTORY_EXPIRED' => '增量游标已失效，请重新选择扫描范围。',
  'GMAIL_INCREMENTAL_LIMIT_EXCEEDED' => '新增邮件超过同步上限，请重新选择扫描范围。',
  'GMAIL_RESYNC_CONFIRMATION_REQUIRED' => '需要确认扫描范围。',
  'GMAIL_AUTH_REVOKED' => 'Google 邮箱授权已失效，请重新授权。',
  'GMAIL_SCOPE_MISSING' => '未勾选 Gmail 只读权限，请重新连接并授予该权限。',
  'GMAIL_AUTHORIZATION_DENIED' => '授权已取消，尚未授予 Gmail 只读权限。',
  'GMAIL_API_DISABLED' => 'Google Cloud 项目尚未启用 Gmail API，请启用后重试。',
  'GMAIL_RATE_LIMITED' => 'Gmail 请求达到限额，请稍后再试。',
  'GMAIL_ACCESS_DENIED' => 'Google 拒绝访问 Gmail，请检查邮箱权限或管理员策略。',
  'GMAIL_CONNECTION_ERROR' => 'Google 服务暂时不可用，请刷新后重试。',
  'GMAIL_AUTHORIZATION_EXPIRED' ||
  'GOOGLE_AUTH_TRANSACTION_EXPIRED' => 'Google 授权已超时，请重新连接。',
  'GOOGLE_IDENTITY_UNVERIFIED' => 'Google 邮箱身份未验证，请完成邮箱验证后重新连接。',
  'GOOGLE_TOKEN_EXCHANGE_FAILED' => 'Google 授权未能完成，请重新连接；若持续失败，请检查服务端配置。',
  'LOGIN_CALLBACK_INVALID' => 'Google 返回信息无效，请重新连接。',
  'LOGIN_BROWSER_UNAVAILABLE' => '无法打开 Google 授权窗口，请重试。',
  'LOGIN_BROWSER_UNSUPPORTED' => '当前浏览器不支持授权窗口，请更新浏览器后重试。',
  _ => null,
};

/// 根据连接状态和已选范围给出能说明写入意图的同步按钮文案。
String _syncButtonLabel(
  GmailConnectionStatus status,
  _GmailScanRange? range,
  bool rescanMode,
) {
  if (status == GmailConnectionStatus.active && !rescanMode) return '立即同步';
  if (range == _GmailScanRange.all) {
    return status == GmailConnectionStatus.pendingInitialScan
        ? '同步全部嘉信邮件'
        : '重新扫描全部嘉信邮件';
  }
  return status == GmailConnectionStatus.pendingInitialScan ? '开始扫描' : '按日期重扫';
}

/// 格式化上次同步时间；空值清楚表示连接尚未成功同步。
String _lastSyncLabel(DateTime? value) {
  if (value == null) return '尚未同步';
  final local = value.toLocal();
  return '上次同步 ${local.year.toString().padLeft(4, '0')}-'
      '${local.month.toString().padLeft(2, '0')}-'
      '${local.day.toString().padLeft(2, '0')} '
      '${local.hour.toString().padLeft(2, '0')}:'
      '${local.minute.toString().padLeft(2, '0')}';
}

/// 格式化已由用户确认的扫描起始日期。
String _chineseDate(DateTime value) =>
    '${value.year}年${value.month}月${value.day}日';

/// 将本地日历选择序列化为服务端使用的日期文本，不做 UTC 日期换算。
String _localDate(DateTime value) =>
    '${value.year.toString().padLeft(4, '0')}-'
    '${value.month.toString().padLeft(2, '0')}-'
    '${value.day.toString().padLeft(2, '0')}';
