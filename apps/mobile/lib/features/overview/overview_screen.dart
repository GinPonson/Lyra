import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_theme.dart';
import '../../core/design/lyra_icons.dart';
import '../import/import_sheet.dart';
import 'pairing_panel.dart';

/// 总览页：未配对时展示配对表单，已配对后展示真实账本汇总。
///
/// 金额保持服务端十进制字符串原样展示；没有数据时显示缺省符号而不伪造。
class OverviewScreen extends ConsumerWidget {
  const OverviewScreen({super.key});

  static const double _contentMaxWidth = 920;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider).value ?? const SessionLoading();
    final summary = switch (session) {
      SessionPaired(:final summary) => summary,
      _ => null,
    };
    final bool paired = session is SessionPaired;
    return ColoredBox(
      color: LyraColors.canvas,
      child: SafeArea(
        bottom: false,
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: _contentMaxWidth),
            child: CustomScrollView(
              key: const PageStorageKey<String>('overview-scroll'),
              slivers: <Widget>[
                SliverPadding(
                  padding: const EdgeInsets.fromLTRB(20, 18, 20, 36),
                  sliver: SliverList.list(
                    children: <Widget>[
                      const _OverviewHeader(),
                      const SizedBox(height: 24),
                      const _AccountScopePill(),
                      const SizedBox(height: 18),
                      _LedgerHero(
                        connected: paired,
                        realizedPnl: summary == null || summary.entries.isEmpty
                            ? null
                            : summary.entries.first.realizedPnlTotal,
                        currency: summary == null || summary.entries.isEmpty
                            ? null
                            : summary.entries.first.currency,
                        ledgerVersion: summary?.metadata.ledgerVersion,
                      ),
                      const SizedBox(height: 12),
                      _MetricGrid(
                        netDeposits: summary == null || summary.entries.isEmpty
                            ? null
                            : summary.entries.first.netDeposits,
                        cashBalance: summary == null || summary.entries.isEmpty
                            ? null
                            : summary.entries.first.cashBalance,
                      ),
                      const SizedBox(height: 12),
                      _LedgerStatusCard(
                        ledgerVersion: summary?.metadata.ledgerVersion,
                        asOf: summary?.metadata.asOf,
                      ),
                      const SizedBox(height: 12),
                      if (!paired) ...<Widget>[
                        const PairingPanel(),
                        const SizedBox(height: 12),
                      ],
                      _ConnectionActions(paired: paired),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _OverviewHeader extends StatelessWidget {
  const _OverviewHeader();

  @override
  Widget build(BuildContext context) {
    return Row(
      children: <Widget>[
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                'LYRA / LEDGER 01',
                style: Theme.of(context).textTheme.labelSmall,
              ),
              const SizedBox(height: 4),
              Text('总览', style: Theme.of(context).textTheme.titleLarge),
            ],
          ),
        ),
        Semantics(
          button: true,
          label: '账户中心',
          child: IconButton.outlined(
            onPressed: null,
            icon: const Icon(LyraIcons.accountCenter),
            tooltip: '账户中心将在后续接入',
          ),
        ),
      ],
    );
  }
}

class _AccountScopePill extends StatelessWidget {
  const _AccountScopePill();

  @override
  Widget build(BuildContext context) {
    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        padding: const EdgeInsets.fromLTRB(14, 10, 12, 10),
        decoration: BoxDecoration(
          color: LyraColors.surfaceStrong,
          borderRadius: BorderRadius.circular(999),
        ),
        child: const Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Icon(
              LyraIcons.accountScope,
              size: 18,
              color: LyraColors.accent,
            ),
            SizedBox(width: 8),
            Text(
              '全部活跃账户',
              style: TextStyle(
                fontWeight: FontWeight.w600,
                color: LyraColors.ink,
              ),
            ),
            SizedBox(width: 6),
            Icon(LyraIcons.disclosure, size: 18, color: LyraColors.muted),
          ],
        ),
      ),
    );
  }
}

class _LedgerHero extends StatelessWidget {
  const _LedgerHero({
    required this.connected,
    this.realizedPnl,
    this.currency,
    this.ledgerVersion,
  });

  final bool connected;
  final String? realizedPnl;
  final String? currency;
  final int? ledgerVersion;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final double heroSize = math.min(
          64,
          math.max(44, constraints.maxWidth * 0.13),
        );
        return Container(
          width: double.infinity,
          padding: EdgeInsets.all(constraints.maxWidth < 380 ? 20 : 24),
          decoration: BoxDecoration(
            color: LyraColors.surface,
            borderRadius: BorderRadius.circular(24),
            border: Border.all(color: LyraColors.hairlineStrong),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Row(
                children: <Widget>[
                  Text(
                    '账本已实现损益',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  const Spacer(),
                  _ConnectionBadge(connected: connected),
                ],
              ),
              const SizedBox(height: 26),
              Text(
                realizedPnl ?? '—',
                key: const ValueKey<String>('overview-hero-value'),
                style: Theme.of(context).textTheme.displayLarge
                    ?.merge(LyraTheme.numeric)
                    .copyWith(
                      fontSize: heroSize,
                      color: realizedPnl == null
                          ? LyraColors.mutedSoft
                          : LyraColors.ink,
                    ),
              ),
              const SizedBox(height: 10),
              Text(
                _caption(),
                style: Theme.of(context).textTheme.bodyMedium
                    ?.copyWith(color: LyraColors.muted),
              ),
            ],
          ),
        );
      },
    );
  }

  String _caption() {
    if (!connected) {
      return '历史成本与 FIFO 口径 · 尚未连接服务端';
    }
    if (realizedPnl == null || currency == null) {
      return '历史成本与 FIFO 口径 · 账本尚无账户数据';
    }
    return '$currency 已实现合计 · 历史成本与 FIFO 口径 · 账本版本 $ledgerVersion';
  }
}

class _ConnectionBadge extends StatelessWidget {
  const _ConnectionBadge({required this.connected});

  final bool connected;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: LyraColors.surfaceStrong,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(
            LyraIcons.security,
            size: 14,
            color: connected ? LyraColors.accent : LyraColors.warning,
          ),
          const SizedBox(width: 6),
          Text(
            connected ? '已配对' : '未连接',
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: connected ? LyraColors.accent : LyraColors.warning,
            ),
          ),
        ],
      ),
    );
  }
}

class _MetricGrid extends StatelessWidget {
  const _MetricGrid({this.netDeposits, this.cashBalance});

  final String? netDeposits;
  final String? cashBalance;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final int columns = constraints.maxWidth >= 660 ? 3 : 2;
        const double gap = 12;
        final double width =
            (constraints.maxWidth - gap * (columns - 1)) / columns;
        final List<_MetricData> metrics = <_MetricData>[
          _MetricData('净入金', netDeposits ?? '—', '现金流口径', LyraIcons.deposit),
          _MetricData('持仓成本', '—', '非实时市值', LyraIcons.cost),
          _MetricData('现金余额', cashBalance ?? '—', '账本计算值', LyraIcons.cash),
        ];
        return Wrap(
          spacing: gap,
          runSpacing: gap,
          children: metrics
              .map(
                (_MetricData metric) => SizedBox(
                  width: width,
                  child: _MetricCard(data: metric),
                ),
              )
              .toList(growable: false),
        );
      },
    );
  }
}

class _MetricData {
  const _MetricData(this.label, this.value, this.note, this.icon);

  final String label;
  final String value;
  final String note;
  final IconData icon;
}

class _MetricCard extends StatelessWidget {
  const _MetricCard({required this.data});

  final _MetricData data;

  @override
  Widget build(BuildContext context) {
    return Container(
      constraints: const BoxConstraints(minHeight: 138),
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: LyraColors.surface,
        borderRadius: BorderRadius.circular(24),
        border: Border.all(color: LyraColors.hairlineStrong),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            data.label,
            style: Theme.of(context).textTheme.bodySmall
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 14),
          Text(
            data.value,
            style: Theme.of(context).textTheme.headlineSmall
                ?.merge(LyraTheme.numeric)
                .copyWith(
                  fontSize: 20,
                  fontWeight: FontWeight.w500,
                  color: data.value == '—'
                      ? LyraColors.mutedSoft
                      : LyraColors.ink,
                ),
          ),
          const SizedBox(height: 6),
          Text(
            data.note,
            style: Theme.of(context).textTheme.labelSmall?.copyWith(
              color: LyraColors.muted,
              letterSpacing: 0,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}

class _LedgerStatusCard extends StatelessWidget {
  const _LedgerStatusCard({this.ledgerVersion, this.asOf});

  final int? ledgerVersion;
  final String? asOf;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(22, 18, 22, 18),
      decoration: BoxDecoration(
        color: LyraColors.surface,
        borderRadius: BorderRadius.circular(24),
        border: Border.all(color: LyraColors.hairlineStrong),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text('数据语境', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 6),
          _StatusRow(
            icon: LyraIcons.ledgerVersion,
            label: '账本版本',
            value: ledgerVersion == null ? '—' : '$ledgerVersion',
          ),
          _StatusRow(
            icon: LyraIcons.dataCutoff,
            label: '数据截止',
            value: asOf ?? '尚未获取',
          ),
          _StatusRow(
            icon: LyraIcons.sync,
            label: '同步状态',
            value: asOf == null ? '等待服务端连接' : '已同步',
            isLast: true,
          ),
        ],
      ),
    );
  }
}

class _StatusRow extends StatelessWidget {
  const _StatusRow({
    required this.icon,
    required this.label,
    required this.value,
    this.isLast = false,
  });

  final IconData icon;
  final String label;
  final String value;
  final bool isLast;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: EdgeInsets.only(bottom: isLast ? 0 : 12, top: isLast ? 12 : 0),
      decoration: BoxDecoration(
        border: isLast
            ? null
            : const Border(bottom: BorderSide(color: LyraColors.hairline)),
      ),
      child: Row(
        children: <Widget>[
          Icon(icon, size: 18, color: LyraColors.muted),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              label,
              style: const TextStyle(color: LyraColors.inkSecondary),
            ),
          ),
          // 时间戳等长值允许换行，避免窄屏溢出破坏可读性。
          Flexible(
            child: Text(
              value,
              textAlign: TextAlign.right,
              style: const TextStyle(
                color: LyraColors.ink,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _ConnectionActions extends StatelessWidget {
  const _ConnectionActions({required this.paired});

  final bool paired;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        color: LyraColors.surface,
        borderRadius: BorderRadius.circular(24),
        border: Border.all(color: LyraColors.hairlineStrong),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Text('开始建立账本', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          Text(
            '服务端 API 接通后启用导入与 Google 授权。',
            style: Theme.of(context).textTheme.bodyMedium
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 18),
          FilledButton.icon(
            onPressed: paired
                ? () => showModalBottomSheet<void>(
                    context: context,
                    isScrollControlled: true,
                    backgroundColor: LyraColors.surface,
                    shape: const RoundedRectangleBorder(
                      borderRadius: BorderRadius.vertical(
                        top: Radius.circular(20),
                      ),
                    ),
                    builder: (BuildContext sheetContext) => Padding(
                      padding: EdgeInsets.only(
                        bottom: MediaQuery.of(sheetContext).viewInsets.bottom,
                      ),
                      child: const ImportSheet(),
                    ),
                  )
                : null,
            icon: const Icon(LyraIcons.importFile),
            label: const Text('导入 Schwab 文件'),
          ),
          const SizedBox(height: 10),
          OutlinedButton.icon(
            onPressed: null,
            icon: const Icon(LyraIcons.gmail),
            label: const Text('连接 Gmail'),
          ),
        ],
      ),
    );
  }
}
