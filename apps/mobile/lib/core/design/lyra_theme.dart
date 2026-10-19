import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'lyra_colors.dart';

/// 石墨灰画布、冰蓝交互与清晰的金融数字层级。
///
/// 文本与金额使用 Geist Sans；金额启用表格数字，中文使用系统字体。
abstract final class LyraTheme {
  static const String fontFamily = 'Geist';
  static const String monoFamily = 'Geist Mono';

  static const List<String> _fontFallback = [
    'PingFang SC',
    'Noto Sans CJK SC',
    'sans-serif',
  ];

  /// 数字采用等宽字形，金额和时间戳在更新时保持对齐。
  static const TextStyle numeric = TextStyle(
    fontFamily: fontFamily,
    fontFamilyFallback: _fontFallback,
    fontFeatures: <FontFeature>[FontFeature.tabularFigures()],
  );

  /// 状态栏和系统导航栏在深色画布上使用浅色图标。
  static const SystemUiOverlayStyle systemUiOverlayStyle = SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    statusBarBrightness: Brightness.dark,
    statusBarIconBrightness: Brightness.light,
    systemStatusBarContrastEnforced: false,
    systemNavigationBarColor: LyraColors.surfaceSubtle,
    systemNavigationBarDividerColor: LyraColors.surfaceSubtle,
    systemNavigationBarIconBrightness: Brightness.light,
    systemNavigationBarContrastEnforced: false,
  );

  static ThemeData get dark {
    final ThemeData base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      fontFamily: fontFamily,
      fontFamilyFallback: _fontFallback,
      applyElevationOverlayColor: false,
      colorScheme: const ColorScheme.dark(
        primary: LyraColors.accent,
        onPrimary: LyraColors.onAccent,
        primaryContainer: LyraColors.accentTint,
        onPrimaryContainer: LyraColors.accent,
        secondary: LyraColors.accent,
        onSecondary: LyraColors.onAccent,
        secondaryContainer: LyraColors.accentTint,
        onSecondaryContainer: LyraColors.accent,
        tertiary: LyraColors.accent,
        onTertiary: LyraColors.onAccent,
        tertiaryContainer: LyraColors.accentTint,
        onTertiaryContainer: LyraColors.accent,
        error: LyraColors.negative,
        onError: LyraColors.onAccent,
        errorContainer: Color(0xFF3B222A),
        onErrorContainer: LyraColors.negative,
        surface: LyraColors.surface,
        onSurface: LyraColors.ink,
        surfaceDim: LyraColors.canvas,
        surfaceBright: LyraColors.surfaceStrong,
        surfaceContainerLowest: LyraColors.canvas,
        surfaceContainerLow: LyraColors.surfaceSubtle,
        surfaceContainer: LyraColors.surface,
        surfaceContainerHigh: LyraColors.surfaceStrong,
        surfaceContainerHighest: LyraColors.surfaceStrong,
        onSurfaceVariant: LyraColors.inkSecondary,
        outline: LyraColors.hairlineStrong,
        outlineVariant: LyraColors.hairline,
        inverseSurface: Color(0xFFE5EBF5),
        onInverseSurface: LyraColors.canvas,
        inversePrimary: LyraColors.onAccent,
        surfaceTint: Colors.transparent,
        shadow: Colors.black,
        scrim: Colors.black,
      ),
      scaffoldBackgroundColor: LyraColors.canvas,
      canvasColor: LyraColors.surface,
      disabledColor: LyraColors.mutedSoft,
      dividerColor: LyraColors.hairline,
    );

    final inputTheme = InputDecorationThemeData(
      filled: true,
      fillColor: LyraColors.surfaceSubtle,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      border: _inputBorder(LyraColors.hairlineStrong, 1),
      enabledBorder: _inputBorder(LyraColors.hairlineStrong, 1),
      focusedBorder: _inputBorder(LyraColors.accent, 1.5),
      disabledBorder: _inputBorder(LyraColors.hairline, 1),
      errorBorder: _inputBorder(LyraColors.negative, 1),
      focusedErrorBorder: _inputBorder(LyraColors.negative, 1.5),
      hintStyle: const TextStyle(color: LyraColors.mutedSoft),
      labelStyle: const TextStyle(color: LyraColors.inkSecondary),
      floatingLabelStyle: const TextStyle(color: LyraColors.accent),
      helperStyle: const TextStyle(color: LyraColors.muted),
      errorStyle: const TextStyle(color: LyraColors.negative),
      prefixIconColor: LyraColors.inkSecondary,
      suffixIconColor: LyraColors.inkSecondary,
    );
    const menuStyle = MenuStyle(
      backgroundColor: WidgetStatePropertyAll(LyraColors.surfaceStrong),
      surfaceTintColor: WidgetStatePropertyAll(Colors.transparent),
      shadowColor: WidgetStatePropertyAll(Color(0x66000000)),
      elevation: WidgetStatePropertyAll(8),
      side: WidgetStatePropertyAll(
        BorderSide(color: LyraColors.hairlineStrong),
      ),
      shape: WidgetStatePropertyAll(
        RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(14)),
        ),
      ),
    );
    const buttonTextStyle = TextStyle(
      fontFamily: fontFamily,
      fontFamilyFallback: _fontFallback,
      fontWeight: FontWeight.w500,
      fontSize: 15,
    );
    final buttonShape = RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(12),
    );

    return base.copyWith(
      textTheme: base.textTheme.copyWith(
        displayLarge: base.textTheme.displayLarge?.copyWith(
          fontSize: 46,
          height: 1.08,
          letterSpacing: -1.5,
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
        headlineLarge: base.textTheme.headlineLarge?.copyWith(
          fontSize: 26,
          height: 1.2,
          letterSpacing: -0.5,
          fontWeight: FontWeight.w600,
          color: LyraColors.ink,
        ),
        headlineSmall: base.textTheme.headlineSmall?.copyWith(
          fontSize: 21,
          height: 1.3,
          letterSpacing: -0.3,
          fontWeight: FontWeight.w500,
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        titleLarge: base.textTheme.titleLarge?.copyWith(
          fontSize: 18,
          height: 1.35,
          fontWeight: FontWeight.w500,
          letterSpacing: -0.1,
          color: LyraColors.ink,
        ),
        titleMedium: base.textTheme.titleMedium?.copyWith(
          fontWeight: FontWeight.w500,
          color: LyraColors.ink,
        ),
        titleSmall: base.textTheme.titleSmall?.copyWith(
          fontWeight: FontWeight.w500,
          color: LyraColors.ink,
        ),
        bodyLarge: base.textTheme.bodyLarge?.copyWith(
          height: 1.5,
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        bodyMedium: base.textTheme.bodyMedium?.copyWith(
          height: 1.5,
          color: LyraColors.ink,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        bodySmall: base.textTheme.bodySmall?.copyWith(
          height: 1.5,
          color: LyraColors.inkSecondary,
          fontFeatures: const <FontFeature>[FontFeature.tabularFigures()],
        ),
        labelLarge: base.textTheme.labelLarge?.copyWith(
          fontWeight: FontWeight.w500,
          letterSpacing: 0,
          color: LyraColors.ink,
        ),
        labelMedium: base.textTheme.labelMedium?.copyWith(
          fontWeight: FontWeight.w500,
          letterSpacing: 0.1,
          color: LyraColors.inkSecondary,
        ),
        labelSmall: base.textTheme.labelSmall?.copyWith(
          fontWeight: FontWeight.w500,
          letterSpacing: 0.1,
          color: LyraColors.muted,
        ),
      ),
      iconTheme: const IconThemeData(color: LyraColors.inkSecondary, size: 24),
      primaryIconTheme: const IconThemeData(color: LyraColors.accent),
      appBarTheme: const AppBarTheme(
        backgroundColor: LyraColors.canvas,
        foregroundColor: LyraColors.ink,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        systemOverlayStyle: systemUiOverlayStyle,
        iconTheme: IconThemeData(color: LyraColors.inkSecondary),
        titleTextStyle: TextStyle(
          fontFamily: fontFamily,
          fontFamilyFallback: _fontFallback,
          fontSize: 17,
          fontWeight: FontWeight.w500,
          color: LyraColors.ink,
        ),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: LyraColors.surfaceSubtle,
        selectedColor: LyraColors.accentTint,
        secondarySelectedColor: LyraColors.accentTint,
        disabledColor: LyraColors.surfaceSubtle,
        checkmarkColor: LyraColors.accent,
        deleteIconColor: LyraColors.inkSecondary,
        side: const BorderSide(color: LyraColors.hairlineStrong),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
        labelStyle: const TextStyle(
          fontFamily: fontFamily,
          fontFamilyFallback: _fontFallback,
          fontSize: 12,
          fontWeight: FontWeight.w500,
          color: LyraColors.inkSecondary,
        ),
        secondaryLabelStyle: const TextStyle(
          fontFamily: fontFamily,
          fontFamilyFallback: _fontFallback,
          fontSize: 12,
          fontWeight: FontWeight.w500,
          color: LyraColors.accent,
        ),
      ),
      inputDecorationTheme: inputTheme,
      textSelectionTheme: const TextSelectionThemeData(
        cursorColor: LyraColors.accent,
        selectionColor: Color(0x40B0CAFA),
        selectionHandleColor: LyraColors.accent,
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: LyraColors.accent,
          foregroundColor: LyraColors.onAccent,
          disabledBackgroundColor: LyraColors.surfaceStrong,
          disabledForegroundColor: LyraColors.mutedSoft,
          minimumSize: const Size(0, 48),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          textStyle: buttonTextStyle,
          shape: buttonShape,
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: LyraColors.accentTint,
          foregroundColor: LyraColors.accent,
          disabledBackgroundColor: LyraColors.surfaceStrong,
          disabledForegroundColor: LyraColors.mutedSoft,
          surfaceTintColor: Colors.transparent,
          elevation: 0,
          minimumSize: const Size(0, 48),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          textStyle: buttonTextStyle,
          shape: buttonShape,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: LyraColors.ink,
          disabledForegroundColor: LyraColors.mutedSoft,
          backgroundColor: Colors.transparent,
          minimumSize: const Size(0, 48),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          side: const BorderSide(color: LyraColors.hairlineStrong),
          textStyle: buttonTextStyle,
          shape: buttonShape,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: LyraColors.accent,
          disabledForegroundColor: LyraColors.mutedSoft,
          textStyle: buttonTextStyle,
          shape: buttonShape,
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: ButtonStyle(
          foregroundColor: WidgetStateProperty.resolveWith<Color>((states) {
            if (states.contains(WidgetState.disabled)) {
              return LyraColors.mutedSoft;
            }
            return states.contains(WidgetState.selected)
                ? LyraColors.accent
                : LyraColors.inkSecondary;
          }),
        ),
      ),
      floatingActionButtonTheme: const FloatingActionButtonThemeData(
        backgroundColor: LyraColors.accent,
        foregroundColor: LyraColors.onAccent,
      ),
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith<Color>((states) {
          if (states.contains(WidgetState.disabled)) {
            return LyraColors.mutedSoft;
          }
          return states.contains(WidgetState.selected)
              ? LyraColors.onAccent
              : LyraColors.inkSecondary;
        }),
        trackColor: WidgetStateProperty.resolveWith<Color>((states) {
          if (states.contains(WidgetState.disabled)) {
            return states.contains(WidgetState.selected)
                ? LyraColors.accentDisabled
                : LyraColors.surfaceSubtle;
          }
          return states.contains(WidgetState.selected)
              ? LyraColors.accent
              : LyraColors.surfaceStrong;
        }),
        trackOutlineColor: WidgetStateProperty.resolveWith<Color>(
          (states) => states.contains(WidgetState.selected)
              ? Colors.transparent
              : LyraColors.mutedSoft,
        ),
        trackOutlineWidth: const WidgetStatePropertyAll(1),
      ),
      checkboxTheme: CheckboxThemeData(
        checkColor: const WidgetStatePropertyAll(LyraColors.onAccent),
        fillColor: WidgetStateProperty.resolveWith<Color>((states) {
          if (!states.contains(WidgetState.selected)) {
            return Colors.transparent;
          }
          return states.contains(WidgetState.disabled)
              ? LyraColors.accentDisabled
              : LyraColors.accent;
        }),
        side: const BorderSide(color: LyraColors.mutedSoft, width: 1.5),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(4)),
      ),
      radioTheme: RadioThemeData(
        fillColor: WidgetStateProperty.resolveWith<Color>((states) {
          if (states.contains(WidgetState.disabled)) {
            return LyraColors.mutedSoft;
          }
          return states.contains(WidgetState.selected)
              ? LyraColors.accent
              : LyraColors.inkSecondary;
        }),
      ),
      // 选中块与按压、聚焦和悬停反馈共用原生形状，避免交互层露出两端。
      navigationBarTheme: NavigationBarThemeData(
        height: 76,
        elevation: 0,
        backgroundColor: LyraColors.surfaceSubtle,
        surfaceTintColor: Colors.transparent,
        indicatorColor: LyraColors.accentTint,
        indicatorShape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(10)),
        ),
        overlayColor: WidgetStateProperty.resolveWith<Color?>((states) {
          if (states.contains(WidgetState.pressed)) {
            return LyraColors.accent.withValues(alpha: 0.14);
          }
          if (states.contains(WidgetState.focused)) {
            return LyraColors.accent.withValues(alpha: 0.18);
          }
          if (states.contains(WidgetState.hovered)) {
            return LyraColors.accent.withValues(alpha: 0.08);
          }
          return Colors.transparent;
        }),
        iconTheme: WidgetStateProperty.resolveWith<IconThemeData>(
          (states) => IconThemeData(
            color: states.contains(WidgetState.selected)
                ? LyraColors.accent
                : LyraColors.inkSecondary,
            size: 24,
          ),
        ),
        labelTextStyle: WidgetStateProperty.resolveWith<TextStyle>(
          (states) => TextStyle(
            fontFamily: fontFamily,
            fontFamilyFallback: _fontFallback,
            color: states.contains(WidgetState.selected)
                ? LyraColors.accent
                : LyraColors.inkSecondary,
            fontSize: 12,
            height: 1.25,
            fontWeight: states.contains(WidgetState.selected)
                ? FontWeight.w600
                : FontWeight.w500,
            letterSpacing: 0,
          ),
        ),
      ),
      navigationRailTheme: const NavigationRailThemeData(
        backgroundColor: LyraColors.canvas,
        indicatorColor: LyraColors.accentTint,
        selectedIconTheme: IconThemeData(color: LyraColors.accent),
        unselectedIconTheme: IconThemeData(color: LyraColors.inkSecondary),
        selectedLabelTextStyle: TextStyle(
          color: LyraColors.accent,
          fontWeight: FontWeight.w500,
        ),
        unselectedLabelTextStyle: TextStyle(color: LyraColors.inkSecondary),
      ),
      tabBarTheme: const TabBarThemeData(
        labelColor: LyraColors.accent,
        unselectedLabelColor: LyraColors.inkSecondary,
        indicatorColor: LyraColors.accent,
        dividerColor: LyraColors.hairline,
      ),
      dividerTheme: const DividerThemeData(
        color: LyraColors.hairline,
        thickness: 1,
        space: 1,
      ),
      cardTheme: const CardThemeData(
        color: LyraColors.surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(18)),
          side: BorderSide(color: LyraColors.hairlineStrong),
        ),
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: LyraColors.surfaceStrong,
        elevation: 8,
        surfaceTintColor: Colors.transparent,
        shadowColor: const Color(0x66000000),
        iconColor: LyraColors.inkSecondary,
        textStyle: base.textTheme.bodyMedium?.copyWith(color: LyraColors.ink),
        labelTextStyle: WidgetStateProperty.resolveWith<TextStyle>(
          (states) => TextStyle(
            fontFamily: fontFamily,
            fontFamilyFallback: _fontFallback,
            fontSize: 14,
            color: states.contains(WidgetState.disabled)
                ? LyraColors.mutedSoft
                : LyraColors.ink,
          ),
        ),
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(14)),
          side: BorderSide(color: LyraColors.hairlineStrong),
        ),
      ),
      menuTheme: const MenuThemeData(style: menuStyle),
      dropdownMenuTheme: DropdownMenuThemeData(
        textStyle: base.textTheme.bodyLarge?.copyWith(color: LyraColors.ink),
        inputDecorationTheme: inputTheme,
        menuStyle: menuStyle,
        disabledColor: LyraColors.mutedSoft,
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: LyraColors.surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        iconColor: LyraColors.accent,
        barrierColor: const Color(0x99000000),
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.all(Radius.circular(20)),
          side: BorderSide(color: LyraColors.hairlineStrong),
        ),
        titleTextStyle: base.textTheme.titleLarge?.copyWith(
          color: LyraColors.ink,
          fontSize: 20,
          fontWeight: FontWeight.w500,
        ),
        contentTextStyle: base.textTheme.bodyMedium?.copyWith(
          color: LyraColors.inkSecondary,
          height: 1.6,
        ),
      ),
      bottomSheetTheme: const BottomSheetThemeData(
        backgroundColor: LyraColors.surface,
        modalBackgroundColor: LyraColors.surface,
        surfaceTintColor: Colors.transparent,
        modalBarrierColor: Color(0x99000000),
        showDragHandle: false,
        dragHandleColor: LyraColors.mutedSoft,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
          side: BorderSide(color: LyraColors.hairlineStrong),
        ),
        clipBehavior: Clip.antiAlias,
      ),
      progressIndicatorTheme: const ProgressIndicatorThemeData(
        color: LyraColors.accent,
        linearTrackColor: LyraColors.surfaceStrong,
        circularTrackColor: LyraColors.surfaceStrong,
        refreshBackgroundColor: LyraColors.surface,
      ),
      tooltipTheme: TooltipThemeData(
        textStyle: const TextStyle(
          fontFamily: fontFamily,
          fontFamilyFallback: _fontFallback,
          color: LyraColors.ink,
          fontSize: 12,
          height: 1.5,
        ),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: LyraColors.surfaceStrong,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: LyraColors.hairlineStrong),
        ),
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: LyraColors.surfaceStrong,
        contentTextStyle: base.textTheme.bodyMedium?.copyWith(
          color: LyraColors.ink,
        ),
        actionTextColor: LyraColors.accent,
        disabledActionTextColor: LyraColors.mutedSoft,
        closeIconColor: LyraColors.inkSecondary,
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: const BorderSide(color: LyraColors.hairlineStrong),
        ),
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
