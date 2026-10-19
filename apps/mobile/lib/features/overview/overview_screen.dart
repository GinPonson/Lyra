import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_date.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/design/lyra_widgets.dart';
import '../accounts/account_sheet.dart';
import '../accounts/account_workspace.dart';
import '../import/import_sheet.dart';
import '../ledger/ledger_models.dart';
import '../ledger/ledger_screen.dart';
import '../ledger/ledger_view.dart';
import '../ledger/ledger_workspace.dart';
import 'login_panel.dart';

/// 账号优先的投资工作台。保留登录、导入及原始精确汇总；AI 为次级入口。
class OverviewScreen extends ConsumerStatefulWidget {
  const OverviewScreen({
    super.key,
    this.onOpenLedger,
    this.onOpenStocks,
    this.onOpenAgent,
  });
  final VoidCallback? onOpenLedger;
  final VoidCallback? onOpenStocks;
  final VoidCallback? onOpenAgent;
  @override
  ConsumerState<OverviewScreen> createState() => _OverviewScreenState();
}

class _OverviewScreenState extends ConsumerState<OverviewScreen> {
  final _loginKey = GlobalKey();

  /// 保留原有内联登录流程，主按钮把登录方式滚入视口。
  void _revealLogin() {
    final target = _loginKey.currentContext;
    if (target != null) {
      Scrollable.ensureVisible(
        target,
        duration: const Duration(milliseconds: 450),
        curve: Curves.easeOutCubic,
      );
    }
  }

  void _openImport() => showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    backgroundColor: LyraColors.canvas,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      side: BorderSide(color: LyraColors.cardBorder, width: .75),
    ),
    builder: (context) => Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: const SingleChildScrollView(child: ImportSheet()),
    ),
  );

  Future<void> _createAccount() async {
    final client = ref.read(sessionProvider.notifier).client;
    final account = await showCreateAccountSheet(context);
    if (account != null &&
        mounted &&
        identical(client, ref.read(sessionProvider.notifier).client)) {
      _openImport();
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(sessionProvider);
    final paired = session.value is SessionAuthenticated;
    final summary = ref.watch(scopedSummaryProvider);
    final accounts = paired ? ref.watch(brokerageAccountsProvider) : null;
    return LyraPage(
      storageKey: 'overview-scroll',
      children: [
        if (paired) ...[
          LyraFinancialHeader(
            title: '总览',
            actions: [_ImportAction(onPressed: _openImport)],
          ),
          const SizedBox(height: 14),
        ] else ...[
          LyraPageTitle(
            title: '总览',
            trailing: LyraIconButton(
              icon: LyraIcons.accountCenter,
              label: '登录',
              onPressed: _revealLogin,
            ),
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const AccountScopeButton(),
              const Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(LyraIcons.security, size: 13, color: LyraColors.accent),
                  SizedBox(width: 5),
                  Text(
                    '尚未登录',
                    style: TextStyle(
                      fontSize: 11,
                      color: LyraColors.inkSecondary,
                    ),
                  ),
                ],
              ),
            ],
          ),
          const SizedBox(height: 18),
        ],
        if (session.hasError)
          LyraLoadError(
            error: session.error!,
            onRetry: () => ref.invalidate(sessionProvider),
          )
        else if (session.isLoading)
          const LyraSkeleton()
        else if (!paired)
          LyraReveal(child: _WelcomeHero(onLogin: _revealLogin))
        else if (accounts!.isLoading)
          const LyraSkeleton()
        else if (accounts.hasError)
          LyraLoadError(
            error: accounts.error!,
            onRetry: () => ref.invalidate(brokerageAccountsProvider),
          )
        else if (accounts.value!.isEmpty)
          LyraEmpty(
            title: '先创建一个证券账户',
            message: '你已登录 Lyra。添加 Schwab 账户后，即可导入交易并查看持仓。',
            icon: LyraIcons.cash,
            action: FilledButton.icon(
              onPressed: _createAccount,
              icon: const Icon(LyraIcons.addAccount, size: 18),
              label: const Text('创建第一个证券账户'),
            ),
          )
        else
          summary.when(
            loading: () => const LyraSkeleton(),
            error: (error, _) => LyraLoadError(
              error: error,
              onRetry: () => ref.invalidate(scopedSummaryProvider),
            ),
            data: (read) =>
                read == null ||
                    read.entries.isEmpty ||
                    read.entries.every((entry) => entry.transactionCount == 0)
                ? LyraEmpty(
                    title: '证券账户已准备好，等待第一笔记录',
                    message: '导入官方交易文件后，已实现损益、现金流与持仓会自动呈现。',
                    action: FilledButton.icon(
                      onPressed: _openImport,
                      icon: const Icon(LyraIcons.importFile, size: 18),
                      label: const Text('导入文件'),
                    ),
                  )
                : Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      for (final (index, entry) in read.entries.indexed)
                        Padding(
                          padding: EdgeInsets.only(top: index == 0 ? 0 : 20),
                          child: _AccountSummary(
                            entry: entry,
                            primary: index == 0,
                            onAnalyze: _openAnalysis,
                            onShowMethod: () =>
                                _showMethod(entry.currency, read.metadata.asOf),
                          ),
                        ),
                      const SizedBox(height: 16),
                      LedgerHoldingsPreview(onOpen: widget.onOpenStocks),
                      const SizedBox(height: 8),
                      Text(
                        '数据截至 ${lyraDateTime(read.metadata.asOf)}',
                        style: const TextStyle(
                          fontSize: 11,
                          height: 1.6,
                          color: LyraColors.muted,
                        ),
                      ),
                    ],
                  ),
          ),
        if (!paired && !session.isLoading) ...[
          const SizedBox(height: 26),
          LoginPanel(key: _loginKey),
        ],
        const SizedBox(height: 28),
        _AssistantNote(onOpen: widget.onOpenAgent),
        const SizedBox(height: 24),
      ],
    );
  }

  /// 打开上下文报告列表；选择报告后先关闭面板，再进入完整报告页。
  Future<void> _openAnalysis() async {
    final view = await showModalBottomSheet<LedgerView>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (context) =>
          _AnalysisSheet(onRefresh: () => ref.invalidate(sessionProvider)),
    );
    if (!mounted || view == null) return;
    await Navigator.push<void>(
      context,
      MaterialPageRoute<void>(builder: (_) => ReportScreen(view: view)),
    );
  }

  /// 展示汇总口径与数据时点；[currency] 是当前卡片币种，[asOf] 是服务端时间戳。
  void _showMethod(String currency, String asOf) {
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('数据口径'),
        content: Text(
          '已实现损益来自账本交易记录，并已扣除分摊费用。\n\n'
          '本卡按当前账户范围以 $currency 单独展示，不合并其他币种。'
          '收益走势复用已实现损益报告数据，不代表账户净值或实时资产。\n\n'
          '数据截至 ${lyraDateTime(asOf)}。',
          style: const TextStyle(height: 1.6),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('知道了'),
          ),
        ],
      ),
    );
  }
}

/// 总览标题栏的导入动作，点击后打开既有文件确认流程。
class _ImportAction extends StatelessWidget {
  const _ImportAction({required this.onPressed});

  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => IconButton(
    tooltip: '导入文件',
    onPressed: onPressed,
    icon: const LyraPrimaryIcon(
      LyraPrimaryGlyph.importFile,
      size: 22,
      color: LyraColors.accent,
    ),
    style: IconButton.styleFrom(
      fixedSize: const Size(44, 44),
      minimumSize: const Size(44, 44),
      foregroundColor: LyraColors.accent,
    ),
  );
}

/// 在按需打开的分析面板中保留报告目录与账号刷新操作。
class _AnalysisSheet extends StatelessWidget {
  const _AnalysisSheet({required this.onRefresh});

  final VoidCallback onRefresh;

  static const _views = [
    LedgerView.monthly,
    LedgerView.attribution,
    LedgerView.drawdown,
    LedgerView.holdingPeriod,
    LedgerView.extremes,
    LedgerView.streaks,
    LedgerView.audit,
  ];

  @override
  Widget build(BuildContext context) => SafeArea(
    child: ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(context).height * .75,
      ),
      child: ListView(
        padding: const EdgeInsets.fromLTRB(20, 4, 20, 24),
        children: [
          const Padding(
            padding: EdgeInsets.fromLTRB(4, 8, 4, 4),
            child: Text(
              '收益与报告',
              style: TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
            ),
          ),
          ListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 4),
            leading: const Icon(LyraIcons.sync, color: LyraColors.accent),
            title: const Text('刷新账号'),
            onTap: () {
              Navigator.pop(context);
              onRefresh();
            },
          ),
          const Divider(height: 8),
          for (final view in _views)
            ListTile(
              contentPadding: const EdgeInsets.symmetric(horizontal: 4),
              leading: Icon(view.icon, color: LyraColors.accent),
              title: Text(view.title),
              subtitle: Text(
                view.basis,
                style: const TextStyle(color: LyraColors.inkSecondary),
              ),
              trailing: const Icon(LyraIcons.disclosure, size: 19),
              onTap: () => Navigator.pop(context, view),
            ),
        ],
      ),
    ),
  );
}

/// 未登录时直接说明可用功能，主按钮继续使用原有登录方式。
class _WelcomeHero extends StatelessWidget {
  const _WelcomeHero({required this.onLogin});
  final VoidCallback onLogin;

  @override
  Widget build(BuildContext context) => LyraCard(
    padding: const EdgeInsets.all(24),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Icon(LyraIcons.accountCenter, size: 30, color: LyraColors.accent),
        const SizedBox(height: 20),
        const Text(
          '登录 Lyra',
          style: TextStyle(fontSize: 24, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 10),
        const Text(
          '导入 Schwab 账单，查看持仓、现金余额和已实现收益。',
          style: TextStyle(
            fontSize: 14,
            height: 1.6,
            color: LyraColors.inkSecondary,
          ),
        ),
        const SizedBox(height: 22),
        SizedBox(
          width: double.infinity,
          child: FilledButton.icon(
            onPressed: onLogin,
            icon: const Icon(LyraIcons.accountCenter, size: 18),
            label: const Text('登录'),
          ),
        ),
      ],
    ),
  );
}

/// 每个币种独立呈现已实现损益、同币种收益走势、净入金与现金余额。
/// 金融金额始终使用服务端精确字符串，并由 [LyraAmount] 处理格式和隐私。
class _AccountSummary extends StatelessWidget {
  const _AccountSummary({
    required this.entry,
    required this.primary,
    required this.onAnalyze,
    required this.onShowMethod,
  });

  final LedgerSummaryEntry entry;
  final bool primary;
  final VoidCallback onAnalyze;
  final VoidCallback onShowMethod;

  @override
  Widget build(BuildContext context) => LyraReveal(
    child: LyraCard(
      key: primary ? const ValueKey('overview-summary-card') : null,
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          LayoutBuilder(
            builder: (context, constraints) {
              final scaled = MediaQuery.textScalerOf(context).scale(14) > 17;
              final compact = constraints.maxWidth < 270 || scaled;
              final label = Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Flexible(
                    child: Text(
                      '已实现损益',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: 18,
                        height: 1.35,
                        fontWeight: FontWeight.w500,
                        color: LyraColors.ink,
                      ),
                    ),
                  ),
                  IconButton(
                    tooltip: '查看损益口径',
                    onPressed: onShowMethod,
                    visualDensity: VisualDensity.compact,
                    style: IconButton.styleFrom(
                      minimumSize: const Size(32, 32),
                      padding: const EdgeInsets.all(6),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                    icon: const LyraPrimaryIcon(
                      LyraPrimaryGlyph.about,
                      size: 18,
                    ),
                  ),
                ],
              );
              final currency = Text(
                entry.currency,
                style: const TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                  color: LyraColors.inkSecondary,
                ),
              );
              final analysis = TextButton(
                onPressed: onAnalyze,
                style: TextButton.styleFrom(
                  foregroundColor: LyraColors.accent,
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  minimumSize: const Size(44, 32),
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
                child: const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('分析'),
                    SizedBox(width: 3),
                    Icon(LyraIcons.disclosure, size: 18),
                  ],
                ),
              );

              if (compact) {
                return Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Row(
                      children: [
                        Expanded(child: label),
                        const SizedBox(width: 6),
                        currency,
                      ],
                    ),
                    Align(alignment: Alignment.centerRight, child: analysis),
                  ],
                );
              }
              return Row(
                children: [
                  Expanded(child: label),
                  const SizedBox(width: 6),
                  currency,
                  analysis,
                ],
              );
            },
          ),
          const SizedBox(height: 2),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: LyraAmount(
              entry.realizedPnlTotal,
              key: primary
                  ? const ValueKey<String>('overview-hero-value')
                  : null,
              size: 52,
              weight: FontWeight.w600,
              colorBySign: true,
              showPositiveSign: true,
            ),
          ),
          LedgerTrendPreview(currency: entry.currency, embedded: true),
          const SizedBox(height: 8),
          _SummaryBalances(entry: entry),
        ],
      ),
    ),
  );
}

/// 在总览中以内嵌面板并排呈现净入金与现金余额。
/// 金额仍由 [LyraAmount] 管理隐藏状态、精度与屏幕阅读器语义。
class _SummaryBalances extends StatelessWidget {
  const _SummaryBalances({required this.entry});

  final LedgerSummaryEntry entry;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      // 大字和窄屏改为纵向排列，避免缩小金额来挤进双列。
      final vertical =
          constraints.maxWidth < 300 ||
          MediaQuery.textScalerOf(context).scale(12) > 16;
      final metrics = [
        _BalanceMetric(label: '净入金', value: entry.netDeposits),
        _BalanceMetric(label: '现金余额', value: entry.cashBalance),
      ];
      return LyraCard(
        inset: true,
        tint: LyraColors.positive,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        child: vertical
            ? Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [metrics[0], const Divider(height: 18), metrics[1]],
              )
            : Row(
                children: [
                  Expanded(child: metrics[0]),
                  const SizedBox(
                    height: 48,
                    child: VerticalDivider(
                      width: 22,
                      color: LyraColors.hairlineStrong,
                      thickness: .75,
                    ),
                  ),
                  Expanded(child: metrics[1]),
                ],
              ),
      );
    },
  );
}

/// 显示带币种含义的精确金额；隐藏状态由共享隐私控制器统一处理。
class _BalanceMetric extends StatelessWidget {
  const _BalanceMetric({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(
        label,
        style: const TextStyle(fontSize: 12, color: LyraColors.inkSecondary),
      ),
      const SizedBox(height: 5),
      FittedBox(
        fit: BoxFit.scaleDown,
        alignment: Alignment.centerLeft,
        child: LyraAmount(value, size: 21, currency: null),
      ),
    ],
  );
}

/// AI 以次级工具入口出现，不与账号和股票争夺一级导航。
class _AssistantNote extends StatelessWidget {
  const _AssistantNote({this.onOpen});
  final VoidCallback? onOpen;
  @override
  Widget build(BuildContext context) => Material(
    color: Colors.transparent,
    child: InkWell(
      onTap: onOpen,
      borderRadius: BorderRadius.circular(16),
      child: LyraCard(
        inset: true,
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 18),
        child: Row(
          children: [
            const Icon(LyraIcons.agent, size: 24, color: LyraColors.accent),
            const SizedBox(width: 14),
            const Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'AI 账本分析',
                    style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                  ),
                  SizedBox(height: 5),
                  Text(
                    '解读持仓与交易记录',
                    style: TextStyle(fontSize: 12, color: LyraColors.muted),
                  ),
                ],
              ),
            ),
            const Icon(
              LyraIcons.arrowUpRight,
              size: 17,
              color: LyraColors.muted,
            ),
          ],
        ),
      ),
    ),
  );
}
