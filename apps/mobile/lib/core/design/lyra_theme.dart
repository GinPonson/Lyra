import 'package:flutter/material.dart';

import 'lyra_colors.dart';

/// 全局 Material 3 主题：白色画布、金融数字排版与绿色交互。
///
/// 文本与金额统一使用 Geist Sans；金额启用表格数字，中文使用系统字体。
abstract final class LyraTheme {
  /// 品牌无衬线字体族。
  static const String fontFamily = 'Geist';

  /// 等宽数字字体族。
  static const String monoFamily = 'Geist Mono';

  /// 数字表格化字形;用于金额、时间戳等数值的对齐稳定性。
  static const TextStyle numeric = TextStyle(
    fontFamily: fontFamily,
    fontFeatures: <FontFeature>[FontFeature.tabularFigures()],
  );

  static ThemeData get light {
    final ThemeData base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.light,
      fontFamily: fontFamily,
      fontFamilyFallback: const [
        'PingFang SC',
        'Noto Sans CJK SC',
        'sans-serif',
      ],
      colorScheme: const ColorScheme.light(
        primary: LyraColors.accent,
        onPrimary: LyraColors.surface,
        primaryContainer: LyraColors.surfaceStrong,
        onPrimaryContainer: LyraColors.ink,
        secondary: LyraColors.positive,
        onSecondary: LyraColors.surface,
        error: LyraColors.negative,
        onError: LyraColors.surface,
        surface: LyraColors.surface,
        onSurface: LyraColors.ink,
        surfaceContainerHighest: LyraColors.surfaceSubtle,
        onSurfaceVariant: LyraColors.inkSecondary,
        outline: LyraColors.hairlineStrong,
        outlineVariant: LyraColors.hairline,
      ),
      scaffoldBackgroundColor: LyraColors.canvas,
    );

    return base.copyWith(
      textTheme: base.textTheme.copyWith(
        // 展示级数字:靠字号、紧字距与中等字重建立层级,不加粗到发闷。
        displayLarge: base.textTheme.displayLarge?.copyWith(
          fontSize: 50,
          height: 1.02,
          letterSpacing: -1.8,
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
        headlineLarge: base.textTheme.headlineLarge?.copyWith(
          fontSize: 30,
          height: 1.1,
          letterSpacing: -0.6,
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
        headlineSmall: base.textTheme.headlineSmall?.copyWith(
          fontSize: 21,
          letterSpacing: -0.3,
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        titleLarge: base.textTheme.titleLarge?.copyWith(
          fontSize: 18,
          fontWeight: FontWeight.w600,
          letterSpacing: -0.1,
          color: LyraColors.ink,
        ),
        titleMedium: base.textTheme.titleMedium?.copyWith(
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
        titleSmall: base.textTheme.titleSmall?.copyWith(
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
        bodyLarge: base.textTheme.bodyLarge?.copyWith(
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        bodyMedium: base.textTheme.bodyMedium?.copyWith(
          color: LyraColors.inkSecondary,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        bodySmall: base.textTheme.bodySmall?.copyWith(
          color: LyraColors.inkSecondary,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        labelSmall: base.textTheme.labelSmall?.copyWith(
          fontWeight: FontWeight.w600,
          letterSpacing: 1.0,
          color: LyraColors.muted,
        ),
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: LyraColors.canvas,
        foregroundColor: LyraColors.ink,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontFamily: fontFamily,
          fontFamilyFallback: ['PingFang SC', 'Noto Sans CJK SC', 'sans-serif'],
          fontSize: 17,
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: LyraColors.canvas,
        selectedColor: LyraColors.ink,
        side: const BorderSide(color: LyraColors.hairlineStrong),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
        labelStyle: const TextStyle(
          fontFamily: fontFamily,
          fontFamilyFallback: ['PingFang SC', 'Noto Sans CJK SC', 'sans-serif'],
          fontSize: 12,
          color: LyraColors.inkSecondary,
        ),
      ),
      inputDecorationTheme: InputDecorationThemeData(
        filled: true,
        fillColor: LyraColors.surface,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 16,
          vertical: 14,
        ),
        border: _inputBorder(LyraColors.hairlineStrong, 1),
        enabledBorder: _inputBorder(LyraColors.hairlineStrong, 1),
        focusedBorder: _inputBorder(LyraColors.accent, 2),
        hintStyle: const TextStyle(color: LyraColors.mutedSoft),
        labelStyle: const TextStyle(color: LyraColors.inkSecondary),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: LyraColors.accent,
          foregroundColor: LyraColors.surface,
          disabledBackgroundColor: LyraColors.accentDisabled,
          disabledForegroundColor: LyraColors.surface,
          minimumSize: const Size(0, 48),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          textStyle: const TextStyle(
            fontFamily: fontFamily,
            fontFamilyFallback: [
              'PingFang SC',
              'Noto Sans CJK SC',
              'sans-serif',
            ],
            fontWeight: FontWeight.w600,
            fontSize: 15,
          ),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: LyraColors.ink,
          disabledForegroundColor: LyraColors.mutedSoft,
          backgroundColor: LyraColors.surface,
          minimumSize: const Size(0, 48),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          side: const BorderSide(color: LyraColors.hairlineStrong),
          textStyle: const TextStyle(
            fontFamily: fontFamily,
            fontFamilyFallback: [
              'PingFang SC',
              'Noto Sans CJK SC',
              'sans-serif',
            ],
            fontWeight: FontWeight.w600,
            fontSize: 15,
          ),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
        ),
      ),
      // 底部保留图标与文字，使用轻色选中背景帮助识别当前入口。
      navigationBarTheme: NavigationBarThemeData(
        height: 76,
        backgroundColor: LyraColors.surface,
        surfaceTintColor: LyraColors.surface,
        indicatorColor: LyraColors.accentTint,
        iconTheme: WidgetStateProperty.resolveWith<IconThemeData>(
          (Set<WidgetState> states) => IconThemeData(
            color: states.contains(WidgetState.selected)
                ? LyraColors.ink
                : LyraColors.muted,
            size: 24,
          ),
        ),
        labelTextStyle: WidgetStateProperty.resolveWith<TextStyle>(
          (Set<WidgetState> states) => TextStyle(
            color: states.contains(WidgetState.selected)
                ? LyraColors.ink
                : LyraColors.muted,
            fontSize: 11,
            fontWeight: states.contains(WidgetState.selected)
                ? FontWeight.w600
                : FontWeight.w500,
          ),
        ),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: LyraColors.canvas,
        indicatorColor: LyraColors.surfaceStrong,
        selectedIconTheme: const IconThemeData(color: LyraColors.accent),
        unselectedIconTheme: const IconThemeData(color: LyraColors.muted),
        selectedLabelTextStyle: const TextStyle(
          color: LyraColors.ink,
          fontWeight: FontWeight.w600,
        ),
        unselectedLabelTextStyle: const TextStyle(color: LyraColors.muted),
      ),
      dividerTheme: const DividerThemeData(
        color: LyraColors.hairline,
        thickness: 1,
        space: 1,
      ),
      cardTheme: CardThemeData(
        color: LyraColors.surface,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: const BorderRadius.all(Radius.circular(24)),
          side: const BorderSide(color: LyraColors.hairlineStrong),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: LyraColors.surface,
        elevation: 8,
        shadowColor: const Color(0x140A0B0D),
        shape: RoundedRectangleBorder(
          borderRadius: const BorderRadius.all(Radius.circular(16)),
          side: const BorderSide(color: LyraColors.hairlineStrong),
        ),
      ),
      bottomSheetTheme: const BottomSheetThemeData(
        backgroundColor: LyraColors.surface,
        showDragHandle: false,
      ),
    );
  }

  static OutlineInputBorder _inputBorder(Color color, double width) {
    return OutlineInputBorder(
      borderRadius: const BorderRadius.all(Radius.circular(12)),
      borderSide: BorderSide(color: color, width: width),
    );
  }
}
