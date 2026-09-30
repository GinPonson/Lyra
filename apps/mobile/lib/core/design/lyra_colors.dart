import 'package:flutter/material.dart';

/// 金融工作台的白色、石墨黑与清晰绿色；涨跌颜色仅表达真实金额的符号。
abstract final class LyraColors {
  // 品牌与强调：选中态、链接和主操作共享同一绿色相。
  static const Color accent = Color(0xFF087F5B);
  static const Color accentDisabled = Color(0xFFAACBC0);
  static const Color accentTint = Color(0xFFE8F5EE);

  // 画布与表面。
  static const Color canvas = Color(0xFFFFFFFF);
  static const Color surface = Color(0xFFFFFFFF);
  static const Color surfaceSubtle = Color(0xFFF5F6F7);
  static const Color surfaceStrong = Color(0xFFEBEEF0);

  // 文字。
  static const Color ink = Color(0xFF15191C);
  static const Color inkSecondary = Color(0xFF596169);
  static const Color muted = Color(0xFF6D767D);
  static const Color mutedSoft = Color(0xFF8A949C);

  // 分割线。
  static const Color hairline = Color(0xFFEDF0F2);
  static const Color hairlineStrong = Color(0xFFDCE1E5);

  // 金融语义(仅用于文字,不做大面积填充)。
  static const Color positive = Color(0xFF087F5B);
  static const Color negative = Color(0xFFD34444);
  static const Color warning = Color(0xFFB54708);
}
