import 'package:flutter/material.dart';

import 'lyra_colors.dart';

/// 全局 Material 3 主题：极简专业白色画布。
abstract final class LyraTheme {
  static ThemeData get light {
    final ThemeData base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.light,
      colorScheme: const ColorScheme.light(
        primary: LyraColors.accent,
        onPrimary: LyraColors.surface,
        primaryContainer: LyraColors.accentSoft,
        onPrimaryContainer: LyraColors.accentDeep,
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
        displayLarge: base.textTheme.displayLarge?.copyWith(
          fontSize: 60,
          height: 1.0,
          letterSpacing: -2.0,
          fontWeight: FontWeight.w700,
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        headlineLarge: base.textTheme.headlineLarge?.copyWith(
          fontSize: 34,
          height: 1.08,
          letterSpacing: -0.8,
          fontWeight: FontWeight.w700,
          color: LyraColors.ink,
        ),
        headlineSmall: base.textTheme.headlineSmall?.copyWith(
          letterSpacing: -0.3,
          fontWeight: FontWeight.w700,
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        titleLarge: base.textTheme.titleLarge?.copyWith(
          fontWeight: FontWeight.w700,
          letterSpacing: -0.3,
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
          fontWeight: FontWeight.w700,
          letterSpacing: 1.1,
          color: LyraColors.muted,
        ),
      ),
      inputDecorationTheme: InputDecorationThemeData(
        filled: true,
        fillColor: LyraColors.surface,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 14,
          vertical: 14,
        ),
        border: _inputBorder(LyraColors.hairlineStrong),
        enabledBorder: _inputBorder(LyraColors.hairlineStrong),
        focusedBorder: _inputBorder(LyraColors.accent),
        hintStyle: const TextStyle(color: LyraColors.muted),
        labelStyle: const TextStyle(color: LyraColors.inkSecondary),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: LyraColors.accent,
          foregroundColor: LyraColors.surface,
          disabledBackgroundColor: LyraColors.hairline,
          disabledForegroundColor: LyraColors.muted,
          minimumSize: const Size.fromHeight(48),
          textStyle: const TextStyle(
            fontWeight: FontWeight.w600,
            fontSize: 15,
          ),
          shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(12)),
          ),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: LyraColors.ink,
          disabledForegroundColor: LyraColors.muted,
          minimumSize: const Size.fromHeight(48),
          side: const BorderSide(color: LyraColors.hairlineStrong),
          textStyle: const TextStyle(
            fontWeight: FontWeight.w600,
            fontSize: 15,
          ),
          shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(12)),
          ),
        ),
      ),
      navigationBarTheme: NavigationBarThemeData(
        height: 72,
        backgroundColor: LyraColors.surface,
        surfaceTintColor: LyraColors.surface,
        indicatorColor: LyraColors.accentSoft,
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
                ? LyraColors.accent
                : LyraColors.muted,
            fontSize: 11,
            fontWeight: states.contains(WidgetState.selected)
                ? FontWeight.w700
                : FontWeight.w500,
          ),
        ),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: LyraColors.canvas,
        indicatorColor: LyraColors.accentSoft,
        selectedIconTheme: const IconThemeData(color: LyraColors.accent),
        unselectedIconTheme: const IconThemeData(color: LyraColors.muted),
        selectedLabelTextStyle: const TextStyle(
          color: LyraColors.accent,
          fontWeight: FontWeight.w700,
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
          borderRadius: const BorderRadius.all(Radius.circular(16)),
          side: const BorderSide(color: LyraColors.hairline),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: LyraColors.surface,
        elevation: 8,
        shadowColor: const Color(0x14101828),
        shape: RoundedRectangleBorder(
          borderRadius: const BorderRadius.all(Radius.circular(12)),
          side: const BorderSide(color: LyraColors.hairline),
        ),
      ),
      bottomSheetTheme: const BottomSheetThemeData(
        backgroundColor: LyraColors.surface,
        showDragHandle: false,
      ),
      splashFactory: InkSparkle.splashFactory,
    );
  }

  static OutlineInputBorder _inputBorder(Color color) {
    return OutlineInputBorder(
      borderRadius: const BorderRadius.all(Radius.circular(12)),
      borderSide: BorderSide(color: color),
    );
  }
}
