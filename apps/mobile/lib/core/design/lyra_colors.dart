import 'package:flutter/material.dart';

/// Lyra 极简专业白色主题调色板。
///
/// 纯白画布 + 近黑正文 + 发丝分割线；强调色只出现在关键动作与关键数字，
/// 语义色(涨跌/警示)保持低饱和度，避免装饰性色块。
abstract final class LyraColors {
  // 品牌与强调。
  static const Color accent = Color(0xFF4F46E5);
  static const Color accentSoft = Color(0xFFEEF0FE);
  static const Color accentDeep = Color(0xFF3730A3);

  // 画布与表面。
  static const Color canvas = Color(0xFFFFFFFF);
  static const Color surface = Color(0xFFFFFFFF);
  static const Color surfaceSubtle = Color(0xFFF8F9FC);

  // 文字。
  static const Color ink = Color(0xFF101828);
  static const Color inkSecondary = Color(0xFF475467);
  static const Color muted = Color(0xFF98A2B3);

  // 分割线。
  static const Color hairline = Color(0xFFEAECF0);
  static const Color hairlineStrong = Color(0xFFD0D5DD);

  // 金融语义。
  static const Color positive = Color(0xFF079455);
  static const Color negative = Color(0xFFD92D20);
  static const Color warning = Color(0xFFB54708);
  static const Color warningSoft = Color(0xFFFDF4E7);
}
