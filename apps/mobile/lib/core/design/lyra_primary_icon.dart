import 'package:flutter/material.dart';
import 'package:hugeicons/hugeicons.dart';

/// 导航、账户与偏好共用圆角描边图形，同一业务使用相同标识。
enum LyraPrimaryGlyph {
  overview(HugeIcons.strokeRoundedDashboardSquare01),
  accounts(HugeIcons.strokeRoundedWallet01),
  stocks(HugeIcons.strokeRoundedChartCandlestick),
  ledger(HugeIcons.strokeRoundedInvoice03),
  profile(HugeIcons.strokeRoundedUserCircle),
  importFile(HugeIcons.strokeRoundedFileUpload),
  connection(HugeIcons.strokeRoundedUserSettings01),
  showAmounts(HugeIcons.strokeRoundedView),
  hideAmounts(HugeIcons.strokeRoundedViewOffSlash),
  brokerage(HugeIcons.strokeRoundedBank),
  accountScope(HugeIcons.strokeRoundedLayers01),
  profitColors(HugeIcons.strokeRoundedPaintBoard),
  server(HugeIcons.strokeRoundedServerStack01),
  about(HugeIcons.strokeRoundedInformationCircle),
  logout(HugeIcons.strokeRoundedLogout01);

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
