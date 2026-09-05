import 'package:flutter/material.dart';

/// Lyra 轻量机构风调色板(参照 Coinbase 设计系统 token)。
///
/// 白色画布 + 近黑 ink;蓝色是"单一电压"——只出现在主 CTA、选中态与
/// 链接;涨跌语义只用于文字色,不做大面积填充;深度靠表面层次而非阴影。
abstract final class LyraColors {
  // 品牌与强调。
  static const Color accent = Color(0xFF0052FF);
  static const Color accentActive = Color(0xFF003ECC);
  static const Color accentDisabled = Color(0xFFA8B8CC);
  static const Color accentSoft = Color(0xFFEEF0F3);

  // 画布与表面。
  static const Color canvas = Color(0xFFFFFFFF);
  static const Color surface = Color(0xFFFFFFFF);
  static const Color surfaceSubtle = Color(0xFFF7F7F7);
  static const Color surfaceStrong = Color(0xFFEEF0F3);

  // 文字。
  static const Color ink = Color(0xFF0A0B0D);
  static const Color inkSecondary = Color(0xFF5B616E);
  static const Color muted = Color(0xFF7C828A);
  static const Color mutedSoft = Color(0xFFA8ACB3);

  // 分割线。
  static const Color hairline = Color(0xFFEEF0F3);
  static const Color hairlineStrong = Color(0xFFDEE1E6);

  // 金融语义(仅用于文字,不做大面积填充)。
  static const Color positive = Color(0xFF05B169);
  static const Color negative = Color(0xFFCF202F);
  static const Color warning = Color(0xFFB54708);
  static const Color warningSoft = Color(0xFFF7F7F7);
}
