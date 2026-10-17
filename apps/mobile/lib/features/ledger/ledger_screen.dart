import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_date.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_number.dart';
import '../../core/design/stock_avatar.dart';
import '../../core/design/lyra_widgets.dart';
import '../../core/preferences/app_preferences.dart';
import '../market/market_widgets.dart';
import 'ledger_view.dart';
import 'ledger_workspace.dart';
import 'position_group.dart';

/// 股票与交易的一级页面；同一目录驱动 tab 与只读查询，保持账户范围一致。
class LedgerScreen extends ConsumerStatefulWidget {
  const LedgerScreen({
    super.key,
    required this.stocks,
    required this.onConnect,
  });
  final bool stocks;
  final VoidCallback onConnect;
  @override
  ConsumerState<LedgerScreen> createState() => _LedgerScreenState();
}

class _LedgerScreenState extends ConsumerState<LedgerScreen> {
  int _tab = 0;
  @override
  Widget build(BuildContext context) {
    final views = widget.stocks
        ? [LedgerView.positions, LedgerView.expiring, LedgerView.allocation]
        : [LedgerView.trades, LedgerView.realized, LedgerView.cashflow];
    return LyraPage(
      storageKey: widget.stocks ? 'stocks-scroll' : 'transactions-scroll',
      children: [
        LyraPageTitle(
          title: widget.stocks ? '我的股票' : '交易记录',
          trailing: const AccountScopeButton(),
        ),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              for (int index = 0; index < views.length; index++)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    showCheckmark: false,
                    label: Text(
                      views[index].title,
                      style: TextStyle(
                        color: _tab == index
                            ? LyraColors.accent
                            : LyraColors.inkSecondary,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    selected: _tab == index,
                    onSelected: (_) => setState(() => _tab = index),
                  ),
                ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        LedgerPanel(
          key: ValueKey(views[_tab]),
          view: views[_tab],
          onConnect: widget.onConnect,
        ),
        if (widget.stocks) ...[
          const SizedBox(height: 34),
          const Text(
            '持仓分析',
            style: TextStyle(fontSize: 18, fontWeight: FontWeight.w500),
          ),
          const SizedBox(height: 12),
          for (final view in [LedgerView.risk, LedgerView.stress])
            ReportLink(view: view),
        ],
      ],
    );
  }
}

/// 次级分析保持普通返回导航，AI 不占据金融报告入口。
class ReportScreen extends StatelessWidget {
  const ReportScreen({super.key, required this.view});
  final LedgerView view;
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text(view.title),
      actions: const [AccountScopeButton(), SizedBox(width: 16)],
    ),
    body: LyraPage(
      storageKey: 'report-${view.command}',
      children: [
        LedgerPanel(view: view, onConnect: () => Navigator.pop(context)),
      ],
    ),
  );
}

/// 报告入口使用线性目录，无重复卡片包裹。
class ReportLink extends StatelessWidget {
  const ReportLink({super.key, required this.view});
  final LedgerView view;
  @override
  Widget build(BuildContext context) => Container(
    decoration: const BoxDecoration(
      border: Border(bottom: BorderSide(color: LyraColors.hairline)),
    ),
    child: ListTile(
      contentPadding: const EdgeInsets.symmetric(vertical: 2),
      leading: Icon(view.icon, color: LyraColors.accent, size: 22),
      title: Text(
        view.title,
        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500),
      ),
      subtitle: Text(
        view.basis,
        style: const TextStyle(
          fontSize: 12,
          height: 1.5,
          color: LyraColors.inkSecondary,
        ),
      ),
      trailing: const Icon(LyraIcons.disclosure, size: 19),
      onTap: () => Navigator.push(
        context,
        MaterialPageRoute<void>(builder: (_) => ReportScreen(view: view)),
      ),
    ),
  );
}

/// 首页收益走势复用报告查询与图表，不聚合金额或制造市场价格序列。
class LedgerTrendPreview extends ConsumerStatefulWidget {
  const LedgerTrendPreview({super.key});

  @override
  ConsumerState<LedgerTrendPreview> createState() => _LedgerTrendPreviewState();
}

class _LedgerTrendPreviewState extends ConsumerState<LedgerTrendPreview> {
  LedgerView _view = LedgerView.monthly;

  @override
  Widget build(BuildContext context) {
    final scope = ref.watch(accountScopeProvider);
    final query = (path: _view.path, parameters: '', scope: scope.id);
    final result = ref.watch(ledgerReadProvider(query));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            const Expanded(
              child: Text(
                '收益走势',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.w500),
              ),
            ),
            for (final (view, label) in [
              (LedgerView.monthly, '按月'),
              (LedgerView.drawdown, '累计'),
            ])
              TextButton(
                style: TextButton.styleFrom(
                  minimumSize: const Size(48, 44),
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  backgroundColor: _view == view
                      ? LyraColors.accentTint
                      : Colors.transparent,
                  foregroundColor: _view == view
                      ? LyraColors.accent
                      : LyraColors.muted,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10),
                  ),
                ),
                onPressed: () => setState(() => _view = view),
                child: Text(label, style: const TextStyle(fontSize: 12)),
              ),
            IconButton(
              tooltip: '查看完整收益报告',
              icon: const Icon(LyraIcons.disclosure, size: 20),
              onPressed: () => Navigator.push(
                context,
                MaterialPageRoute<void>(
                  builder: (_) => ReportScreen(view: _view),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),
        result.when(
          loading: () => const SizedBox(
            height: 160,
            child: Center(child: CircularProgressIndicator()),
          ),
          error: (error, _) => LyraLoadError(
            error: error,
            onRetry: () => ref.invalidate(ledgerReadProvider(query)),
          ),
          data: (read) => Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (read.sections.isEmpty)
                const Text('暂无收益记录', style: TextStyle(color: LyraColors.muted)),
              for (final section in read.sections)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Builder(
                    builder: (context) {
                      final rows = (section.data['rows'] as List<Object?>)
                          .cast<Map<String, Object?>>();
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Text(
                            '${section.currency ?? '按账户币种'} · 已实现损益',
                            style: const TextStyle(
                              fontSize: 12,
                              color: LyraColors.muted,
                            ),
                          ),
                          const SizedBox(height: 12),
                          if (rows.isEmpty)
                            const Padding(
                              padding: EdgeInsets.symmetric(vertical: 32),
                              child: Text('暂无收益记录'),
                            )
                          else
                            // 查询改变后清除图表选中索引；单条记录同样保留精确读数。
                            _DataPlot(
                              key: ValueKey((query, section.currency)),
                              rows: rows,
                              field: _view == LedgerView.monthly
                                  ? 'netPnl'
                                  : 'cumulativePnl',
                              line: _view == LedgerView.drawdown,
                              compact: true,
                            ),
                        ],
                      );
                    },
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

/// 总览的持仓摘要复用股票页查询和展开明细，按币种展示前三个标的。
/// [onOpen] 进入完整持仓；不重算市值、不合并不同币种。
class LedgerHoldingsPreview extends ConsumerWidget {
  const LedgerHoldingsPreview({super.key, this.onOpen});
  final VoidCallback? onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(accountScopeProvider);
    final query = (
      path: LedgerView.positions.path,
      parameters: '',
      scope: scope.id,
    );
    final read = ref.watch(ledgerReadProvider(query));
    return Column(
      key: const ValueKey('overview-holdings-preview'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            const Expanded(
              child: Text(
                '我的持仓',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.w500),
              ),
            ),
            TextButton(
              style: TextButton.styleFrom(
                foregroundColor: LyraColors.accent,
                padding: const EdgeInsets.only(left: 12),
                minimumSize: const Size(0, 44),
              ),
              onPressed: onOpen,
              child: const Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text('查看全部', style: TextStyle(fontSize: 13)),
                  SizedBox(width: 4),
                  Icon(LyraIcons.disclosure, size: 19),
                ],
              ),
            ),
          ],
        ),
        read.when(
          loading: () => const LyraSkeleton(),
          error: (error, _) => LyraLoadError(
            error: error,
            onRetry: () => ref.invalidate(ledgerReadProvider(query)),
          ),
          data: (result) => Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (result.sections.isEmpty)
                const Text('暂无持仓记录', style: TextStyle(color: LyraColors.muted)),
              for (final section in result.sections)
                _ReportSection(
                  view: LedgerView.positions,
                  read: result,
                  data: section.data,
                  currency: section.currency,
                  previewLimit: 3,
                ),
              MarketPositionsPanel(ledger: result),
            ],
          ),
        ),
      ],
    );
  }
}

/// 筛选、分页与结果状态。查询键含范围和完整参数，旧请求不会覆盖新筛选。
class LedgerPanel extends ConsumerStatefulWidget {
  const LedgerPanel({super.key, required this.view, required this.onConnect});
  final LedgerView view;
  final VoidCallback onConnect;
  @override
  ConsumerState<LedgerPanel> createState() => _LedgerPanelState();
}

class _LedgerPanelState extends ConsumerState<LedgerPanel> {
  final _search = TextEditingController();
  String _symbol = '';
  bool _searchOpen = false;
  String _groupBy = 'underlying';
  String? _action;
  int _days = 30;
  DateTimeRange? _dateRange;
  final List<String?> _cursors = [null];
  String? _lastScope;

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  /// 改筛选时从第一页开始，避免沿用其它查询的分页游标。
  void _filter(VoidCallback change) => setState(() {
    change();
    _cursors
      ..clear()
      ..add(null);
  });

  /// 搜索提交后收起键盘，并复用筛选入口重置分页游标。
  void _applySearch() {
    FocusScope.of(context).unfocus();
    _filter(() {
      _symbol = _search.text.trim().toUpperCase();
      _searchOpen = false;
    });
  }

  Future<void> _chooseDates() async {
    final range = await showDateRangePicker(
      context: context,
      initialDateRange: _dateRange,
      firstDate: DateTime(1970),
      lastDate: DateTime.now(),
      helpText: '选择交易日期范围',
      saveText: '应用',
    );
    if (!mounted || range == null) return;
    _filter(() => _dateRange = range);
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(sessionProvider);
    if (session.isLoading) return const LyraSkeleton();
    if (session.hasError) {
      return LyraLoadError(
        error: session.error!,
        onRetry: () => ref.invalidate(sessionProvider),
      );
    }
    if (session.value is! SessionAuthenticated) {
      return LyraEmpty(
        title: widget.view == LedgerView.positions ? '你的持仓，从这里展开' : '登录后，开始查看',
        message: widget.view == LedgerView.positions
            ? '导入 Schwab 交易记录后，股票与期权的数量、方向和历史成本将在这里清晰呈现。'
            : '登录 Lyra 后，即可按证券账户查看${widget.view.title}。',
        icon: widget.view.icon,
        action: FilledButton.icon(
          onPressed: widget.onConnect,
          icon: const Icon(LyraIcons.arrowUpRight, size: 18),
          label: const Text('前往登录'),
        ),
      );
    }
    final scope = ref.watch(accountScopeProvider);
    if (_lastScope != scope.id) {
      _lastScope = scope.id;
      _cursors
        ..clear()
        ..add(null);
    }
    final parameters = <String, String>{
      ...widget.view.parameters,
      if (_symbol.isNotEmpty)
        (widget.view == LedgerView.positions ? 'underlying' : 'symbol'):
            _symbol,
      if (widget.view == LedgerView.expiring) 'days': '$_days',
      if ([LedgerView.allocation, LedgerView.attribution].contains(widget.view))
        'groupBy': _groupBy,
      'action': ?_action,
      if (_dateRange != null)
        'fromDate': _dateRange!.start.toIso8601String().substring(0, 10),
      if (_dateRange != null)
        'toDate': _dateRange!.end.toIso8601String().substring(0, 10),
      'cursor': ?_cursors.last,
    };
    final query = (
      path: widget.view.path,
      parameters: Uri(queryParameters: parameters).query,
      scope: scope.id,
    );
    final read = ref.watch(ledgerReadProvider(query));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Wrap(
          spacing: 10,
          runSpacing: 10,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            if (widget.view.dateFilter)
              OutlinedButton.icon(
                onPressed: _chooseDates,
                icon: const Icon(LyraIcons.dataCutoff, size: 16),
                label: Text(
                  _dateRange == null
                      ? '全部日期'
                      : '${parameters['fromDate']} — ${parameters['toDate']}',
                ),
              ),
            if (_dateRange != null || _action != null)
              TextButton(
                onPressed: () => _filter(() {
                  _dateRange = null;
                  _symbol = '';
                  _action = null;
                  _search.clear();
                }),
                child: const Text('清除筛选'),
              ),
            if (widget.view == LedgerView.expiring)
              DropdownButton<int>(
                value: _days,
                underline: const SizedBox.shrink(),
                items: [7, 30, 60, 90, 180, 366]
                    .map(
                      (days) => DropdownMenuItem(
                        value: days,
                        child: Text('未来 $days 天'),
                      ),
                    )
                    .toList(),
                onChanged: (value) {
                  if (value != null) _filter(() => _days = value);
                },
              ),
            if ([
              LedgerView.allocation,
              LedgerView.attribution,
            ].contains(widget.view))
              DropdownButton<String>(
                value: _groupBy,
                underline: const SizedBox.shrink(),
                items: [
                  const DropdownMenuItem(
                    value: 'underlying',
                    child: Text('按标的'),
                  ),
                  const DropdownMenuItem(
                    value: 'asset-type',
                    child: Text('按资产类别'),
                  ),
                  if (widget.view == LedgerView.attribution) ...[
                    const DropdownMenuItem(
                      value: 'direction',
                      child: Text('按方向'),
                    ),
                    const DropdownMenuItem(
                      value: 'close-action',
                      child: Text('按平仓动作'),
                    ),
                  ],
                ],
                onChanged: (value) {
                  if (value != null) _filter(() => _groupBy = value);
                },
              ),
            if (widget.view == LedgerView.trades)
              DropdownButton<String>(
                hint: const Text('全部动作'),
                value: _action,
                underline: const SizedBox.shrink(),
                items:
                    [
                          'Buy',
                          'Sell',
                          'Buy to Open',
                          'Sell to Open',
                          'Buy to Close',
                          'Sell to Close',
                          'Expired',
                          'Assigned',
                        ]
                        .map(
                          (action) => DropdownMenuItem(
                            value: action,
                            child: Text(ledgerText(action)),
                          ),
                        )
                        .toList(),
                onChanged: (value) => _filter(() => _action = value),
              ),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: Text(
                widget.view.basis,
                style: const TextStyle(
                  fontSize: 12,
                  height: 1.5,
                  color: LyraColors.muted,
                ),
              ),
            ),
            if (widget.view.searchable)
              IconButton(
                tooltip: _searchOpen ? '收起搜索' : '搜索证券',
                isSelected: _searchOpen || _symbol.isNotEmpty,
                onPressed: () => setState(() {
                  _searchOpen = !_searchOpen;
                  // 关闭仅收起编辑器；已应用条件继续可见，不改变查询。
                  _search.text = _symbol;
                  if (!_searchOpen) FocusScope.of(context).unfocus();
                }),
                icon: Icon(
                  _searchOpen ? LyraIcons.dismiss : LyraIcons.search,
                  size: 20,
                ),
              ),
            IconButton(
              tooltip: '刷新数据',
              onPressed: () => ref.invalidate(ledgerReadProvider(query)),
              icon: const Icon(LyraIcons.sync, size: 18),
            ),
          ],
        ),
        if (_searchOpen) ...[
          const SizedBox(height: 8),
          TextField(
            key: const ValueKey('ledger-symbol-search'),
            controller: _search,
            autofocus: true,
            textCapitalization: TextCapitalization.characters,
            autocorrect: false,
            textInputAction: TextInputAction.search,
            onSubmitted: (_) => _applySearch(),
            decoration: InputDecoration(
              hintText: '输入证券代码，如 AAPL',
              prefixIcon: const Icon(LyraIcons.search, size: 20),
              suffixIcon: IconButton(
                tooltip: '应用搜索',
                icon: const Icon(LyraIcons.arrowUpRight, size: 20),
                onPressed: _applySearch,
              ),
            ),
          ),
        ],
        if (_symbol.isNotEmpty)
          Align(
            alignment: Alignment.centerLeft,
            child: InputChip(
              label: Text('标的 · $_symbol'),
              deleteButtonTooltipMessage: '清除标的筛选',
              onPressed: () => setState(() => _searchOpen = true),
              onDeleted: () => _filter(() {
                _symbol = '';
                _search.clear();
              }),
            ),
          ),
        const SizedBox(height: 16),
        read.when(
          loading: () => const LyraSkeleton(),
          error: (error, _) => LyraLoadError(
            error: error,
            onRetry: () => ref.invalidate(ledgerReadProvider(query)),
          ),
          data: (read) => Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (read.sections.isEmpty)
                const LyraEmpty(
                  title: '还没有可分析的记录',
                  message: '导入完整交易记录后，这里将呈现相应结果。',
                ),
              for (final section in read.sections)
                _ReportSection(
                  view: widget.view,
                  read: read,
                  data: section.data,
                  currency: section.currency,
                ),
              if (widget.view == LedgerView.positions)
                MarketPositionsPanel(
                  ledger: read,
                  underlying: _symbol.isEmpty ? null : _symbol,
                ),
              if (widget.view.paginated)
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    TextButton(
                      onPressed: _cursors.length > 1
                          ? () => setState(() => _cursors.removeLast())
                          : null,
                      child: const Text('上一页'),
                    ),
                    Text(
                      '第 ${_cursors.length} 页',
                      style: const TextStyle(
                        color: LyraColors.muted,
                        fontSize: 12,
                      ),
                    ),
                    TextButton(
                      onPressed: read.data['nextCursor'] is String
                          ? () => setState(
                              () => _cursors.add(
                                read.data['nextCursor'] as String,
                              ),
                            )
                          : null,
                      child: const Text('下一页'),
                    ),
                  ],
                ),
              const SizedBox(height: 24),
              Text(
                '${scope.label}\n数据截至 ${lyraDateTime(read.metadata['asOf'] is String ? read.metadata['asOf'] as String : null)}',
                style: const TextStyle(
                  fontSize: 11,
                  height: 1.6,
                  color: LyraColors.muted,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// 一个币种一个区块。图形仅使用响应中的精确金额投影坐标，不回写金融数据。
class _ReportSection extends StatelessWidget {
  const _ReportSection({
    required this.view,
    required this.read,
    required this.data,
    this.currency,
    this.previewLimit,
  });
  final LedgerView view;
  final LedgerRead read;
  final Map<String, Object?> data;
  final String? currency;
  final int? previewLimit;
  @override
  Widget build(BuildContext context) {
    final rows = (data['rows'] as List<Object?>).cast<Map<String, Object?>>();
    final auditPassed =
        rows.isNotEmpty &&
        data['ok'] == true &&
        rows.every((row) => row['status'] == 'pass');
    final auditPending =
        view == LedgerView.audit &&
        rows.any((row) => row['status'] != 'pass' && row['status'] != 'fail');
    final failed = data['failed'];
    final positionGroups = view == LedgerView.positions
        ? PositionGroup.fromRows(
            rows,
            currencyFor: (row) => currency ?? read.currencyFor(row),
          )
        : null;
    final countLabel = positionGroups == null
        ? '${rows.length} 条'
        : previewLimit != null && positionGroups.length > previewLimit!
        ? '${positionGroups.length} 个标的 · 展示前 $previewLimit 个'
        : '${positionGroups.length} 个标的 · ${rows.length} 项持仓';
    const totals = [
      'totalPnl',
      'total',
      'totalCostBasis',
      'maxDrawdown',
      'totalLongPremiumPaid',
      'totalShortPremiumReceived',
      'allLongOptionsLossIfZero',
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (previewLimit != null)
            Text(
              countLabel,
              style: const TextStyle(
                fontSize: 12,
                height: 1.5,
                color: LyraColors.inkSecondary,
              ),
            )
          else
            Wrap(
              alignment: WrapAlignment.spaceBetween,
              crossAxisAlignment: WrapCrossAlignment.center,
              spacing: 12,
              runSpacing: 6,
              children: [
                Text(
                  currency == null ? '记录明细' : '$currency / ${view.title}',
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w500,
                    color: LyraColors.ink,
                  ),
                ),
                Text(
                  countLabel,
                  style: const TextStyle(fontSize: 12, color: LyraColors.muted),
                ),
              ],
            ),
          if (view == LedgerView.positions) ...[
            const SizedBox(height: 18),
            _PositionColumns(currency: currency),
            if (rows.any((row) => row['assetType'] == 'option'))
              const Padding(
                padding: EdgeInsets.only(top: 6, bottom: 4),
                child: Text(
                  '期权历史成本未乘合约乘数',
                  style: TextStyle(fontSize: 12, color: LyraColors.muted),
                ),
              ),
          ] else
            const SizedBox(height: 18),
          for (final field in totals.where(
            (field) => currency != null && data.containsKey(field),
          ))
            Padding(
              padding: const EdgeInsets.only(bottom: 18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    ledgerLabels[field]!,
                    style: const TextStyle(
                      fontSize: 12,
                      color: LyraColors.muted,
                    ),
                  ),
                  const SizedBox(height: 8),
                  FittedBox(
                    fit: BoxFit.scaleDown,
                    alignment: Alignment.centerLeft,
                    child: LyraAmount(
                      ledgerText(data[field]),
                      size: 30,
                      colorBySign: ledgerSignedAmountFields.contains(field),
                    ),
                  ),
                ],
              ),
            ),
          if (view == LedgerView.audit)
            Container(
              padding: const EdgeInsets.all(18),
              margin: const EdgeInsets.only(bottom: 14),
              decoration: BoxDecoration(
                color: auditPassed
                    ? LyraColors.accentTint
                    : LyraColors.surfaceSubtle,
                borderRadius: BorderRadius.circular(12),
              ),
              child: Row(
                children: [
                  Icon(auditPassed ? LyraIcons.confirm : LyraIcons.error),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      rows.isEmpty
                          ? '暂无检查结果'
                          : auditPending || data['ok'] is! bool
                          ? '部分检查结果待确认'
                          : auditPassed
                          ? '账本检查通过'
                          : failed is int && failed > 0
                          ? '$failed 项检查需要处理'
                          : '有检查需要核对',
                    ),
                  ),
                ],
              ),
            ),
          if (rows.isEmpty)
            LyraEmpty(
              title: '暂无${view.title}记录',
              message: '当前账号和筛选范围内没有对应数据。可调整筛选或导入交易记录。',
              icon: view.icon,
            ),
          if (rows.length > 1 &&
              [LedgerView.monthly, LedgerView.drawdown].contains(view)) ...[
            _DataPlot(
              rows: rows,
              field: view == LedgerView.drawdown ? 'cumulativePnl' : 'netPnl',
              line: view == LedgerView.drawdown,
            ),
            const SizedBox(height: 24),
          ],
          if (positionGroups != null)
            for (final group
                in previewLimit == null
                    ? positionGroups
                    : positionGroups.take(previewLimit!))
              _PositionGroupRow(
                group: group,
                currencyInHeader: currency != null,
              )
          else
            for (final (index, raw) in rows.indexed)
              _ReportRow(
                view: view,
                row: raw,
                index: index,
                currency: currency ?? read.currencyFor(raw),
                currencyInHeader: currency != null,
              ),
        ],
      ),
    );
  }
}

/// 多条同标的持仓折叠为一行；股票、期权及多空记录展开后各自保留。
class _PositionGroupRow extends ConsumerWidget {
  const _PositionGroupRow({
    required this.group,
    required this.currencyInHeader,
  });
  final PositionGroup group;
  final bool currencyInHeader;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (group.rows.length == 1) {
      return _ReportRow(
        view: LedgerView.positions,
        row: group.rows.single,
        index: 0,
        currency: group.currency,
        currencyInHeader: currencyInHeader,
      );
    }
    final types = group.rows
        .map((row) => ledgerText(row['assetType']))
        .toSet()
        .join(' · ');
    final symbol = ref.watch(amountsHiddenProvider)
        ? ledgerSecurityText(group.rows.first, amountsHidden: true)
        : group.symbol;
    return Theme(
      data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
      child: DecoratedBox(
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: LyraColors.hairline)),
        ),
        child: ExpansionTile(
          key: PageStorageKey(
            'position-group-${group.symbol}-${group.currency}-${group.rows}',
          ),
          tilePadding: EdgeInsets.zero,
          minTileHeight: 74,
          visualDensity: VisualDensity.standard,
          iconColor: LyraColors.accent,
          collapsedIconColor: LyraColors.inkSecondary,
          textColor: LyraColors.ink,
          collapsedTextColor: LyraColors.ink,
          childrenPadding: const EdgeInsets.fromLTRB(12, 4, 0, 12),
          title: Row(
            children: [
              StockAvatar(symbol: symbol, size: 44),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      symbol,
                      style: const TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      types,
                      style: const TextStyle(
                        fontSize: 12,
                        height: 1.5,
                        color: LyraColors.inkSecondary,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Text(
                      '${group.rows.length} 项持仓',
                      textAlign: TextAlign.end,
                      style: const TextStyle(
                        fontSize: 13,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      '展开看成本${currencyInHeader || group.currency == null ? '' : ' · ${group.currency}'}',
                      textAlign: TextAlign.end,
                      style: const TextStyle(
                        fontSize: 12,
                        height: 1.5,
                        color: LyraColors.muted,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          children: [
            for (final (index, row) in group.rows.indexed)
              _ReportRow(
                view: LedgerView.positions,
                row: row,
                index: index,
                currency: group.currency,
                currencyInHeader: currencyInHeader,
                nestedPosition: true,
              ),
          ],
        ),
      ),
    );
  }
}

/// 明确历史成本口径和币种；币种不统一时，每条记录继续单独标注。
class _PositionColumns extends StatelessWidget {
  const _PositionColumns({this.currency});
  final String? currency;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.only(bottom: 10),
    decoration: const BoxDecoration(
      border: Border(bottom: BorderSide(color: LyraColors.hairline)),
    ),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Expanded(
          child: Text(
            '证券 / 合约',
            style: TextStyle(fontSize: 12, color: LyraColors.inkSecondary),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            currency == null ? '历史成本' : '历史成本 · $currency',
            textAlign: TextAlign.end,
            style: const TextStyle(
              fontSize: 12,
              color: LyraColors.inkSecondary,
            ),
          ),
        ),
      ],
    ),
  );
}

/// 证券与历史成本同一主行；方向、合约信息与数量在次行。
/// [currency] 仅在表头无法统一标注币种时显示，避免跨币种混淆。
class _PositionCells extends ConsumerWidget {
  const _PositionCells({
    required this.record,
    this.currency,
    this.nested = false,
  });
  final bool nested;
  final Map<String, Object?> record;
  final String? currency;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden = ref.watch(amountsHiddenProvider);
    final option = record['assetType'] == 'option';
    final ticker = ledgerSecurityText(record, amountsHidden: hidden);
    final strike = hidden
        ? '••••'
        : LyraNumber.quantity(ledgerText(record['strike']));
    final contract = option
        ? '${ledgerText(record['expiry'])}\n$strike ${ledgerText(record['optionType'])}'
        : '股票 · ${ledgerText(record['direction'])}';
    final name = Text(
      nested
          ? (option ? '$strike ${ledgerText(record['optionType'])}' : '股票')
          : ticker,
      style: const TextStyle(
        fontSize: 16,
        height: 1.3,
        fontWeight: FontWeight.w500,
        color: LyraColors.ink,
      ),
    );
    final description = Text(
      nested && option ? ledgerText(record['expiry']) : contract,
      style: const TextStyle(
        fontSize: 12,
        height: 1.5,
        color: LyraColors.inkSecondary,
      ),
    );
    final amount = FittedBox(
      fit: BoxFit.scaleDown,
      alignment: Alignment.centerRight,
      child: LyraAmount(ledgerText(record['cost']), size: 17),
    );
    final quantity = Column(
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        Text(
          '${LyraNumber.quantity(ledgerText(record['quantity']))} ${option ? '张 · ${ledgerText(record['direction'])}' : '股'}',
          textAlign: TextAlign.end,
          style: const TextStyle(
            fontSize: 12,
            height: 1.5,
            color: LyraColors.inkSecondary,
          ),
        ),
        if (currency != null)
          Text(
            currency!,
            style: const TextStyle(fontSize: 12, color: LyraColors.muted),
          ),
      ],
    );
    return LayoutBuilder(
      builder: (context, constraints) {
        final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
        final contentWidth = constraints.maxWidth - (nested ? 0 : 58);
        // 大字模式为金额留出完整一行，不把辅助信息挤出视口。
        final stacked = contentWidth / textScale < 178;
        return Row(
          crossAxisAlignment: stacked
              ? CrossAxisAlignment.start
              : CrossAxisAlignment.center,
          children: [
            // 分组标题已展示标的图标，子行将空间留给合约和到期日。
            if (!nested) ...[
              StockAvatar(
                symbol: hidden ? ticker : record['underlying'] as String?,
                size: 44,
              ),
              const SizedBox(width: 14),
            ],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: stacked
                    ? [
                        name,
                        const SizedBox(height: 3),
                        description,
                        const SizedBox(height: 8),
                        Align(alignment: Alignment.centerRight, child: amount),
                        const SizedBox(height: 3),
                        quantity,
                      ]
                    : [
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Expanded(child: name),
                            const SizedBox(width: 8),
                            Expanded(child: amount),
                          ],
                        ),
                        const SizedBox(height: 4),
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Expanded(child: description),
                            const SizedBox(width: 8),
                            Expanded(child: quantity),
                          ],
                        ),
                      ],
              ),
            ),
          ],
        );
      },
    );
  }
}

/// 紧凑持仓行与展开明细共用同一权威记录，不构造虚假收益曲线。
class _ReportRow extends ConsumerWidget {
  const _ReportRow({
    required this.view,
    required this.row,
    required this.index,
    this.currency,
    this.currencyInHeader = false,
    this.nestedPosition = false,
  });
  final LedgerView view;
  final Map<String, Object?> row;
  final int index;
  final String? currency;
  final bool currencyInHeader;
  final bool nestedPosition;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden = ref.watch(amountsHiddenProvider);
    final record = view == LedgerView.extremes
        ? row['trade'] as Map<String, Object?>
        : row;
    final title = switch (view) {
      LedgerView.monthly => ledgerText(record['month']),
      LedgerView.allocation ||
      LedgerView.attribution => ledgerText(record['category']),
      LedgerView.drawdown => ledgerText(record['date']),
      LedgerView.risk ||
      LedgerView.holdingPeriod => ledgerDayRangeText(record['bucket']),
      LedgerView.streaks => ledgerText(record['kind']),
      LedgerView.audit => ledgerAuditTitle(record['check']),
      _ => ledgerSecurityText(record, amountsHidden: hidden),
    };
    final subtitle = switch (view) {
      LedgerView.positions || LedgerView.expiring =>
        '${ledgerText(record['assetType'])} · ${ledgerText(record['direction'])} · ${LyraNumber.quantity(ledgerText(record['quantity']))} ${record['assetType'] == 'option' ? '张' : '股'}${record['expiry'] != null ? '\n${ledgerText(record['expiry'])}' : ''}${record['strike'] != null ? ' · ${hidden ? '••••' : LyraNumber.quantity(ledgerText(record['strike']))}' : ''}${record['optionType'] != null ? ' ${ledgerText(record['optionType'])}' : ''}',
      LedgerView.trades || LedgerView.cashflow =>
        '${ledgerText(record['action'])} · ${ledgerText(record['transactionDate'])}',
      LedgerView.realized =>
        '${ledgerText(record['openDate'])} → ${ledgerText(record['closeDate'])}',
      LedgerView.extremes =>
        '${row['side'] == 'best'
            ? '最佳'
            : row['side'] == 'worst'
            ? '最差'
            : '交易排行'} #${ledgerText(row['rank'])} · ${ledgerText(record['closeDate'])}',
      LedgerView.streaks =>
        '${ledgerText(record['count'])} 笔 · ${ledgerText(record['startDate'])} — ${ledgerText(record['endDate'])}',
      LedgerView.audit => ledgerAuditStatusText(record['status']),
      _ => ledgerLabels[view.valueField]!,
    };
    final value = ledgerText(record[view.valueField]);
    final percent = record['allocationPct'] as String?;
    return Container(
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: LyraColors.hairline)),
      ),
      child: Theme(
        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
        child: ExpansionTile(
          key: PageStorageKey<String>(
            'report-${view.command}-$currency-$index-${row.toString()}',
          ),
          tilePadding: EdgeInsets.zero,
          minTileHeight: 74,
          visualDensity: VisualDensity.standard,
          iconColor: LyraColors.accent,
          collapsedIconColor: LyraColors.inkSecondary,
          textColor: LyraColors.ink,
          collapsedTextColor: LyraColors.ink,
          childrenPadding: const EdgeInsets.fromLTRB(4, 6, 4, 20),
          expandedCrossAxisAlignment: CrossAxisAlignment.stretch,
          title: view == LedgerView.positions
              ? _PositionCells(
                  record: record,
                  nested: nestedPosition,
                  currency: currencyInHeader ? null : currency,
                )
              : LayoutBuilder(
                  builder: (context, constraints) {
                    final hasAvatar = [
                      LedgerView.expiring,
                      LedgerView.trades,
                    ].contains(view);
                    final textScale =
                        MediaQuery.textScalerOf(context).scale(16) / 16;
                    final contentWidth =
                        constraints.maxWidth - (hasAvatar ? 54 : 0);
                    final stacked = contentWidth / textScale < 230;
                    final description = Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          title,
                          style: const TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w500,
                            height: 1.3,
                          ),
                        ),
                        const SizedBox(height: 5),
                        Text(
                          subtitle,
                          style: const TextStyle(
                            fontSize: 12,
                            height: 1.5,
                            color: LyraColors.inkSecondary,
                          ),
                        ),
                      ],
                    );
                    final amount = Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        FittedBox(
                          fit: BoxFit.scaleDown,
                          alignment: Alignment.centerRight,
                          child: LyraAmount(
                            value,
                            size: 17,
                            colorBySign: ledgerSignedAmountFields.contains(
                              view.valueField,
                            ),
                            isMonetary: view != LedgerView.audit,
                            decimals: view == LedgerView.audit ? 0 : 2,
                          ),
                        ),
                        const SizedBox(height: 5),
                        Text(
                          view == LedgerView.audit
                              ? '记录数'
                              : (currency ?? '按账户币种'),
                          style: const TextStyle(
                            fontSize: 12,
                            color: LyraColors.muted,
                          ),
                        ),
                      ],
                    );
                    return Row(
                      crossAxisAlignment: stacked
                          ? CrossAxisAlignment.start
                          : CrossAxisAlignment.center,
                      children: [
                        if (hasAvatar) ...[
                          StockAvatar(
                            symbol: hidden
                                ? ledgerSecurityText(
                                    record,
                                    amountsHidden: true,
                                  )
                                : (record['underlying'] ?? record['symbol'])
                                      as String?,
                          ),
                          const SizedBox(width: 12),
                        ],
                        Expanded(
                          child: stacked
                              // 大字或窄屏时让金融读数独占一行，保留正常
                              // 字号；超长金额仍可缩放并通过 tooltip 读原值。
                              ? Column(
                                  crossAxisAlignment:
                                      CrossAxisAlignment.stretch,
                                  children: [
                                    description,
                                    const SizedBox(height: 10),
                                    amount,
                                  ],
                                )
                              : Row(
                                  children: [
                                    Expanded(flex: 5, child: description),
                                    const SizedBox(width: 10),
                                    Flexible(flex: 4, child: amount),
                                  ],
                                ),
                        ),
                      ],
                    );
                  },
                ),
          children: [
            if (percent != null) ...[
              Text('历史成本占比 $percent%', style: const TextStyle(fontSize: 12)),
              const SizedBox(height: 8),
              LinearProgressIndicator(
                value: (double.parse(percent) / 100).clamp(0, 1),
                minHeight: 4,
                color: LyraColors.accent,
                backgroundColor: LyraColors.surfaceStrong,
              ),
              const SizedBox(height: 18),
            ],
            for (final field in record.keys.where(ledgerLabels.containsKey))
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: Text(
                        ledgerLabels[field]!,
                        style: const TextStyle(
                          fontSize: 12,
                          color: LyraColors.muted,
                        ),
                      ),
                    ),
                    const SizedBox(width: 18),
                    Expanded(
                      child: SelectionArea(
                        child: ledgerAmountFields.contains(field)
                            ? LyraAmount(
                                ledgerText(record[field]),
                                size: 13,
                                exact: true,
                                colorBySign: ledgerSignedAmountFields.contains(
                                  field,
                                ),
                                textAlign: TextAlign.end,
                              )
                            : Text(
                                ledgerFieldText(
                                  field,
                                  record,
                                  amountsHidden: hidden,
                                ),
                                textAlign: TextAlign.end,
                                style: const TextStyle(fontSize: 13),
                              ),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// 真实已实现数据的坐标投影，屏幕阅读器可通过下方逐行金额获得完整信息。
class _DataPlot extends ConsumerStatefulWidget {
  const _DataPlot({
    super.key,
    required this.rows,
    required this.field,
    required this.line,
    this.compact = false,
  });
  final List<Map<String, Object?>> rows;
  final String field;
  final bool line;
  final bool compact;
  @override
  ConsumerState<_DataPlot> createState() => _DataPlotState();
}

class _DataPlotState extends ConsumerState<_DataPlot> {
  int? _selected;

  /// 指针坐标仅选择记录索引，金额读数始终保留服务端十进制字符串。
  void _select(double x, double width) {
    final index = ((x / width) * widget.rows.length).floor().clamp(
      0,
      widget.rows.length - 1,
    );
    if (index != _selected) setState(() => _selected = index);
  }

  @override
  Widget build(BuildContext context) {
    if (ref.watch(amountsHiddenProvider)) {
      // 不保留坐标、选中读数或图形比例，防止隐藏后仍由形状推断金额规模。
      return Semantics(
        label: '金额已隐藏，收益图表已收起',
        excludeSemantics: true,
        child: SizedBox(
          key: const ValueKey('ledger-amounts-hidden'),
          height: widget.compact ? 142 : 222,
          child: const Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(LyraIcons.hideAmounts, color: LyraColors.muted, size: 22),
              SizedBox(height: 10),
              Text(
                '金额已隐藏',
                style: TextStyle(fontSize: 12, color: LyraColors.inkSecondary),
              ),
              SizedBox(height: 5),
              Text(
                '显示金额后查看收益图表',
                style: TextStyle(fontSize: 12, color: LyraColors.muted),
              ),
            ],
          ),
        ),
      );
    }
    final scheme = ref.watch(profitColorSchemeProvider);
    final rows = widget.rows;
    final field = widget.field;
    final line = widget.line;
    final selected = (_selected ?? rows.length - 1).clamp(0, rows.length - 1);
    final values = rows
        .map((row) => double.parse(row[field] as String))
        .toList();
    final dateField = line ? 'date' : 'month';
    return Semantics(
      label: line ? '累计已实现损益曲线，可点选日期查看金额' : '月度已实现损益柱形图，可点选月份查看金额',
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '${ledgerText(rows[selected][dateField])} / ${line ? '累计损益' : '当月损益'}',
                  style: const TextStyle(fontSize: 12, color: LyraColors.muted),
                ),
              ),
              const SizedBox(width: 12),
              Flexible(
                child: FittedBox(
                  fit: BoxFit.scaleDown,
                  alignment: Alignment.centerRight,
                  child: LyraAmount(
                    ledgerText(rows[selected][field]),
                    size: 15,
                    colorBySign: true,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          LayoutBuilder(
            builder: (context, constraints) => MouseRegion(
              cursor: SystemMouseCursors.precise,
              onHover: (event) =>
                  _select(event.localPosition.dx, constraints.maxWidth),
              child: GestureDetector(
                onTapDown: (details) =>
                    _select(details.localPosition.dx, constraints.maxWidth),
                onHorizontalDragUpdate: (details) =>
                    _select(details.localPosition.dx, constraints.maxWidth),
                child: SizedBox(
                  key: const ValueKey<String>('ledger-data-plot'),
                  height: widget.compact ? 100 : 180,
                  width: double.infinity,
                  child: TweenAnimationBuilder<double>(
                    tween: Tween(begin: 0, end: 1),
                    duration: MediaQuery.disableAnimationsOf(context)
                        ? Duration.zero
                        : const Duration(milliseconds: 800),
                    curve: Curves.easeOutCubic,
                    builder: (context, progress, _) => CustomPaint(
                      painter: _LedgerPlotPainter(
                        values,
                        line,
                        progress,
                        selected,
                        positiveColor: lyraProfitColor('1', scheme),
                        negativeColor: lyraProfitColor('-1', scheme),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Flexible(
                child: Text(
                  ledgerText(rows.first[dateField]),
                  style: const TextStyle(fontSize: 11, color: LyraColors.muted),
                ),
              ),
              const SizedBox(width: 12),
              Flexible(
                child: Text(
                  ledgerText(rows.last[dateField]),
                  textAlign: TextAlign.end,
                  style: const TextStyle(fontSize: 11, color: LyraColors.muted),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _LedgerPlotPainter extends CustomPainter {
  _LedgerPlotPainter(
    this.values,
    this.line,
    this.progress,
    this.selected, {
    required this.positiveColor,
    required this.negativeColor,
  });
  final List<double> values;
  final bool line;
  final double progress;
  final int selected;
  final Color positiveColor;
  final Color negativeColor;

  Color _colorFor(double value) => value == 0
      ? LyraColors.ink
      : value < 0
      ? negativeColor
      : positiveColor;
  @override
  void paint(Canvas canvas, Size size) {
    final low = math.min(0.0, values.reduce(math.min));
    final high = math.max(0.0, values.reduce(math.max));
    final extent = high == low ? 1.0 : high - low;
    double y(double value) => 12 + (high - value) / extent * (size.height - 28);
    final grid = Paint()
      ..color = LyraColors.hairline
      ..strokeWidth = 1;
    for (int i = 0; i < 4; i++) {
      final top = 12 + i * (size.height - 28) / 3;
      canvas.drawLine(Offset(0, top), Offset(size.width, top), grid);
    }
    canvas.drawLine(
      Offset(0, y(0)),
      Offset(size.width, y(0)),
      Paint()..color = LyraColors.hairlineStrong,
    );
    canvas.save();
    canvas.clipRect(Rect.fromLTWH(0, 0, size.width * progress, size.height));
    if (line) {
      // 单条累计记录只绘制居中的数据点，避免除以零或虚构一段走势。
      double xFor(int index) => values.length == 1
          ? size.width / 2
          : index * size.width / (values.length - 1);
      void segment(Offset from, Offset to, double value) => canvas.drawLine(
        from,
        to,
        Paint()
          ..color = _colorFor(value)
          ..strokeWidth = 2.5
          ..strokeCap = StrokeCap.round,
      );
      for (int i = 1; i < values.length; i++) {
        final previous = values[i - 1];
        final current = values[i];
        final from = Offset(xFor(i - 1), y(previous));
        final to = Offset(xFor(i), y(current));
        if ((previous < 0 && current > 0) || (previous > 0 && current < 0)) {
          // 穿越零轴时两侧各用真实盈亏方向，避免整条曲线被涂成盈利色。
          final crossing = Offset.lerp(
            from,
            to,
            previous.abs() / (previous.abs() + current.abs()),
          )!;
          segment(from, crossing, previous);
          segment(crossing, to, current);
        } else {
          segment(from, to, current == 0 ? previous : current);
        }
      }
      final point = Offset(xFor(selected), y(values[selected]));
      canvas.drawLine(
        Offset(point.dx, 0),
        Offset(point.dx, size.height),
        Paint()..color = LyraColors.hairlineStrong,
      );
      canvas.drawCircle(point, 5, Paint()..color = LyraColors.surface);
      canvas.drawCircle(point, 3, Paint()..color = _colorFor(values[selected]));
    } else {
      final step = size.width / values.length;
      for (int i = 0; i < values.length; i++) {
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            Rect.fromLTRB(
              i * step + step * .15,
              math.min(y(0), y(values[i])),
              (i + 1) * step - step * .15,
              math.max(y(0), y(values[i])) + 1,
            ),
            const Radius.circular(3),
          ),
          Paint()
            ..color = _colorFor(values[i])
                .withValues(alpha: i == selected ? 1 : .65),
        );
      }
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(_LedgerPlotPainter oldDelegate) =>
      oldDelegate.values != values ||
      oldDelegate.progress != progress ||
      oldDelegate.line != line ||
      oldDelegate.selected != selected ||
      oldDelegate.positiveColor != positiveColor ||
      oldDelegate.negativeColor != negativeColor;
}
