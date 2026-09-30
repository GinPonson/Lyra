import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/design/lyra_widgets.dart';
import '../import/import_sheet.dart';
import '../ledger/ledger_models.dart';
import '../ledger/ledger_screen.dart';
import '../ledger/ledger_view.dart';
import '../ledger/ledger_workspace.dart';
import 'pairing_panel.dart';

/// 账号优先的投资工作台。保留配对、导入及原始精确汇总；AI 为次级入口。
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
  final _pairingKey = GlobalKey();

  /// 保留原有内联配对流程，主按钮把表单滚入视口并保留已填写字段。
  void _revealPairing() {
    final target = _pairingKey.currentContext;
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
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
    ),
    builder: (context) => Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: const ImportSheet(),
    ),
  );

  /// 连接管理复用已验证表单，不创建另一套凭据或配对实现。
  void _openConnection() => showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    useSafeArea: true,
    builder: (context) => SingleChildScrollView(
      padding: EdgeInsets.fromLTRB(
        16,
        0,
        16,
        MediaQuery.viewInsetsOf(context).bottom + 24,
      ),
      child: const PairingPanel(),
    ),
  );

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(sessionProvider);
    final paired = session.value is SessionPaired;
    final summary = ref.watch(scopedSummaryProvider);
    return LyraPage(
      storageKey: 'overview-scroll',
      children: [
        if (paired) ...[
          Row(
            children: [
              const Expanded(
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: AccountScopeButton(prominent: true),
                ),
              ),
              IconButton(
                tooltip: '导入文件',
                onPressed: _openImport,
                icon: const LyraPrimaryIcon(
                  LyraPrimaryGlyph.importFile,
                  size: 22,
                ),
              ),
              IconButton(
                tooltip: '连接管理',
                onPressed: _openConnection,
                icon: const LyraPrimaryIcon(
                  LyraPrimaryGlyph.connection,
                  size: 22,
                ),
              ),
            ],
          ),
          const SizedBox(height: 18),
        ] else ...[
          LyraPageTitle(
            title: '我的账号',
            trailing: LyraIconButton(
              icon: LyraIcons.accountCenter,
              label: '连接管理',
              onPressed: paired ? _openConnection : _revealPairing,
            ),
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const AccountScopeButton(),
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    paired ? LyraIcons.confirm : LyraIcons.security,
                    size: 13,
                    color: LyraColors.accent,
                  ),
                  const SizedBox(width: 5),
                  Text(
                    paired ? '已配对' : '等待连接',
                    style: const TextStyle(
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
          LyraReveal(child: _WelcomeHero(onConnect: _revealPairing))
        else
          summary.when(
            loading: () => const LyraSkeleton(),
            error: (error, _) => LyraLoadError(
              error: error,
              onRetry: () => ref.invalidate(scopedSummaryProvider),
            ),
            data: (read) => read == null || read.entries.isEmpty
                ? LyraEmpty(
                    title: '账号已连接，等待第一笔记录',
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
                          ),
                        ),
                      const SizedBox(height: 16),
                      // 账号先呈现持仓；宽屏仍以持仓为左侧主区，走势为辅助。
                      LayoutBuilder(
                        builder: (context, constraints) {
                          final holdings = LedgerHoldingsPreview(
                            onOpen: widget.onOpenStocks,
                          );
                          if (constraints.maxWidth >= 760) {
                            return Row(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Expanded(flex: 6, child: holdings),
                                const SizedBox(width: 40),
                                const Expanded(
                                  flex: 4,
                                  child: LedgerTrendPreview(),
                                ),
                              ],
                            );
                          }
                          return Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              holdings,
                              const SizedBox(height: 24),
                              const LedgerTrendPreview(),
                            ],
                          );
                        },
                      ),
                    ],
                  ),
          ),
        if (!paired && !session.isLoading) ...[
          const SizedBox(height: 26),
          const _MetricStrip(),
        ],
        const SizedBox(height: 28),
        _AssistantNote(onOpen: widget.onOpenAgent),
        const SizedBox(height: 32),
        if (!paired && !session.isLoading) PairingPanel(key: _pairingKey),
        if (paired) ...[
          Row(
            children: [
              const Expanded(
                child: Text(
                  '收益与报告',
                  style: TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                    letterSpacing: -.5,
                  ),
                ),
              ),
              IconButton(
                tooltip: '刷新账号',
                onPressed: () => ref.invalidate(sessionProvider),
                icon: const Icon(LyraIcons.sync, size: 19),
              ),
            ],
          ),
          const SizedBox(height: 10),
          const ReportLink(view: LedgerView.monthly),
          const ReportLink(view: LedgerView.attribution),
          const ReportLink(view: LedgerView.drawdown),
          ExpansionTile(
            key: const PageStorageKey<String>('account-research-expanded'),
            tilePadding: EdgeInsets.zero,
            title: const Text('更多分析', style: TextStyle(fontSize: 14)),
            children: const [
              ReportLink(view: LedgerView.holdingPeriod),
              ReportLink(view: LedgerView.extremes),
              ReportLink(view: LedgerView.streaks),
            ],
          ),
          const ReportLink(view: LedgerView.audit),
        ],
        const SizedBox(height: 24),
        if (summary.value != null)
          Text(
            '账本 v${summary.value!.metadata.ledgerVersion} · 截止 ${summary.value!.metadata.asOf}',
            style: const TextStyle(
              fontSize: 10,
              height: 1.8,
              color: LyraColors.muted,
            ),
          ),
        const SizedBox(height: 16),
        const Row(
          children: [
            Icon(LyraIcons.security, size: 12, color: LyraColors.muted),
            SizedBox(width: 6),
            Expanded(
              child: Text(
                '基于已导入交易记录 · 不含实时行情',
                style: TextStyle(fontSize: 10, color: LyraColors.muted),
              ),
            ),
          ],
        ),
      ],
    );
  }
}

/// 未连接时直接说明可用功能，主按钮继续使用原有配对表单。
class _WelcomeHero extends StatelessWidget {
  const _WelcomeHero({required this.onConnect});
  final VoidCallback onConnect;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(24),
    decoration: BoxDecoration(
      color: LyraColors.surfaceSubtle,
      borderRadius: BorderRadius.circular(20),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Icon(LyraIcons.accountCenter, size: 30, color: LyraColors.accent),
        const SizedBox(height: 20),
        const Text(
          '连接你的投资账号',
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
            onPressed: onConnect,
            icon: const Icon(LyraIcons.accountCenter, size: 18),
            label: const Text('连接我的账户'),
          ),
        ),
      ],
    ),
  );
}

/// 每个币种独立的账户汇总卡：主金额与六个辅助指标集中呈现。
/// 只显示已导入账本数据，不将历史损益包装成实时资产或购买力。
class _AccountSummary extends StatelessWidget {
  const _AccountSummary({required this.entry, required this.primary});
  final LedgerSummaryEntry entry;
  final bool primary;

  @override
  Widget build(BuildContext context) => LyraReveal(
    child: Container(
      key: primary ? const ValueKey('overview-summary-card') : null,
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(18),
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFFE9F7F0), Color(0xFFF4F8F6), Color(0xFFFAF9F5)],
          stops: [0, .58, 1],
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  '已实现损益',
                  style: TextStyle(
                    fontSize: 13,
                    color: LyraColors.inkSecondary,
                  ),
                ),
              ),
              Text(
                entry.currency,
                style: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: LyraColors.inkSecondary,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: LyraAmount(
              entry.realizedPnlTotal,
              key: primary
                  ? const ValueKey<String>('overview-hero-value')
                  : null,
              size: 34,
              color: entry.realizedPnlTotal.startsWith('-')
                  ? LyraColors.negative
                  : LyraColors.ink,
            ),
          ),
          const SizedBox(height: 5),
          const Text(
            '全部记录 · 已扣除分摊费用',
            style: TextStyle(fontSize: 10, color: LyraColors.muted),
          ),
          const SizedBox(height: 20),
          _MetricStrip(entry: entry),
        ],
      ),
    ),
  );
}

/// 汇总指标通常为三列两行；小屏或放大字体改为两列，避免压缩阅读空间。
/// 未连接时保留原有三个缺失金额占位，不生成模拟数据。
class _MetricStrip extends StatelessWidget {
  const _MetricStrip({this.entry});
  final LedgerSummaryEntry? entry;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final metrics = <(String, String, int)>[
        ('净入金', entry?.netDeposits ?? '—', 2),
        ('现金余额', entry?.cashBalance ?? '—', 2),
        ('股息收入', entry?.dividends ?? '—', 2),
        if (entry != null) ...[
          ('累计费用', entry!.totalFees, 2),
          ('持仓条目', '${entry!.openPositions}', 0),
          ('交易笔数', '${entry!.transactionCount}', 0),
        ],
      ];
      final columns =
          constraints.maxWidth < 290 ||
              MediaQuery.textScalerOf(context).scale(12) > 16
          ? 2
          : 3;
      return Column(
        children: [
          for (int start = 0; start < metrics.length; start += columns)
            Padding(
              padding: EdgeInsets.only(top: start == 0 ? 0 : 18),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  for (int offset = 0; offset < columns; offset++)
                    Expanded(
                      child: start + offset >= metrics.length
                          ? const SizedBox.shrink()
                          : Padding(
                              padding: EdgeInsets.only(
                                right: offset == columns - 1 ? 0 : 10,
                              ),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    metrics[start + offset].$1,
                                    style: const TextStyle(
                                      fontSize: 11,
                                      color: LyraColors.inkSecondary,
                                    ),
                                  ),
                                  const SizedBox(height: 5),
                                  FittedBox(
                                    fit: BoxFit.scaleDown,
                                    child: LyraAmount(
                                      metrics[start + offset].$2,
                                      size: 18,
                                      decimals: metrics[start + offset].$3,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                    ),
                ],
              ),
            ),
        ],
      );
    },
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
      borderRadius: BorderRadius.circular(14),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 18),
        decoration: BoxDecoration(
          border: Border.all(color: LyraColors.hairlineStrong),
          borderRadius: BorderRadius.circular(14),
        ),
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
                    style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                  ),
                  SizedBox(height: 5),
                  Text(
                    '解读持仓与交易记录',
                    style: TextStyle(fontSize: 10, color: LyraColors.muted),
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
