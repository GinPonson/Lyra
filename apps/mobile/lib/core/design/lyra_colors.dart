import 'package:flutter/material.dart';

/// 石墨灰金融工作台：冰蓝表达交互，红绿只表达金额的涨跌语义。
abstract final class LyraColors {
  // 品牌与强调；浅色实心按钮使用独立的深色前景，避免和表面色耦合。
  static const Color accent = Color(0xFFB0CAFA);
  static const Color onAccent = Color(0xFF122038);
  static const Color accentDisabled = Color(0xFF35435B);
  static const Color accentTint = Color(0xFF1B2941);

  // 画布与表面。
  static const Color canvas = Color(0xFF101318);
  static const Color surface = Color(0xFF191D25);
  static const Color surfaceSubtle = Color(0xFF151920);
  static const Color surfaceStrong = Color(0xFF222936);

  // 文字。
  static const Color ink = Color(0xFFF1F4F8);
  static const Color inkSecondary = Color(0xFFA6AFBD);
  static const Color muted = Color(0xFF959FAE);
  static const Color mutedSoft = Color(0xFF8994A5);

  // 分割线。
  static const Color hairline = Color(0xFF242B35);
  static const Color hairlineStrong = Color(0xFF2B3442);

  // 金融语义(仅用于文字,不做大面积填充)。
  static const Color positive = Color(0xFF94D0BC);
  static const Color negative = Color(0xFFF0A0A6);
  static const Color warning = Color(0xFFE8BD7C);
}
