import 'package:flutter/material.dart';

import 'lyra_colors.dart';

/// 全局 Material 3 主题。
abstract final class LyraTheme {
  static ThemeData get dark {
    final ThemeData base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: const ColorScheme.dark(
        primary: LyraColors.brandBright,
        onPrimary: LyraColors.white,
        primaryContainer: LyraColors.brandDeep,
        onPrimaryContainer: LyraColors.white,
        secondary: LyraColors.positive,
        onSecondary: LyraColors.black,
        error: LyraColors.negative,
        onError: LyraColors.black,
        surface: LyraColors.deep,
        onSurface: LyraColors.white,
        surfaceContainerHighest: LyraColors.elevatedStrong,
        onSurfaceVariant: LyraColors.muted,
        outline: LyraColors.divider,
      ),
      scaffoldBackgroundColor: LyraColors.black,
    );

    return base.copyWith(
      textTheme: base.textTheme.copyWith(
        displayLarge: base.textTheme.displayLarge?.copyWith(
          fontSize: 64,
          height: 0.96,
          letterSpacing: -3.2,
          fontWeight: FontWeight.w700,
        ),
        headlineLarge: base.textTheme.headlineLarge?.copyWith(
          fontSize: 40,
          height: 1.02,
          letterSpacing: -1.6,
          fontWeight: FontWeight.w700,
        ),
        titleLarge: base.textTheme.titleLarge?.copyWith(
          fontWeight: FontWeight.w700,
          letterSpacing: -0.4,
        ),
        titleMedium: base.textTheme.titleMedium?.copyWith(
          fontWeight: FontWeight.w600,
        ),
        labelSmall: base.textTheme.labelSmall?.copyWith(
          fontWeight: FontWeight.w700,
          letterSpacing: 1.3,
        ),
      ),
      navigationBarTheme: NavigationBarThemeData(
        height: 76,
        backgroundColor: LyraColors.deep.withValues(alpha: 0.96),
        indicatorColor: LyraColors.brandPrimary,
        iconTheme: WidgetStateProperty.resolveWith<IconThemeData>(
          (Set<WidgetState> states) => IconThemeData(
            color: states.contains(WidgetState.selected)
                ? LyraColors.white
                : LyraColors.muted,
            size: 23,
          ),
        ),
        labelTextStyle: WidgetStateProperty.resolveWith<TextStyle>(
          (Set<WidgetState> states) => TextStyle(
            color: states.contains(WidgetState.selected)
                ? LyraColors.white
                : LyraColors.muted,
            fontSize: 11,
            fontWeight: states.contains(WidgetState.selected)
                ? FontWeight.w700
                : FontWeight.w500,
          ),
        ),
      ),
      navigationRailTheme: const NavigationRailThemeData(
        backgroundColor: LyraColors.deep,
        indicatorColor: LyraColors.brandPrimary,
        selectedIconTheme: IconThemeData(color: LyraColors.white),
        unselectedIconTheme: IconThemeData(color: LyraColors.muted),
        selectedLabelTextStyle: TextStyle(
          color: LyraColors.white,
          fontWeight: FontWeight.w700,
        ),
        unselectedLabelTextStyle: TextStyle(color: LyraColors.muted),
      ),
      dividerTheme: const DividerThemeData(
        color: LyraColors.divider,
        thickness: 1,
        space: 1,
      ),
      cardTheme: const CardThemeData(
        color: LyraColors.elevated,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(24)),
        ),
      ),
    );
  }
}
