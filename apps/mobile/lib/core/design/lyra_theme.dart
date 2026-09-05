import 'package:flutter/material.dart';

import 'lyra_colors.dart';

/// 全局 Material 3 主题：轻量机构风(参照 Coinbase 设计系统)。
abstract final class LyraTheme {
  /// 数字专用等宽字形;用于金额、时间戳等表格化数值。
  static const TextStyle numeric = TextStyle(
    fontFamily: 'monospace',
    fontFeatures: <FontFeature>[FontFeature.tabularFigures()],
  );

  static ThemeData get light {
    final ThemeData base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.light,
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
        // 展示级标题 weight 400 不加粗,靠字号与紧字距建立层级。
        displayLarge: base.textTheme.displayLarge?.copyWith(
          fontSize: 56,
          height: 1.02,
          letterSpacing: -1.6,
          fontWeight: FontWeight.w400,
          color: LyraColors.ink,
        ),
        headlineLarge: base.textTheme.headlineLarge?.copyWith(
          fontSize: 34,
          height: 1.08,
          letterSpacing: -1.0,
          fontWeight: FontWeight.w400,
          color: LyraColors.ink,
        ),
        headlineSmall: base.textTheme.headlineSmall?.copyWith(
          fontSize: 22,
          letterSpacing: -0.3,
          fontWeight: FontWeight.w400,
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
          minimumSize: const Size.fromHeight(44),
          textStyle: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
          shape: const StadiumBorder(),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: LyraColors.ink,
          disabledForegroundColor: LyraColors.mutedSoft,
          backgroundColor: LyraColors.surface,
          minimumSize: const Size.fromHeight(44),
          side: const BorderSide(color: LyraColors.hairlineStrong),
          textStyle: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
          shape: const StadiumBorder(),
        ),
      ),
      navigationBarTheme: NavigationBarThemeData(
        height: 72,
        backgroundColor: LyraColors.surface,
        surfaceTintColor: LyraColors.surface,
        indicatorColor: LyraColors.surfaceStrong,
        iconTheme: WidgetStateProperty.resolveWith<IconThemeData>(
          (Set<WidgetState> states) => IconThemeData(
            color: states.contains(WidgetState.selected)
                ? LyraColors.accent
                : LyraColors.muted,
            size: 23,
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
