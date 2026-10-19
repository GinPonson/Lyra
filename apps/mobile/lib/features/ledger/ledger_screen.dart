import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_date.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_number.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/design/lyra_theme.dart';
import '../../core/design/stock_avatar.dart';
import '../../core/design/lyra_widgets.dart';
import '../../core/preferences/app_preferences.dart';
import '../market/market_history_models.dart';
import '../market/market_history_widgets.dart';
import '../market/market_underlying_quote_widgets.dart';
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
  final GlobalKey<_LedgerPanelState> _panelKey = GlobalKey<_LedgerPanelState>();

  @override
  Widget build(BuildContext context) {
    final views = widget.stocks
        ? [LedgerView.positions, LedgerView.expiring, LedgerView.allocation]
        : [LedgerView.trades, LedgerView.realized, LedgerView.cashflow];
    final view = views[_tab];
    final actions = <Widget>[
      if (view.searchable)
        IconButton(
          tooltip: '搜索证券',
          onPressed: () => _panelKey.currentState?.toggleSearch(),
          icon: const LyraPrimaryIcon(LyraPrimaryGlyph.search, size: 21),
        ),
      if (view.dateFilter ||
          view == LedgerView.trades ||
          view == LedgerView.expiring ||
          view == LedgerView.allocation ||
          view == LedgerView.attribution)
        IconButton(
          tooltip: '筛选',
          onPressed: () => _panelKey.currentState?.openFilters(),
          icon: const LyraPrimaryIcon(LyraPrimaryGlyph.filter, size: 21),
        ),
      if (widget.stocks)
        PopupMenuButton<LedgerView>(
          tooltip: '更多分析',
          icon: const LyraPrimaryIcon(LyraPrimaryGlyph.more, size: 21),
          onSelected: (view) => Navigator.push(
            context,
            MaterialPageRoute<void>(builder: (_) => ReportScreen(view: view)),
          ),
          itemBuilder: (context) => [
            for (final report in [LedgerView.risk, LedgerView.stress])
              PopupMenuItem(value: report, child: Text(report.title)),
          ],
        ),
    ];
    return RefreshIndicator(
      onRefresh: () async {
        await _panelKey.currentState?.refreshFromStart();
      },
      child: LyraPage(
        storageKey: 'ledger-scroll',
        children: [
          LyraFinancialHeader(
            title: widget.stocks ? '股票' : '交易',
            actions: actions,
          ),
          const SizedBox(height: 8),
          _LedgerTabs(
            views: views,
            selectedIndex: _tab,
            onSelected: (index) => setState(() => _tab = index),
          ),
          const SizedBox(height: 14),
          LedgerPanel(key: _panelKey, view: view, onConnect: widget.onConnect),
        ],
      ),
    );
  }
}

/// 轻量文字页签只以底部短线标识当前视图，避免按钮外框抢占首屏空间。
class _LedgerTabs extends StatelessWidget {
  const _LedgerTabs({
    required this.views,
    required this.selectedIndex,
    required this.onSelected,
  });

  final List<LedgerView> views;
  final int selectedIndex;
  final ValueChanged<int> onSelected;

  @override
  Widget build(BuildContext context) => Container(
    decoration: const BoxDecoration(
      border: Border(bottom: BorderSide(color: LyraColors.hairline)),
    ),
    child: LayoutBuilder(
      builder: (context, constraints) {
        final tabs = [
          for (final (index, view) in views.indexed) _tab(context, view, index),
        ];
        // 放大字体时允许水平滚动，避免三个中文页签被强行压到相邻文字上。
        if (constraints.maxWidth < 360 &&
            MediaQuery.textScalerOf(context).scale(15) > 18) {
          return SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            primary: false,
            child: Row(
              children: [
                for (final tab in tabs)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    child: tab,
                  ),
              ],
            ),
          );
        }
        return Row(children: [for (final tab in tabs) Expanded(child: tab)]);
      },
    ),
  );

  /// 构建可访问的页签；[view] 提供业务标题，[index] 对应当前视图目录位置。
  Widget _tab(BuildContext context, LedgerView view, int index) => Semantics(
    selected: selectedIndex == index,
    button: true,
    child: InkWell(
      onTap: () => onSelected(index),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(
            height: math.max(
              46,
              MediaQuery.textScalerOf(context).scale(15) * 1.5 + 10,
            ),
            child: Center(
              child: Text(
                _tabTitle(view),
                style: TextStyle(
                  color: selectedIndex == index
                      ? LyraColors.accent
                      : LyraColors.inkSecondary,
                  fontSize: 15,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ),
          ),
          Container(
            height: 2,
            width: selectedIndex == index ? 48 : 0,
            color: LyraColors.accent,
          ),
        ],
      ),
    ),
  );

  String _tabTitle(LedgerView view) => switch (view) {
    LedgerView.positions => '持仓',
    LedgerView.expiring => '期权到期',
    LedgerView.allocation => '仓位配置',
    LedgerView.trades => '全部',
    LedgerView.realized => '已实现',
    LedgerView.cashflow => '现金流',
    _ => view.title,
  };
}

/// 次级分析保持普通返回导航，AI 不占据金融报告入口。
class ReportScreen extends StatelessWidget {
  const ReportScreen({super.key, required this.view});
  final LedgerView view;
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: Text(view.title),
      actions: const [
        LyraAmountsButton(),
        AccountScopeButton(compact: true),
        SizedBox(width: 12),
      ],
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
  const LedgerTrendPreview({super.key, this.currency, this.embedded = false});

  /// When embedded, limits the read and chart to this server currency group.
  final String? currency;

  /// The caller owns the surrounding card and heading when this is true.
  final bool embedded;

  @override
  ConsumerState<LedgerTrendPreview> createState() => _LedgerTrendPreviewState();
}

class _LedgerTrendPreviewState extends ConsumerState<LedgerTrendPreview> {
  late LedgerView _view;

  @override
  void initState() {
    super.initState();
    // The homepage preview uses the cumulative report; standalone analytics
    // retains its previous monthly default.
    _view = widget.embedded ? LedgerView.drawdown : LedgerView.monthly;
  }

  @override
  void didUpdateWidget(covariant LedgerTrendPreview oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.embedded != widget.embedded) {
      _view = widget.embedded ? LedgerView.drawdown : LedgerView.monthly;
    }
  }

  @override
  Widget build(BuildContext context) {
    final scope = ref.watch(accountScopeProvider);
    final query = (
      path: _view.path,
      // 既有报告端点按 scope 返回币种组；显示层从同一响应中选择当前币种。
      parameters: '',
      scope: scope.id,
    );
    final result = ref.watch(ledgerReadProvider(query));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (!widget.embedded)
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
          )
        else
          Align(
            alignment: Alignment.centerRight,
            child: Wrap(
              spacing: 8,
              children: [
                for (final (view, label) in [
                  (LedgerView.monthly, '按月'),
                  (LedgerView.drawdown, '累计'),
                ])
                  TextButton(
                    onPressed: () => setState(() => _view = view),
                    style: TextButton.styleFrom(
                      minimumSize: const Size(44, 28),
                      padding: const EdgeInsets.symmetric(horizontal: 4),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                    child: Container(
                      padding: const EdgeInsets.only(bottom: 5),
                      decoration: BoxDecoration(
                        border: Border(
                          bottom: BorderSide(
                            color: _view == view
                                ? LyraColors.accent
                                : Colors.transparent,
                            width: 2,
                          ),
                        ),
                      ),
                      child: Text(
                        label,
                        style: TextStyle(
                          fontSize: 13,
                          color: _view == view
                              ? LyraColors.accent
                              : LyraColors.muted,
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        SizedBox(height: widget.embedded ? 4 : 14),
        result.when(
          loading: () => const SizedBox(
            height: 160,
            child: Center(child: CircularProgressIndicator()),
          ),
          error: (error, _) => LyraLoadError(
            error: error,
            onRetry: () => ref.invalidate(ledgerReadProvider(query)),
          ),
          data: (read) {
            final sections = widget.embedded && widget.currency != null
                ? read.sections
                      .where((section) => section.currency == widget.currency)
                      .toList(growable: false)
                : read.sections;
            return Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (sections.isEmpty)
                  const Text(
                    '暂无收益记录',
                    style: TextStyle(color: LyraColors.muted),
                  ),
                for (final section in sections)
                  Padding(
                    padding: EdgeInsets.only(bottom: widget.embedded ? 0 : 12),
                    child: Builder(
                      builder: (context) {
                        final rows = (section.data['rows'] as List<Object?>)
                            .cast<Map<String, Object?>>();
                        return Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            if (!widget.embedded) ...[
                              Text(
                                '${section.currency ?? '按账户币种'} · 已实现损益',
                                style: const TextStyle(
                                  fontSize: 12,
                                  color: LyraColors.muted,
                                ),
                              ),
                              const SizedBox(height: 12),
                            ],
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
                                compact: widget.embedded,
                              ),
                          ],
                        );
                      },
                    ),
                  ),
              ],
            );
          },
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
        read.when(
          loading: () => const LyraSkeleton(),
          error: (error, _) => LyraLoadError(
            error: error,
            onRetry: () => ref.invalidate(ledgerReadProvider(query)),
          ),
          data: (result) => _HoldingsCard(
            read: result,
            previewLimit: 3,
            onOpen: onOpen,
            overview: true,
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
  DateTime _analysisDate = DateTime.now();
  DateTimeRange? _dateRange;
  final List<String?> _requestedCursors = [null];
  final List<_LoadedLedgerPage> _loadedPages = [];
  ScrollPosition? _scrollPosition;
  String? _queryIdentity;
  String? _nextCursor;
  String? _paginationError;
  int _queryGeneration = 0;
  bool _loadingNext = false;

  static const double _loadThreshold = 380;

  bool get searchIsOpen => _searchOpen;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final nextPosition = Scrollable.maybeOf(context)?.position;
    if (identical(nextPosition, _scrollPosition)) return;
    _scrollPosition?.removeListener(_onScroll);
    _scrollPosition = nextPosition;
    _scrollPosition?.addListener(_onScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) => _maybeLoadNext());
  }

  @override
  void didUpdateWidget(covariant LedgerPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.view == widget.view) return;
    _symbol = '';
    _search.clear();
    _searchOpen = false;
    _groupBy = 'underlying';
    _action = null;
    _days = 30;
    _analysisDate = DateTime.now();
    _dateRange = null;
    _queryIdentity = null;
    _resetPagination(notify: false);
  }

  @override
  void dispose() {
    _scrollPosition?.removeListener(_onScroll);
    _search.dispose();
    super.dispose();
  }

  /// 筛选和账户范围变化会使旧游标与旧响应失效，避免跨查询拼接记录。
  void _resetPagination({bool notify = true}) {
    void reset() {
      ++_queryGeneration;
      _requestedCursors
        ..clear()
        ..add(null);
      _loadedPages.clear();
      _nextCursor = null;
      _paginationError = null;
      _loadingNext = false;
    }

    if (notify && mounted) {
      setState(reset);
    } else {
      reset();
    }
  }

  /// 应用筛选时清空已读页，让新查询只从第一页开始。
  void _filter(VoidCallback change) => setState(() {
    change();
    _resetPagination(notify: false);
  });

  /// 页头搜索按钮只切换输入框，不常驻空搜索栏或提示文案。
  void toggleSearch() => setState(() {
    _searchOpen = !_searchOpen;
    _search.text = _symbol;
    if (!_searchOpen) FocusScope.of(context).unfocus();
  });

  /// 查询即将到达末尾时只发起一个 nextCursor 请求。
  void _onScroll() => _maybeLoadNext();

  void _maybeLoadNext() {
    if (!mounted ||
        !widget.view.paginated ||
        _loadedPages.isEmpty ||
        _loadingNext ||
        _paginationError != null) {
      return;
    }
    final cursor = _nextCursor;
    if (cursor == null) return;
    final position = _scrollPosition;
    if (position == null ||
        !position.hasContentDimensions ||
        position.extentAfter > _loadThreshold) {
      return;
    }
    if (_requestedCursors.contains(cursor)) {
      _stopForPaginationError('服务器返回了重复分页游标，已停止自动加载。');
      return;
    }
    setState(() {
      _requestedCursors.add(cursor);
      _loadingNext = true;
    });
  }

  /// 游标协议错误停止自动请求，并明确给出从第一页恢复的操作。
  void _stopForPaginationError(String message) {
    if (!mounted) return;
    setState(() {
      _loadingNext = false;
      _paginationError = message;
    });
  }

  /// 新页只在查询身份和账本版本都相同后加入当前列表。
  void _queuePageAcceptance(
    LedgerRead page,
    String? cursor,
    String identity,
    int generation,
  ) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted ||
          _paginationError != null ||
          generation != _queryGeneration ||
          identity != _queryIdentity ||
          _requestedCursors.last != cursor ||
          _loadedPages.any((loaded) => loaded.cursor == cursor)) {
        return;
      }
      final first = _loadedPages.isEmpty ? null : _loadedPages.first.read;
      if (first != null &&
          first.metadata['ledgerVersion'] != page.metadata['ledgerVersion']) {
        _stopForPaginationError('账本版本已更新，不能合并新旧记录。请从头刷新。');
        return;
      }
      final nextValue = page.data['nextCursor'];
      if (nextValue != null && (nextValue is! String || nextValue.isEmpty)) {
        _stopForPaginationError('服务器返回了无效分页游标，已停止自动加载。');
        return;
      }
      final next = nextValue as String?;
      if (next != null && _requestedCursors.contains(next)) {
        _stopForPaginationError('服务器返回了重复分页游标，已停止自动加载。');
        return;
      }
      setState(() {
        _loadedPages.add(_LoadedLedgerPage(cursor: cursor, read: page));
        _nextCursor = next;
        _loadingNext = false;
        _paginationError = null;
      });
      // 首屏可能短于视口。布局完成后继续消费剩余游标，避免要求用户
      // 对着空白区域反复拖动来触发同一个加载动作。
      WidgetsBinding.instance.addPostFrameCallback((_) => _maybeLoadNext());
    });
  }

  /// 刷新从头读取，并等待首屏完成；失败仍由当前查询错误态显示。
  Future<void> refreshFromStart() async {
    final session = ref.read(sessionProvider).value;
    if (session is! SessionAuthenticated) return;
    _analysisDate = DateTime.now();
    _resetPagination();
    final scope = ref.read(accountScopeProvider);
    final query = _queryFor(scope.id, null);
    try {
      final _ = await ref.refresh(ledgerReadProvider(query).future);
    } on Object {
      // RefreshIndicator must finish its gesture; the watched provider retains
      // AsyncError so the page can show the real failure and retry action.
    }
  }

  /// 搜索提交后收起键盘，并复用筛选入口重置分页游标。
  void _applySearch() {
    FocusScope.of(context).unfocus();
    _filter(() {
      _symbol = _search.text.trim().toUpperCase();
      _searchOpen = false;
    });
  }

  /// 在独立筛选面板中编辑日期、动作和分析分组，应用时一次性重置查询。
  Future<void> openFilters() async {
    var selectedDates = _dateRange;
    var selectedAction = _action;
    var selectedDays = _days;
    var selectedGroupBy = _groupBy;
    final applied = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (context, setSheetState) => Padding(
          padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                '筛选${widget.view.title}',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 16),
              if (widget.view.dateFilter)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(LyraIcons.dataCutoff),
                  title: const Text('交易日期'),
                  subtitle: Text(
                    selectedDates == null
                        ? '全部日期'
                        : '${_dayLabel(selectedDates!.start)} — ${_dayLabel(selectedDates!.end)}',
                  ),
                  onTap: () async {
                    final range = await showDateRangePicker(
                      context: sheetContext,
                      initialDateRange: selectedDates,
                      firstDate: DateTime(1970),
                      lastDate: DateTime.now(),
                      helpText: '选择交易日期范围',
                      saveText: '应用',
                    );
                    if (range != null) {
                      setSheetState(() => selectedDates = range);
                    }
                  },
                ),
              if (widget.view == LedgerView.trades)
                DropdownButtonFormField<String?>(
                  initialValue: selectedAction,
                  decoration: const InputDecoration(labelText: '交易动作'),
                  items: [
                    const DropdownMenuItem<String?>(
                      value: null,
                      child: Text('全部动作'),
                    ),
                    for (final action in [
                      'Buy',
                      'Sell',
                      'Buy to Open',
                      'Sell to Open',
                      'Buy to Close',
                      'Sell to Close',
                      'Expired',
                      'Assigned',
                    ])
                      DropdownMenuItem(
                        value: action,
                        child: Text(ledgerText(action)),
                      ),
                  ],
                  onChanged: (value) =>
                      setSheetState(() => selectedAction = value),
                ),
              if (widget.view == LedgerView.expiring)
                DropdownButtonFormField<int>(
                  initialValue: selectedDays,
                  decoration: const InputDecoration(labelText: '到期范围'),
                  items: [7, 30, 60, 90, 180, 366]
                      .map(
                        (days) => DropdownMenuItem(
                          value: days,
                          child: Text('未来 $days 天'),
                        ),
                      )
                      .toList(),
                  onChanged: (value) {
                    if (value != null) {
                      setSheetState(() => selectedDays = value);
                    }
                  },
                ),
              if ([
                LedgerView.allocation,
                LedgerView.attribution,
              ].contains(widget.view))
                DropdownButtonFormField<String>(
                  initialValue: selectedGroupBy,
                  decoration: const InputDecoration(labelText: '分组方式'),
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
                    if (value != null) {
                      setSheetState(() => selectedGroupBy = value);
                    }
                  },
                ),
              const SizedBox(height: 18),
              Row(
                children: [
                  TextButton(
                    onPressed: () => Navigator.pop(sheetContext, false),
                    child: const Text('取消'),
                  ),
                  const Spacer(),
                  FilledButton(
                    onPressed: () => Navigator.pop(sheetContext, true),
                    child: const Text('应用筛选'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
    if (!mounted || applied != true) return;
    _filter(() {
      _dateRange = selectedDates;
      _action = selectedAction;
      _days = selectedDays;
      _groupBy = selectedGroupBy;
    });
  }

  String _dayLabel(DateTime value) =>
      '${value.year}-${value.month.toString().padLeft(2, '0')}-'
      '${value.day.toString().padLeft(2, '0')}';

  /// 日期分析显式固定本次页面的基准日；刷新时再更新，避免跨午夜混用查询缓存。
  /// [cursor] 原样续读当前查询；返回服务端公开 Query 参数，含必填的 as_of。
  Map<String, String> _parameters({required String? cursor}) => {
    ...widget.view.parameters,
    if (_symbol.isNotEmpty)
      (widget.view == LedgerView.positions ? 'underlying' : 'symbol'): _symbol,
    if (widget.view == LedgerView.expiring) 'days': '$_days',
    if ([LedgerView.expiring, LedgerView.risk].contains(widget.view))
      'as_of': _dayLabel(_analysisDate),
    if ([LedgerView.allocation, LedgerView.attribution].contains(widget.view))
      'group_by': _groupBy,
    'action': ?_action,
    if (_dateRange != null) 'fromDate': _dayLabel(_dateRange!.start),
    if (_dateRange != null) 'toDate': _dayLabel(_dateRange!.end),
    'cursor': ?cursor,
  };

  LedgerQuery _queryFor(String scope, String? cursor) => (
    path: widget.view.path,
    parameters: Uri(queryParameters: _parameters(cursor: cursor)).query,
    scope: scope,
  );

  String _identityFor(SessionAuthenticated session, String scope) {
    final client = ref.read(sessionProvider.notifier).client;
    final stableSession =
        '${preferencesIdentityKey(session.snapshot)}:${session.snapshot.deviceId}:'
        '${client == null ? 'missing-client' : identityHashCode(client)}';
    final parameters = _parameters(cursor: null)..remove('cursor');
    return '$stableSession|${widget.view.path}|$scope|'
        '${Uri(queryParameters: parameters).query}';
  }

  @override
  Widget build(BuildContext context) {
    final sessionState = ref.watch(sessionProvider);
    if (sessionState.isLoading) {
      if (_queryIdentity != null) {
        _queryIdentity = null;
        _resetPagination(notify: false);
      }
      return const LyraSkeleton();
    }
    if (sessionState.hasError) {
      if (_queryIdentity != null) {
        _queryIdentity = null;
        _resetPagination(notify: false);
      }
      return LyraLoadError(
        error: sessionState.error!,
        onRetry: () => ref.invalidate(sessionProvider),
      );
    }
    final session = sessionState.value;
    if (session is! SessionAuthenticated) {
      if (_queryIdentity != null) {
        _queryIdentity = null;
        _resetPagination(notify: false);
      }
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
    final identity = _identityFor(session, scope.id);
    if (_queryIdentity != identity) {
      _queryIdentity = identity;
      _resetPagination(notify: false);
    }
    final cursor = _requestedCursors.last;
    final query = _queryFor(scope.id, cursor);
    final result = ref.watch(ledgerReadProvider(query));
    if (widget.view.paginated) {
      result.whenData(
        (page) =>
            _queuePageAcceptance(page, cursor, identity, _queryGeneration),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (_searchOpen) ...[
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
          const SizedBox(height: 8),
        ],
        if (_symbol.isNotEmpty || _dateRange != null || _action != null) ...[
          Wrap(
            spacing: 8,
            runSpacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              if (_symbol.isNotEmpty)
                TextButton.icon(
                  onPressed: toggleSearch,
                  icon: const Icon(LyraIcons.search, size: 16),
                  label: Text('标的 · $_symbol'),
                ),
              if (_dateRange != null)
                _FilterLabel(
                  '${_dayLabel(_dateRange!.start)} — ${_dayLabel(_dateRange!.end)}',
                ),
              if (_action != null) _FilterLabel(ledgerText(_action)),
              if (_symbol.isNotEmpty || _dateRange != null || _action != null)
                TextButton(
                  onPressed: () => _filter(() {
                    _symbol = '';
                    _search.clear();
                    _dateRange = null;
                    _action = null;
                  }),
                  child: const Text('清除'),
                ),
            ],
          ),
          const SizedBox(height: 8),
        ],
        if (widget.view.paginated)
          _buildPaginatedResult(result, scope.label, query)
        else
          result.when(
            loading: () => const LyraSkeleton(),
            error: (error, _) => LyraLoadError(
              error: error,
              onRetry: () => ref.invalidate(ledgerReadProvider(query)),
            ),
            data: (read) => _buildSingleRead(read, scope.label),
          ),
      ],
    );
  }

  Widget _buildSingleRead(LedgerRead read, String scopeLabel) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      if (widget.view == LedgerView.positions)
        _HoldingsCard(read: read, underlying: _symbol.isEmpty ? null : _symbol)
      else if (read.sections.isEmpty)
        const LyraEmpty(title: '还没有可分析的记录', message: '导入完整交易记录后，这里将呈现相应结果。')
      else
        for (final section in read.sections)
          _ReportSection(
            view: widget.view,
            read: read,
            data: section.data,
            currency: section.currency,
          ),
      if (widget.view != LedgerView.positions) ...[
        const SizedBox(height: 24),
        _ReadTimestamp(read: read, scopeLabel: scopeLabel),
      ],
    ],
  );

  Widget _buildPaginatedResult(
    AsyncValue<LedgerRead> result,
    String scopeLabel,
    LedgerQuery query,
  ) {
    if (_loadedPages.isEmpty) {
      if (_paginationError case final message?) {
        return LyraEmpty(
          title: '记录未能加载',
          message: message,
          action: TextButton(
            onPressed: refreshFromStart,
            child: const Text('从头刷新'),
          ),
        );
      }
      return result.when(
        loading: () => const LyraSkeleton(),
        error: (error, _) => LyraLoadError(
          error: error,
          onRetry: () => ref.invalidate(ledgerReadProvider(query)),
        ),
        data: (_) => const LyraSkeleton(),
      );
    }
    final read = _loadedPages.first.read;
    final sections = _mergeLoadedSections();
    final hasRows = sections.any(
      (section) => (section.data['rows'] as List<Object?>).isNotEmpty,
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (!hasRows && _nextCursor == null)
          LyraEmpty(
            title: '暂无${widget.view.title}记录',
            message: '当前账号和筛选范围内没有对应数据。可调整筛选或导入交易记录。',
            icon: widget.view.icon,
          )
        else
          for (final section in sections)
            _TransactionSection(
              view: widget.view,
              read: read,
              section: section,
            ),
        if (_paginationError case final message?) ...[
          const SizedBox(height: 8),
          LyraCard(
            inset: true,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  message,
                  style: const TextStyle(color: LyraColors.negative),
                ),
                const SizedBox(height: 8),
                TextButton(
                  onPressed: refreshFromStart,
                  child: const Text('从头刷新'),
                ),
              ],
            ),
          ),
        ] else if (result.hasError)
          LyraLoadError(
            error: result.error!,
            onRetry: () {
              setState(() => _loadingNext = true);
              ref.invalidate(ledgerReadProvider(query));
            },
          )
        else if (_loadingNext || result.isLoading)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 18),
            child: Center(child: CircularProgressIndicator()),
          ),
        const SizedBox(height: 24),
        _ReadTimestamp(read: read, scopeLabel: scopeLabel),
      ],
    );
  }

  List<_MergedLedgerSection> _mergeLoadedSections() {
    final order = <String?>[];
    final rows = <String?, List<Map<String, Object?>>>{};
    for (final page in _loadedPages) {
      for (final section in page.read.sections) {
        for (final row
            in (section.data['rows'] as List<Object?>)
                .cast<Map<String, Object?>>()) {
          // Ungrouped trade endpoints may contain accounts in several
          // currencies. Resolve each row from metadata before joining pages.
          final currency = section.currency ?? page.read.currencyFor(row);
          if (!rows.containsKey(currency)) {
            order.add(currency);
            rows[currency] = <Map<String, Object?>>[];
          }
          rows[currency]!.add(row);
        }
      }
    }
    return [
      for (final currency in order)
        _MergedLedgerSection(
          currency: currency,
          data: <String, Object?>{
            'rows': List<Map<String, Object?>>.unmodifiable(rows[currency]!),
          },
        ),
    ];
  }
}

class _LoadedLedgerPage {
  const _LoadedLedgerPage({required this.cursor, required this.read});

  final String? cursor;
  final LedgerRead read;
}

class _MergedLedgerSection {
  const _MergedLedgerSection({required this.currency, required this.data});

  final String? currency;
  final Map<String, Object?> data;
}

class _FilterLabel extends StatelessWidget {
  const _FilterLabel(this.label);

  final String label;

  @override
  Widget build(BuildContext context) => Text(
    label,
    style: const TextStyle(fontSize: 12, color: LyraColors.inkSecondary),
  );
}

/// 展示所用账本时点与当前范围；保留首屏原始 metadata，不伪造合并时点。
class _ReadTimestamp extends StatelessWidget {
  const _ReadTimestamp({required this.read, required this.scopeLabel});

  final LedgerRead read;
  final String scopeLabel;

  @override
  Widget build(BuildContext context) => Text(
    '$scopeLabel\n数据截至 ${lyraDateTime(read.metadata['asOf'] is String ? read.metadata['asOf'] as String : null)}',
    style: const TextStyle(fontSize: 11, height: 1.6, color: LyraColors.muted),
  );
}

/// 股票页和总览共用同一张历史成本组卡，不把客户端重新计算的金额写回列表。
class _HoldingsCard extends ConsumerWidget {
  const _HoldingsCard({
    required this.read,
    this.underlying,
    this.previewLimit,
    this.onOpen,
    this.overview = false,
  });

  final LedgerRead read;
  final String? underlying;
  final int? previewLimit;
  final VoidCallback? onOpen;
  final bool overview;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sections = read.sections;
    final groupsBySection = <List<PositionGroup>>[
      for (final section in sections)
        PositionGroup.fromRows(
          (section.data['rows'] as List<Object?>).cast<Map<String, Object?>>(),
          currencyFor: (row) => section.currency ?? read.currencyFor(row),
        ),
    ];
    final groupCount = groupsBySection.fold<int>(
      0,
      (count, groups) => count + groups.length,
    );
    final singleCurrency = sections.length == 1
        ? sections.single.currency
        : null;
    return LyraCard(
      padding: overview
          ? const EdgeInsets.symmetric(horizontal: 14, vertical: 12)
          : const EdgeInsets.fromLTRB(16, 16, 16, 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (overview)
            Row(
              children: [
                Tooltip(
                  message: '查看全部持仓',
                  child: TextButton(
                    onPressed: onOpen,
                    style: TextButton.styleFrom(
                      minimumSize: const Size(0, 28),
                      padding: EdgeInsets.zero,
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                    child: const Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          '持仓',
                          style: TextStyle(
                            fontSize: 17,
                            fontWeight: FontWeight.w600,
                            color: LyraColors.ink,
                          ),
                        ),
                        SizedBox(width: 6),
                        Icon(LyraIcons.disclosure, size: 19),
                      ],
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    '历史成本${singleCurrency == null ? '' : ' · $singleCurrency'}',
                    textAlign: TextAlign.right,
                    style: const TextStyle(
                      fontSize: 12,
                      color: LyraColors.inkSecondary,
                    ),
                  ),
                ),
              ],
            )
          else ...[
            Row(
              children: [
                const Text(
                  '历史成本',
                  style: TextStyle(fontSize: 16, fontWeight: FontWeight.w500),
                ),
                const SizedBox(width: 8),
                Tooltip(
                  message:
                      '历史开仓成本，不代表实时市值。'
                      '\n期权已计入合约乘数；空头为开仓收取的权利金。'
                      '\n数据截至 ${lyraDateTime(read.metadata['asOf'] as String?)}',
                  child: const Icon(
                    LyraIcons.about,
                    color: LyraColors.inkSecondary,
                    size: 17,
                  ),
                ),
                const Spacer(),
                Text(
                  sections.length > 1 ? '按币种分列' : (singleCurrency ?? '—'),
                  style: const TextStyle(
                    fontSize: 12,
                    color: LyraColors.inkSecondary,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
          ],
          if (groupCount == 0)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 20),
              child: Text('暂无持仓记录', style: TextStyle(color: LyraColors.muted)),
            ),
          for (final (sectionIndex, section) in sections.indexed) ...[
            if (sections.length > 1) ...[
              if (sectionIndex > 0) const SizedBox(height: 10),
              Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: Text(
                  section.currency ?? '按账户币种',
                  style: const TextStyle(
                    fontSize: 12,
                    color: LyraColors.inkSecondary,
                  ),
                ),
              ),
            ],
            for (final (groupIndex, group)
                in groupsBySection[sectionIndex]
                    .take(previewLimit ?? groupsBySection[sectionIndex].length)
                    .indexed)
              if (overview && group.rows.length == 1)
                _HoldingRecordRow(
                  record: group.rows.single,
                  currency: sections.length > 1 || singleCurrency != null
                      ? null
                      : group.currency,
                  compact: true,
                )
              else
                _HoldingGroupRow(
                  group: group,
                  currencyInHeader:
                      sections.length > 1 || singleCurrency != null,
                  compact: overview,
                  groupIndex: groupIndex,
                  ledgerVersion: (read.metadata['ledgerVersion'] as num)
                      .toInt(),
                  originScope: ref.read(accountScopeProvider).id,
                  onOpenHistory: overview
                      ? null
                      : (position) => _openHistory(
                          context,
                          ref,
                          position,
                          currency: section.currency,
                          target: MarketHistoryTarget.position,
                        ),
                  onOpenUnderlyingHistory: overview
                      ? null
                      : (position) => _openHistory(
                          context,
                          ref,
                          position,
                          currency: section.currency,
                          target: MarketHistoryTarget.underlying,
                        ),
                ),
          ],
          if (!overview)
            MarketPositionsPanel(
              ledger: read,
              underlying: underlying,
              compact: true,
            ),
        ],
      ),
    );
  }

  /// 使用真实持仓授权查看价格，返回在行情面板关闭时完成的 Future。
  ///
  /// [position] 保留账户和原证券身份；[currency] 来自所属账本科目，
  /// [target] 仅决定读取该证券还是其正股，不会构造新的股票持仓。
  Future<void> _openHistory(
    BuildContext context,
    WidgetRef ref,
    Map<String, Object?> position, {
    required String? currency,
    required MarketHistoryTarget target,
  }) => showMarketHistorySheet(
    context,
    position: {
      ...position,
      'currency':
          position['currency'] ?? currency ?? read.currencyFor(position),
    },
    ledgerVersion: (read.metadata['ledgerVersion'] as num).toInt(),
    originScope: ref.read(accountScopeProvider).id,
    target: target,
  );
}

/// 同一标的的持仓先折叠为摘要，展开后仍逐条显示真实证券与历史成本。
class _HoldingGroupRow extends ConsumerWidget {
  const _HoldingGroupRow({
    required this.group,
    required this.currencyInHeader,
    required this.groupIndex,
    required this.ledgerVersion,
    required this.originScope,
    this.compact = false,
    this.onOpenHistory,
    this.onOpenUnderlyingHistory,
  });

  final PositionGroup group;
  final bool currencyInHeader;

  /// 仅为未知币种分组生成独立展示键，隔离每条记录的展开状态。
  final int groupIndex;
  final bool compact;
  final int ledgerVersion;
  final String originScope;

  /// 使用本行完整持仓身份打开历史价格；总览预览不提供二级行情入口。
  final ValueChanged<Map<String, Object?>>? onOpenHistory;

  /// 同标的只提供一个正股入口，只有期权持仓时也使用真实期权记录作为授权依据。
  final ValueChanged<Map<String, Object?>>? onOpenUnderlyingHistory;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // 优先用实有正股作为授权锚点；仅持期权时仍保留真实期权行的账户和证券键。
    final underlyingAnchor = group.rows.firstWhere(
      (record) => record['assetType'] == 'stock',
      orElse: () => group.rows.first,
    );
    final symbol = ref.watch(amountsHiddenProvider)
        ? ledgerSecurityText(group.rows.first, amountsHidden: true)
        : group.symbol;
    // 未知币种按原始行单独成组；同标的、同长度的多行需要稳定隔离展开状态。
    final unknownCurrencySuffix = group.currency == null
        ? '-unknown-$groupIndex'
        : '';
    return Theme(
      data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
      child: Container(
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: LyraColors.hairline)),
        ),
        child: ExpansionTile(
          key: PageStorageKey(
            'holding-group-${group.symbol}-${group.currency}-${group.rows.length}'
            '$unknownCurrencySuffix',
          ),
          tilePadding: EdgeInsets.zero,
          minTileHeight: 72,
          iconColor: LyraColors.accent,
          collapsedIconColor: LyraColors.inkSecondary,
          childrenPadding: const EdgeInsets.only(left: 10),
          title: Row(
            children: [
              StockAvatar(symbol: symbol, size: 42),
              const SizedBox(width: 12),
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
                      '${group.rows.length} 项持仓',
                      style: const TextStyle(
                        fontSize: 12,
                        color: LyraColors.inkSecondary,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          children: [
            if (onOpenUnderlyingHistory != null)
              MarketUnderlyingQuotePreview(
                position: {
                  ...underlyingAnchor,
                  'currency': underlyingAnchor['currency'] ?? group.currency,
                },
                ledgerVersion: ledgerVersion,
                originScope: originScope,
                onOpenHistory: () => onOpenUnderlyingHistory!(underlyingAnchor),
              ),
            for (final record in group.rows)
              _HoldingRecordRow(
                record: record,
                currency: currencyInHeader ? null : group.currency,
                nested: true,
                compact: compact,
                onOpenHistory:
                    onOpenHistory == null || record['assetType'] != 'option'
                    ? null
                    : () => onOpenHistory!(record),
              ),
          ],
        ),
      ),
    );
  }
}

/// 将结构化期权字段组织成完整合约说明；金额隐藏时不暴露行权价。
///
/// [record] 是服务端持仓或交易行；[amountsHidden] 控制行权价的显示，
/// 返回保留到期日与看涨/看跌身份的文字，不从合约代码推断金融字段。
String _optionContractLabel(
  Map<String, Object?> record, {
  required bool amountsHidden,
}) =>
    '${ledgerText(record['expiry'])} · '
    '${amountsHidden ? '••••' : LyraNumber.quantity(ledgerText(record['strike']))} '
    '${ledgerText(record['optionType'])}';

/// 持仓行显示完整合约与服务端历史成本，避免把未乘合约乘数的基数当作金额。
class _HoldingRecordRow extends ConsumerWidget {
  const _HoldingRecordRow({
    required this.record,
    this.currency,
    this.nested = false,
    this.compact = false,
    this.onOpenHistory,
  });

  final Map<String, Object?> record;
  final String? currency;
  final bool nested;
  final bool compact;

  /// 独立图标直接打开本条证券的走势，行本身仍展开原始账本明细。
  final VoidCallback? onOpenHistory;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden = ref.watch(amountsHiddenProvider);
    final option = record['assetType'] == 'option';
    final short = record['direction'] == 'short';
    final ticker = ledgerSecurityText(record, amountsHidden: hidden);
    final quantity =
        '${LyraNumber.quantity(ledgerText(record['quantity']))} '
        '${option ? '张' : '股'}';
    final contract = option
        ? nested
              ? ledgerText(record['expiry'])
              : _optionContractLabel(record, amountsHidden: hidden)
        : null;
    final quantityDetails = <String>[
      if (option || short) short ? '空头' : '多头',
      quantity,
      ?currency,
    ].join(' · ');
    final underlying = record['underlying'] as String?;
    final avatarSymbol =
        underlying ?? (hidden ? ticker : record['symbolKey'] as String?);
    return Theme(
      data: Theme.of(context).copyWith(
        dividerColor: Colors.transparent,
        listTileTheme: Theme.of(context).listTileTheme
            .copyWith(minVerticalPadding: 0),
      ),
      child: Container(
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: LyraColors.hairline)),
        ),
        child: ExpansionTile(
          key: PageStorageKey(
            'holding-row-${record['accountId']}-${record['symbolKey']}-'
            '${record['expiry']}-${record['strike']}-${record['direction']}',
          ),
          tilePadding: EdgeInsets.zero,
          minTileHeight: compact ? 52 : 64,
          showTrailingIcon: onOpenHistory != null,
          trailing: onOpenHistory == null
              ? null
              : IconButton(
                  tooltip: '合约走势',
                  onPressed: onOpenHistory,
                  icon: const Icon(LyraIcons.analytics, size: 21),
                ),
          dense: true,
          iconColor: LyraColors.accent,
          collapsedIconColor: LyraColors.inkSecondary,
          childrenPadding: const EdgeInsets.fromLTRB(0, 6, 4, 14),
          title: LayoutBuilder(
            builder: (context, constraints) {
              final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
              final contentWidth = constraints.maxWidth - (nested ? 0 : 54);
              final stacked = contentWidth / textScale < 214;
              final title = Text(
                nested && option
                    ? '${hidden ? '••••' : LyraNumber.quantity(ledgerText(record['strike']))} ${ledgerText(record['optionType'])}'
                    : ticker,
                style: const TextStyle(
                  fontSize: 16,
                  height: 1.3,
                  fontWeight: FontWeight.w500,
                ),
              );
              final cost = FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerRight,
                child: LyraAmount(ledgerText(record['costBasis']), size: 16),
              );
              final description = Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (stacked)
                    title
                  else
                    Row(
                      children: [
                        Expanded(flex: 5, child: title),
                        const SizedBox(width: 12),
                        Expanded(
                          flex: 4,
                          child: Align(
                            alignment: Alignment.centerRight,
                            child: cost,
                          ),
                        ),
                      ],
                    ),
                  const SizedBox(height: 4),
                  // 合约身份使用整行宽度自然换行，不能被右侧金额挤掉行权价。
                  if (contract != null)
                    Text(
                      contract,
                      style: const TextStyle(
                        fontSize: 12,
                        height: 1.5,
                        color: LyraColors.inkSecondary,
                      ),
                    ),
                  Text(
                    quantityDetails,
                    style: const TextStyle(
                      fontSize: 12,
                      height: 1.5,
                      color: LyraColors.inkSecondary,
                    ),
                  ),
                  if (stacked) ...[
                    const SizedBox(height: 8),
                    Align(alignment: Alignment.centerRight, child: cost),
                  ],
                ],
              );
              return Row(
                crossAxisAlignment: stacked
                    ? CrossAxisAlignment.start
                    : CrossAxisAlignment.center,
                children: [
                  if (!nested) ...[
                    StockAvatar(symbol: avatarSymbol, size: 42),
                    const SizedBox(width: 12),
                  ],
                  Expanded(child: description),
                ],
              );
            },
          ),
          children: [_LedgerRecordDetails(record: record)],
        ),
      ),
    );
  }
}

/// 按成交/平仓日期和币种合并外观分组；行内金额保持服务端原始正负值。
class _TransactionSection extends StatelessWidget {
  const _TransactionSection({
    required this.view,
    required this.read,
    required this.section,
  });

  final LedgerView view;
  final LedgerRead read;
  final _MergedLedgerSection section;

  @override
  Widget build(BuildContext context) {
    final rows = (section.data['rows'] as List<Object?>)
        .cast<Map<String, Object?>>();
    final buckets = <(String, String?), List<Map<String, Object?>>>{};
    for (final row in rows) {
      final date = switch (view) {
        LedgerView.realized => ledgerText(row['closeDate']),
        _ => ledgerText(row['transactionDate'] ?? row['recordDate']),
      };
      final currency = section.currency ?? read.currencyFor(row);
      buckets.putIfAbsent((date, currency), () => []).add(row);
    }
    return Column(
      children: [
        for (final bucket in buckets.entries)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: LyraCard(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          _transactionDateLabel(bucket.key.$1),
                          style: const TextStyle(
                            fontSize: 15,
                            fontWeight: FontWeight.w500,
                            color: LyraColors.inkSecondary,
                          ),
                        ),
                      ),
                      Text(
                        bucket.key.$2 ?? '按账户币种',
                        style: const TextStyle(
                          fontSize: 12,
                          color: LyraColors.inkSecondary,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  for (final (index, row) in bucket.value.indexed)
                    _TransactionRow(
                      view: view,
                      row: row,
                      currency: bucket.key.$2,
                      index: index,
                    ),
                ],
              ),
            ),
          ),
      ],
    );
  }
}

String _transactionDateLabel(String value) {
  final date = DateTime.tryParse(value);
  if (date == null) return value;
  return '${date.year}/${date.month.toString().padLeft(2, '0')}/'
      '${date.day.toString().padLeft(2, '0')}';
}

/// 交易行直接区分完整期权合约，动作与数量作次级信息，展开保留原始字段。
class _TransactionRow extends ConsumerWidget {
  const _TransactionRow({
    required this.view,
    required this.row,
    required this.currency,
    required this.index,
  });

  final LedgerView view;
  final Map<String, Object?> row;
  final String? currency;
  final int index;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden = ref.watch(amountsHiddenProvider);
    final title = view == LedgerView.cashflow
        ? row['description'] == null
              ? ledgerText(row['action'])
              : ledgerFieldText('description', row, amountsHidden: hidden)
        : ledgerSecurityText(row, amountsHidden: hidden);
    final quantity = row['quantity'] == null
        ? null
        : '${LyraNumber.quantity(ledgerText(row['quantity']))} '
              '${row['assetType'] == 'option' ? '张' : '股'}';
    final subtitle = switch (view) {
      LedgerView.trades => [ledgerText(row['action']), ?quantity].join(' · '),
      LedgerView.realized => [
        '${ledgerText(row['openDate'])} → ${ledgerText(row['closeDate'])}',
        ?quantity,
      ].join(' · '),
      LedgerView.cashflow => ledgerText(row['action']),
      _ => '',
    };
    final signed = ledgerSignedAmountFields.contains(view.valueField);
    final contract = row['assetType'] == 'option'
        ? _optionContractLabel(row, amountsHidden: hidden)
        : null;
    return Theme(
      data: Theme.of(context).copyWith(
        dividerColor: Colors.transparent,
        listTileTheme: Theme.of(context).listTileTheme
            .copyWith(minVerticalPadding: 0),
      ),
      child: Container(
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: LyraColors.hairline)),
        ),
        child: ExpansionTile(
          key: PageStorageKey(
            'transaction-row-${view.command}-$currency-$index-${row.toString()}',
          ),
          tilePadding: EdgeInsets.zero,
          minTileHeight: 60,
          showTrailingIcon: false,
          dense: true,
          iconColor: LyraColors.accent,
          collapsedIconColor: LyraColors.inkSecondary,
          childrenPadding: const EdgeInsets.fromLTRB(4, 6, 4, 14),
          title: LayoutBuilder(
            builder: (context, constraints) {
              final hasAvatar = view == LedgerView.trades;
              final contentWidth = constraints.maxWidth - (hasAvatar ? 54 : 0);
              final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
              final narrow = contentWidth / textScale < 214;
              final heading = Text(
                title,
                maxLines: view == LedgerView.cashflow ? 2 : 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w500,
                ),
              );
              final amount = FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerRight,
                child: LyraAmount(
                  ledgerText(row[view.valueField]),
                  size: 17,
                  colorBySign: signed,
                ),
              );
              final description = Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (narrow)
                    heading
                  else
                    Row(
                      children: [
                        Expanded(flex: 5, child: heading),
                        const SizedBox(width: 12),
                        Expanded(
                          flex: 4,
                          child: Align(
                            alignment: Alignment.centerRight,
                            child: amount,
                          ),
                        ),
                      ],
                    ),
                  if (contract != null) ...[
                    const SizedBox(height: 4),
                    Text(
                      contract,
                      style: const TextStyle(
                        fontSize: 12,
                        height: 1.5,
                        color: LyraColors.inkSecondary,
                      ),
                    ),
                  ],
                  if (subtitle.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(
                      subtitle,
                      style: const TextStyle(
                        fontSize: 12,
                        height: 1.5,
                        color: LyraColors.inkSecondary,
                      ),
                    ),
                  ],
                  if (narrow) ...[
                    const SizedBox(height: 8),
                    Align(alignment: Alignment.centerRight, child: amount),
                  ],
                ],
              );
              return Row(
                crossAxisAlignment: narrow
                    ? CrossAxisAlignment.start
                    : CrossAxisAlignment.center,
                children: [
                  if (hasAvatar) ...[
                    StockAvatar(
                      symbol: hidden
                          ? '••'
                          : (row['underlying'] ?? row['symbol']) as String?,
                    ),
                    const SizedBox(width: 12),
                  ],
                  Expanded(child: description),
                ],
              );
            },
          ),
          children: [_LedgerRecordDetails(record: row)],
        ),
      ),
    );
  }
}

/// 展开查看协议字段；所有金额仍以服务端十进制字符串和隐私组件呈现。
class _LedgerRecordDetails extends ConsumerWidget {
  const _LedgerRecordDetails({required this.record});

  final Map<String, Object?> record;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden = ref.watch(amountsHiddenProvider);
    final percent = record['allocationPct'] as String?;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
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
        // cost 是领域计算基数；已有完整成本金额时不再向用户重复呈现它。
        for (final field in record.keys.where(
          (field) =>
              ledgerLabels.containsKey(field) &&
              !(field == 'cost' && record.containsKey('costBasis')),
        ))
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
    );
  }
}

/// 一个币种一张报告组卡；图形仅投影服务端金额，不回写金融数据。
class _ReportSection extends StatelessWidget {
  const _ReportSection({
    required this.view,
    required this.read,
    required this.data,
    this.currency,
  });
  final LedgerView view;
  final LedgerRead read;
  final Map<String, Object?> data;
  final String? currency;
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
    final countLabel = '${rows.length} 条';
    const totals = [
      'totalPnl',
      'total',
      'totalCostBasis',
      'maxDrawdown',
      'totalLongPremiumPaid',
      'totalShortPremiumReceived',
      'allLongOptionsLossIfZero',
    ];
    final content = Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Wrap(
          alignment: WrapAlignment.spaceBetween,
          crossAxisAlignment: WrapCrossAlignment.center,
          spacing: 12,
          runSpacing: 6,
          children: [
            Text(
              currency == null
                  ? '记录明细'
                  : view == LedgerView.expiring
                  ? '历史权利金 · $currency'
                  : '$currency / ${view.title}',
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
                  style: const TextStyle(fontSize: 12, color: LyraColors.muted),
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
        for (final (index, raw) in rows.indexed)
          _ReportRow(
            view: view,
            row: raw,
            index: index,
            currency: currency ?? read.currencyFor(raw),
            currencyInHeader: currency != null,
          ),
      ],
    );
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: rows.isEmpty
          ? content
          : LyraCard(padding: const EdgeInsets.all(16), child: content),
    );
  }
}

/// 报告行与展开明细共用同一权威记录，不构造虚假收益曲线。
class _ReportRow extends ConsumerWidget {
  const _ReportRow({
    required this.view,
    required this.row,
    required this.index,
    required this.currencyInHeader,
    this.currency,
  });
  final LedgerView view;
  final Map<String, Object?> row;
  final int index;
  final String? currency;
  final bool currencyInHeader;

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
        '${ledgerText(record[view == LedgerView.expiring ? 'status' : 'assetType'])} · ${ledgerText(record['direction'])} · ${LyraNumber.quantity(ledgerText(record['quantity']))} ${record['assetType'] == 'option' ? '张' : '股'}${record['expiry'] != null ? '\n${ledgerText(record['expiry'])}' : ''}${record['strike'] != null ? ' · ${hidden ? '••••' : LyraNumber.quantity(ledgerText(record['strike']))}' : ''}${record['optionType'] != null ? ' ${ledgerText(record['optionType'])}' : ''}',
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
          title: LayoutBuilder(
            builder: (context, constraints) {
              final hasAvatar = [
                LedgerView.expiring,
                LedgerView.trades,
              ].contains(view);
              final textScale = MediaQuery.textScalerOf(context).scale(16) / 16;
              final contentWidth = constraints.maxWidth - (hasAvatar ? 54 : 0);
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
                  if (view == LedgerView.audit || !currencyInHeader) ...[
                    const SizedBox(height: 5),
                    Text(
                      view == LedgerView.audit ? '记录数' : (currency ?? '按账户币种'),
                      style: const TextStyle(
                        fontSize: 12,
                        color: LyraColors.muted,
                      ),
                    ),
                  ],
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
                          ? ledgerSecurityText(record, amountsHidden: true)
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
                            crossAxisAlignment: CrossAxisAlignment.stretch,
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
          children: [_LedgerRecordDetails(record: record)],
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
    final scale = MediaQuery.textScalerOf(context).scale(11) / 11;
    final left = widget.compact ? 40 * scale : 0.0;
    final available = math.max(1.0, width - left - (widget.compact ? 8 : 0));
    final position = widget.compact && widget.line
        ? ((x - left) / available * (widget.rows.length - 1)).round()
        : ((x / width) * widget.rows.length).floor();
    final index = position.clamp(0, widget.rows.length - 1);
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
          height: widget.compact ? 180 : 222,
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
          if (!widget.compact || _selected != null)
            Row(
              children: [
                Expanded(
                  child: Text(
                    '${ledgerText(rows[selected][dateField])} / ${line ? '累计损益' : '当月损益'}',
                    style: const TextStyle(
                      fontSize: 12,
                      color: LyraColors.muted,
                    ),
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
          if (!widget.compact || _selected != null) const SizedBox(height: 12),
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
                  height: widget.compact ? 164 : 180,
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
                        compact: widget.compact,
                        labels: [
                          for (final row in rows) ledgerText(row[dateField]),
                        ],
                        fontScale:
                            MediaQuery.textScalerOf(context).scale(11) / 11,
                        positiveColor: lyraProfitColor('1', scheme),
                        negativeColor: lyraProfitColor('-1', scheme),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
          if (!widget.compact)
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Flexible(
                  child: Text(
                    ledgerText(rows.first[dateField]),
                    style: const TextStyle(
                      fontSize: 11,
                      color: LyraColors.muted,
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Flexible(
                  child: Text(
                    ledgerText(rows.last[dateField]),
                    textAlign: TextAlign.end,
                    style: const TextStyle(
                      fontSize: 11,
                      color: LyraColors.muted,
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

class _LedgerPlotPainter extends CustomPainter {
  _LedgerPlotPainter(
    this.values,
    this.line,
    this.progress,
    this.selected, {
    required this.positiveColor,
    required this.negativeColor,
    this.compact = false,
    this.labels = const [],
    this.fontScale = 1,
  });
  final List<double> values;
  final bool line;
  final double progress;
  final int selected;
  final Color positiveColor;
  final Color negativeColor;
  final bool compact;
  final List<String> labels;
  final double fontScale;

  Color _colorFor(double value) => value == 0
      ? LyraColors.ink
      : value < 0
      ? negativeColor
      : positiveColor;

  /// 将绘图范围扩展到易读刻度，返回低点、高点与步长；不修改原始金融读数。
  ({double low, double high, double step}) _bounds() {
    final low = math.min(0.0, values.reduce(math.min));
    final high = math.max(0.0, values.reduce(math.max));
    if (!compact) return (low: low, high: high, step: (high - low) / 3);
    final rough = (high - low) / 4;
    if (rough == 0) return (low: 0, high: 1, step: .25);
    final magnitude = math
        .pow(10, (math.log(rough) / math.ln10).floor())
        .toDouble();
    final fraction = rough / magnitude;
    final unit = const [
      1.0,
      2.0,
      4.0,
      5.0,
      8.0,
      10.0,
    ].firstWhere((unit) => unit >= fraction);
    final step = unit * magnitude;
    return (
      low: (low / step).floor() * step,
      high: (high / step).ceil() * step,
      step: step,
    );
  }

  /// 画布刻度仅表达近似坐标；原始精确金额仍由可点选的 LyraAmount 展示。
  String _tickLabel(double value, double step) {
    if (value.abs() >= 1e9) return value.toStringAsExponential(1);
    final decimals = step < 1 ? -(math.log(step) / math.ln10).floor() : 0;
    return LyraNumber.amount(
      value.toStringAsFixed(decimals.clamp(0, 8)),
      decimals: decimals.clamp(0, 8),
    );
  }

  /// 将同年跨月序列显示为月份，同月日序列保留日期，避免把日线伪装成月线。
  String _dateLabel(String value) {
    DateTime? date(String raw) =>
        DateTime.tryParse(raw.length == 7 ? '$raw-01' : raw);
    final current = date(value);
    final first = labels.isEmpty ? null : date(labels.first);
    final last = labels.isEmpty ? null : date(labels.last);
    if (current == null || first == null || last == null) return value;
    if (first.year != last.year) {
      return '${current.year % 100}/${current.month}';
    }
    if (first.month != last.month || !line) return '${current.month}月';
    return '${current.month}/${current.day}';
  }

  /// 在 [anchor] 附近绘制刻度，并将文字约束在当前画布 [size] 内。
  void _label(
    Canvas canvas,
    Size size,
    String text,
    Offset anchor, {
    bool right = false,
  }) {
    final painter = TextPainter(
      text: TextSpan(
        text: text,
        style: LyraTheme.numeric.copyWith(
          fontSize: 10 * fontScale,
          height: 1,
          color: LyraColors.inkSecondary,
        ),
      ),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    )..layout(maxWidth: size.width);
    final x = right ? anchor.dx - painter.width : anchor.dx - painter.width / 2;
    painter.paint(
      canvas,
      Offset(
        x.clamp(0.0, math.max(0.0, size.width - painter.width)),
        anchor.dy,
      ),
    );
    painter.dispose();
  }

  /// 使用同一条虚线规则绘制网格，不参与金额、日期或交易计算。
  void _dashed(Canvas canvas, Offset from, Offset to, Paint paint) {
    final distance = (to - from).distance;
    if (distance == 0) return;
    for (double start = 0; start < distance; start += 7) {
      canvas.drawLine(
        Offset.lerp(from, to, start / distance)!,
        Offset.lerp(from, to, math.min(start + 4, distance) / distance)!,
        paint,
      );
    }
  }

  @override
  void paint(Canvas canvas, Size size) {
    final bounds = _bounds();
    final low = bounds.low;
    final high = bounds.high;
    final extent = high == low ? 1.0 : high - low;
    final plot = Rect.fromLTRB(
      compact ? 40 * fontScale : 0,
      12,
      size.width - (compact ? 8 : 0),
      size.height - (compact ? 24 * fontScale : 16),
    );
    double y(double value) => plot.top + (high - value) / extent * plot.height;
    final grid = Paint()
      ..color = LyraColors.hairline
      ..strokeWidth = 1;
    final intervals = compact ? ((high - low) / bounds.step).round() : 3;
    for (int i = 0; i <= intervals; i++) {
      final value = high - i * (high - low) / intervals;
      final top = compact ? y(value) : plot.top + i * plot.height / intervals;
      if (compact) {
        _dashed(canvas, Offset(plot.left, top), Offset(plot.right, top), grid);
        _label(
          canvas,
          size,
          _tickLabel(value, bounds.step),
          Offset(plot.left - 10, top - 5 * fontScale),
          right: true,
        );
      } else {
        canvas.drawLine(Offset(plot.left, top), Offset(plot.right, top), grid);
      }
    }
    canvas.drawLine(
      Offset(plot.left, y(0)),
      Offset(plot.right, y(0)),
      Paint()..color = LyraColors.hairlineStrong,
    );
    if (compact && labels.isNotEmpty) {
      final maxLabels = math.max(2, (plot.width / (40 * fontScale)).floor());
      final stride = math.max(1, (labels.length / maxLabels).ceil());
      for (int i = 0; i < labels.length; i++) {
        if (i % stride != 0 && i != labels.length - 1) continue;
        final x = line
            ? labels.length == 1
                  ? plot.center.dx
                  : plot.left + i * plot.width / (labels.length - 1)
            : plot.left + (i + .5) * plot.width / labels.length;
        _dashed(canvas, Offset(x, plot.top), Offset(x, plot.bottom), grid);
        _label(
          canvas,
          size,
          _dateLabel(labels[i]),
          Offset(x, plot.bottom + 10),
        );
      }
    }
    canvas.save();
    canvas.clipRect(Rect.fromLTWH(0, 0, size.width * progress, size.height));
    if (line) {
      // 单条累计记录只绘制居中的数据点，避免除以零或虚构一段走势。
      double xFor(int index) => values.length == 1
          ? plot.center.dx
          : plot.left + index * plot.width / (values.length - 1);
      if (compact && values.length > 1) {
        final area = Path()..moveTo(plot.left, y(0));
        for (int i = 0; i < values.length; i++) {
          area.lineTo(xFor(i), y(values[i]));
        }
        area
          ..lineTo(plot.right, y(0))
          ..close();
        // 正负区域分别着色，保持跨零走势与用户的涨跌配色一致。
        for (final (rect, color) in [
          (Rect.fromLTRB(plot.left, plot.top, plot.right, y(0)), positiveColor),
          (
            Rect.fromLTRB(plot.left, y(0), plot.right, plot.bottom),
            negativeColor,
          ),
        ]) {
          canvas.save();
          canvas.clipRect(rect);
          canvas.drawPath(
            area,
            Paint()
              ..shader = LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [
                  color.withValues(alpha: .15),
                  color.withValues(alpha: .015),
                ],
              ).createShader(plot),
          );
          canvas.restore();
        }
      }
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
      // 密集交易日期保留真实折线，只突出选中点，避免圆点覆盖走势细节。
      final point = Offset(xFor(selected), y(values[selected]));
      canvas.drawLine(
        Offset(point.dx, plot.top),
        Offset(point.dx, plot.bottom),
        Paint()..color = LyraColors.hairlineStrong,
      );
      canvas.drawCircle(
        point,
        5,
        Paint()..color = compact ? LyraColors.ink : LyraColors.surface,
      );
      canvas.drawCircle(point, 3, Paint()..color = _colorFor(values[selected]));
    } else {
      final step = plot.width / values.length;
      for (int i = 0; i < values.length; i++) {
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            Rect.fromLTRB(
              plot.left + i * step + step * .15,
              math.min(y(0), y(values[i])),
              plot.left + (i + 1) * step - step * .15,
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
      oldDelegate.negativeColor != negativeColor ||
      oldDelegate.compact != compact ||
      oldDelegate.fontScale != fontScale ||
      oldDelegate.labels != labels;
}
