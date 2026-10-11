import 'package:flutter/material.dart';
import 'package:hugeicons/hugeicons.dart';

/// 首批统一的一级入口图形，同一业务在导航与快捷入口使用相同标识。
enum LyraPrimaryGlyph {
  overview(HugeIcons.strokeRoundedDashboardSquare01),
  accounts(HugeIcons.strokeRoundedWallet01),
  stocks(HugeIcons.strokeRoundedChartCandlestick),
  ledger(HugeIcons.strokeRoundedInvoice03),
  profile(HugeIcons.strokeRoundedUserCircle),
  importFile(HugeIcons.strokeRoundedFileUpload),
  connection(HugeIcons.strokeRoundedUserSettings01);

  const LyraPrimaryGlyph(this.paths);
  final List<List<dynamic>> paths;
}

/// Hugeicons 圆角描边统一为 24px、1.75px 线宽。
/// [color] 未提供时继承 IconTheme；语义标签由外层按钮统一提供。
class LyraPrimaryIcon extends StatelessWidget {
  const LyraPrimaryIcon(this.glyph, {super.key, this.size = 24, this.color});
  final LyraPrimaryGlyph glyph;
  final double size;
  final Color? color;

  @override
  Widget build(BuildContext context) => ExcludeSemantics(
    child: HugeIcon(
      icon: glyph.paths,
      size: size,
      strokeWidth: 1.75,
      color: color ?? IconTheme.of(context).color,
    ),
  );
}
