import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';

/// 无服务端连接时的可信总览骨架。
///
/// 所有金额显示为缺省符号，不使用演示数字冒充真实金融数据。
class OverviewScreen extends StatelessWidget {
  const OverviewScreen({super.key});

  static const double _contentMaxWidth = 920;

  @override
  Widget build(BuildContext context) {
    return ColoredBox(
      color: LyraColors.black,
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
                    children: const <Widget>[
                      _OverviewHeader(),
                      SizedBox(height: 24),
                      _AccountScopePill(),
                      SizedBox(height: 18),
                      _LedgerHero(),
                      SizedBox(height: 20),
                      _MetricGrid(),
                      SizedBox(height: 16),
                      _LedgerStatusCard(),
                      SizedBox(height: 16),
                      _ConnectionActions(),
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
                style: Theme.of(context).textTheme.labelSmall
                    ?.copyWith(color: LyraColors.muted),
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
          color: LyraColors.elevated,
          borderRadius: BorderRadius.circular(18),
          border: Border.all(color: LyraColors.divider),
        ),
        child: const Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            Icon(
              LyraIcons.accountScope,
              size: 18,
              color: LyraColors.brandBright,
            ),
            SizedBox(width: 8),
            Text('全部活跃账户', style: TextStyle(fontWeight: FontWeight.w600)),
            SizedBox(width: 6),
            Icon(LyraIcons.disclosure, size: 18, color: LyraColors.muted),
          ],
        ),
      ),
    );
  }
}

class _LedgerHero extends StatelessWidget {
  const _LedgerHero();

  @override
  Widget build(BuildContext context) {
    final bool reduceMotion = MediaQuery.disableAnimationsOf(context);
    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final double heroSize = math.min(
          72,
          math.max(48, constraints.maxWidth * 0.145),
        );
        return Container(
          height: constraints.maxWidth < 380 ? 286 : 310,
          clipBehavior: Clip.antiAlias,
          decoration: BoxDecoration(
            color: LyraColors.elevated,
            borderRadius: BorderRadius.circular(32),
          ),
          child: Stack(
            fit: StackFit.expand,
            children: <Widget>[
              TweenAnimationBuilder<double>(
                duration: reduceMotion
                    ? Duration.zero
                    : const Duration(milliseconds: 1100),
                curve: Curves.easeOutCubic,
                tween: Tween<double>(begin: 0, end: 1),
                builder:
                    (BuildContext context, double progress, Widget? child) {
                      return CustomPaint(painter: _LedgerArcPainter(progress));
                    },
              ),
              Padding(
                padding: const EdgeInsets.all(24),
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
                        const _ConnectionBadge(),
                      ],
                    ),
                    const Spacer(),
                    Text(
                      '—',
                      key: const ValueKey<String>('overview-hero-value'),
                      style: Theme.of(context).textTheme.displayLarge?.copyWith(
                        fontSize: heroSize,
                        color: LyraColors.white,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      '历史成本与 FIFO 口径 · 尚未连接服务端',
                      style: Theme.of(context).textTheme.bodyMedium
                          ?.copyWith(color: LyraColors.muted),
                    ),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _ConnectionBadge extends StatelessWidget {
  const _ConnectionBadge();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
      decoration: BoxDecoration(
        color: LyraColors.black.withValues(alpha: 0.48),
        borderRadius: BorderRadius.circular(99),
        border: Border.all(color: LyraColors.divider),
      ),
      child: const Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(LyraIcons.security, size: 15, color: LyraColors.warning),
          SizedBox(width: 6),
          Text(
            '未连接',
            style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700),
          ),
        ],
      ),
    );
  }
}

class _MetricGrid extends StatelessWidget {
  const _MetricGrid();

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final int columns = constraints.maxWidth >= 660 ? 3 : 2;
        const double gap = 12;
        final double width =
            (constraints.maxWidth - gap * (columns - 1)) / columns;
        const List<_MetricData> metrics = <_MetricData>[
          _MetricData('净入金', '—', '现金流口径', LyraIcons.deposit),
          _MetricData('持仓成本', '—', '非实时市值', LyraIcons.cost),
          _MetricData('现金余额', '—', '账本计算值', LyraIcons.cash),
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
      constraints: const BoxConstraints(minHeight: 148),
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: LyraColors.elevated,
        borderRadius: BorderRadius.circular(24),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Icon(data.icon, size: 20, color: LyraColors.brandBright),
          const SizedBox(height: 18),
          Text(
            data.label,
            style: Theme.of(context).textTheme.bodySmall
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 4),
          Text(
            data.value,
            style: Theme.of(context).textTheme.headlineSmall
                ?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 5),
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
  const _LedgerStatusCard();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        color: LyraColors.warm,
        borderRadius: BorderRadius.circular(28),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            '数据语境',
            style: Theme.of(context).textTheme.titleLarge
                ?.copyWith(color: LyraColors.ink),
          ),
          const SizedBox(height: 18),
          const _StatusRow(
            icon: LyraIcons.ledgerVersion,
            label: '账本版本',
            value: '—',
          ),
          const _StatusRow(
            icon: LyraIcons.dataCutoff,
            label: '数据截止',
            value: '尚未获取',
          ),
          const _StatusRow(
            icon: LyraIcons.sync,
            label: '同步状态',
            value: '等待服务端连接',
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
      padding: EdgeInsets.only(bottom: isLast ? 0 : 14, top: isLast ? 14 : 0),
      decoration: BoxDecoration(
        border: isLast
            ? null
            : const Border(bottom: BorderSide(color: Color(0xFFDADBD7))),
      ),
      child: Row(
        children: <Widget>[
          Icon(icon, size: 20, color: LyraColors.brandDeep),
          const SizedBox(width: 12),
          Expanded(
            child: Text(label, style: const TextStyle(color: LyraColors.ink)),
          ),
          Text(
            value,
            style: const TextStyle(
              color: LyraColors.ink,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }
}

class _ConnectionActions extends StatelessWidget {
  const _ConnectionActions();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        color: LyraColors.elevated,
        borderRadius: BorderRadius.circular(28),
        border: Border.all(color: LyraColors.divider),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Text('开始建立账本', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          Text(
            '服务端 API 接通后启用导入与 Google 授权。',
            style: Theme.of(context).textTheme.bodyMedium
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 18),
          FilledButton.icon(
            onPressed: null,
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

/// 低成本的一次性背景绘制；不在滚动或空闲时持续刷新。
class _LedgerArcPainter extends CustomPainter {
  const _LedgerArcPainter(this.progress);

  final double progress;

  @override
  void paint(Canvas canvas, Size size) {
    final Offset center = Offset(size.width * 0.78, size.height * 0.38);
    final double radius = math.min(size.width, size.height) * 0.52;

    final Paint glow = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 34
      ..strokeCap = StrokeCap.round
      ..color = LyraColors.brandPrimary.withValues(alpha: 0.24 * progress)
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 28);
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: radius),
      -math.pi * 0.82,
      math.pi * 1.24 * progress,
      false,
      glow,
    );

    final Paint line = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.2
      ..strokeCap = StrokeCap.round
      ..shader = const LinearGradient(
        colors: <Color>[
          LyraColors.brandBright,
          LyraColors.brandPrimary,
          Color(0x00676CFF),
        ],
      ).createShader(Rect.fromLTWH(0, 0, size.width, size.height));
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: radius),
      -math.pi * 0.82,
      math.pi * 1.24 * progress,
      false,
      line,
    );

    final Paint grid = Paint()
      ..strokeWidth = 0.7
      ..color = LyraColors.white.withValues(alpha: 0.055);
    for (double y = 36; y < size.height; y += 36) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), grid);
    }
  }

  @override
  bool shouldRepaint(covariant _LedgerArcPainter oldDelegate) {
    return oldDelegate.progress != progress;
  }
}
