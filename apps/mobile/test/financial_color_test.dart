import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_colors.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/design/lyra_widgets.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/accounts/account_workspace.dart';
import 'package:lyra_mobile/features/ledger/ledger_screen.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';
import 'package:lyra_mobile/features/overview/overview_screen.dart';

import 'support/ledger_fixture.dart';

/// 同一累计曲线跨越零轴，验证两个方向都按偏好着色。
class _CrossingTrendFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command == 'drawdown') {
      final group = (data['groups'] as List).first as Map<String, Object?>;
      final section = group['data'] as Map<String, Object?>;
      section['rows'] = [
        {
          'date': '2026-09-01',
          'dailyPnl': '-8.1250',
          'cumulativePnl': '-8.1250',
          'runningPeak': '0.00',
          'drawdown': '8.1250',
        },
        {
          'date': '2026-09-02',
          'dailyPnl': '20.7500',
          'cumulativePnl': '12.6250',
          'runningPeak': '12.6250',
          'drawdown': '0.00',
        },
      ];
    }
    return data;
  }
}

Future<ProviderContainer> _pump(
  WidgetTester tester,
  Widget body, {
  LedgerFixture? fixture,
}) async {
  final dependencies = await (fixture ?? LedgerFixture()).dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          MemoryAppPreferencesStore(),
        ),
      ],
      child: MaterialApp(
        theme: LyraTheme.dark,
        home: Scaffold(body: body),
      ),
    ),
  );
  final container = ProviderScope.containerOf(
    tester.element(find.byType(MaterialApp)),
  );
  await _waitForFinancialData(tester, container, body);
  await tester.pumpAndSettle();
  return container;
}

/// 等待页面实际读取的 provider，避免 pumpAndSettle 在无帧但请求未完成时提前返回。
Future<void> _waitForFinancialData(
  WidgetTester tester,
  ProviderContainer container,
  Widget body,
) async {
  container.read(appPreferencesProvider);
  await _pumpUntilReady(
    tester,
    () => !container.read(appPreferencesProvider).isLoading,
    '偏好读取',
  );

  final hasTrend = find.byType(LedgerTrendPreview).evaluate().isNotEmpty;
  if (body is! OverviewScreen && !hasTrend) return;

  await _pumpUntilReady(
    tester,
    () => !container.read(sessionProvider).isLoading,
    '会话恢复',
  );
  final session = container.read(sessionProvider).value;
  if (session is! SessionAuthenticated) return;
  // 让认证完成后的页面先重建并订阅页面查询，再观察加载完成状态。
  await tester.pump();
  container.read(accountScopeReadyProvider);
  await _pumpUntilReady(
    tester,
    () => !container.read(accountScopeReadyProvider).isLoading,
    '账户范围恢复',
  );
  await tester.pump();
  final scope = container.read(accountScopeProvider).id;

  if (body is OverviewScreen) {
    container.read(brokerageAccountsProvider);
    await _pumpUntilReady(
      tester,
      () => !container.read(brokerageAccountsProvider).isLoading,
      '证券账户查询',
    );
    await tester.pump();
    container.read(scopedSummaryProvider);
    await _pumpUntilReady(
      tester,
      () => !container.read(scopedSummaryProvider).isLoading,
      '总览汇总查询',
    );
    await tester.pump();
  }

  for (final trend in tester.widgetList<LedgerTrendPreview>(
    find.byType(LedgerTrendPreview),
  )) {
    final view = trend.embedded ? LedgerView.drawdown : LedgerView.monthly;
    final read = ledgerReadProvider((
      path: view.path,
      parameters: '',
      scope: scope,
    ));
    container.read(read);
    await _pumpUntilReady(
      tester,
      () => !container.read(read).isLoading,
      '收益走势查询',
    );
    await tester.pump();
  }
}

/// 以短帧推进测试调度器，直到指定的真实异步依赖完成或明确失败。
Future<void> _pumpUntilReady(
  WidgetTester tester,
  bool Function() ready,
  String dependency,
) async {
  for (var attempt = 0; attempt < 150; attempt++) {
    if (ready()) return;
    await tester.pump(const Duration(milliseconds: 20));
  }
  fail('$dependency在测试等待期限内仍未完成');
}

Color? _textColor(WidgetTester tester, String value) =>
    tester.widget<Text>(find.text(value).first).style?.color;

Future<void> _setScheme(
  WidgetTester tester,
  ProviderContainer container,
  ProfitColorScheme scheme,
) async {
  final change = container
      .read(appPreferencesProvider.notifier)
      .setProfitColorScheme(scheme);
  await tester.pumpAndSettle();
  await change;
}

/// 渲染真实图表 Painter 并读取像素，避免只检查配置字段却漏掉实际绘制颜色。
Future<Uint8List> _plotPixels(WidgetTester tester) async {
  final plot = find.byKey(const ValueKey('ledger-data-plot'));
  final painter = tester
      .widget<CustomPaint>(
        find.descendant(of: plot, matching: find.byType(CustomPaint)),
      )
      .painter!;
  final bytes = await tester.runAsync(() async {
    final recorder = ui.PictureRecorder();
    final canvas = Canvas(recorder);
    painter.paint(canvas, const Size(360, 180));
    final picture = recorder.endRecording();
    final image = await picture.toImage(360, 180);
    final data = await image.toByteData(format: ui.ImageByteFormat.rawRgba);
    final result = Uint8List.fromList(data!.buffer.asUint8List());
    image.dispose();
    picture.dispose();
    return result;
  });
  return bytes!;
}

/// 找绘图区中实际不透明色块，再核对切换后同一像素的颜色和几何位置。
int _solidPixel(Uint8List bytes, Color color) {
  final rgb = color.toARGB32();
  for (int i = 0; i < bytes.length; i += 4) {
    if (bytes[i] == ((rgb >> 16) & 255) &&
        bytes[i + 1] == ((rgb >> 8) & 255) &&
        bytes[i + 2] == (rgb & 255) &&
        bytes[i + 3] == 255) {
      return i;
    }
  }
  fail('图表中没有实际绘制 $color');
}

void _expectPixel(Uint8List bytes, int offset, Color color) {
  final rgb = color.toARGB32();
  expect(bytes.sublist(offset, offset + 4), [
    (rgb >> 16) & 255,
    (rgb >> 8) & 255,
    rgb & 255,
    255,
  ]);
}

void main() {
  test('盈亏方向由原始十进制字符串确定，极小数、大整数和零保持准确语义', () {
    for (final value in ['0.0000000000001', '9007199254740993.995']) {
      expect(
        lyraProfitColor(value, ProfitColorScheme.greenUp),
        LyraColors.positive,
      );
      expect(
        lyraProfitColor(value, ProfitColorScheme.redUp),
        LyraColors.negative,
      );
      expect(
        lyraProfitColor('-$value', ProfitColorScheme.greenUp),
        LyraColors.negative,
      );
      expect(
        lyraProfitColor('-$value', ProfitColorScheme.redUp),
        LyraColors.positive,
      );
    }
    for (final value in ['0', '-0.0000', '—']) {
      for (final scheme in ProfitColorScheme.values) {
        expect(lyraProfitColor(value, scheme), LyraColors.ink);
      }
    }
  });

  testWidgets('金融文字切换红涨绿跌后保留正负号和精度，隐藏时不泄露方向色', (tester) async {
    final container = await _pump(
      tester,
      const Column(
        children: [
          LyraAmount('12.3450', colorBySign: true, showPositiveSign: true),
          LyraAmount('-0.0001', colorBySign: true, showPositiveSign: true),
          LyraAmount('0.0000', colorBySign: true, showPositiveSign: true),
          LyraAmount('0.0001', colorBySign: true, showPositiveSign: true),
          LyraAmount('620.00'),
          ReportLink(view: LedgerView.stress),
        ],
      ),
    );
    expect(_textColor(tester, '+12.35'), LyraColors.positive);
    expect(_textColor(tester, '> −0.01'), LyraColors.negative);
    expect(_textColor(tester, '< 0.01'), LyraColors.positive);
    expect(find.byTooltip('原始值：12.3450'), findsOneWidget);
    expect(find.byTooltip('原始值：-0.0001'), findsOneWidget);
    await _setScheme(tester, container, ProfitColorScheme.redUp);
    expect(_textColor(tester, '+12.35'), LyraColors.negative);
    expect(_textColor(tester, '> −0.01'), LyraColors.positive);
    expect(_textColor(tester, '0.00'), LyraColors.ink);
    expect(_textColor(tester, '620.00'), LyraColors.ink);
    expect(
      tester
          .widget<Icon>(
            find
                .descendant(
                  of: find.byType(ReportLink),
                  matching: find.byType(Icon),
                )
                .first,
          )
          .color,
      LyraColors.accent,
    );
    expect(
      Theme.of(tester.element(find.byType(ReportLink))).colorScheme.primary,
      LyraColors.accent,
    );

    container.read(amountsHiddenProvider.notifier).setHidden(true);
    await tester.pumpAndSettle();
    for (final text in tester.widgetList<Text>(find.text('••••'))) {
      expect(text.style!.color, LyraColors.ink);
    }
    expect(find.byTooltip('原始值：-0.0001'), findsNothing);
    await _setScheme(tester, container, ProfitColorScheme.greenUp);
    expect(find.text('••••'), findsNWidgets(5));
  });

  testWidgets('总览盈亏文字响应设置，深色卡片不受涨跌配色影响', (tester) async {
    final container = await _pump(tester, const OverviewScreen());
    final card = find.byKey(const ValueKey('overview-summary-card'));
    final cardSurface = find
        .descendant(of: card, matching: find.byType(Container))
        .first;
    final before =
        tester.widget<Container>(cardSurface).decoration as BoxDecoration;
    expect(before.gradient!.colors, [
      LyraColors.surface,
      LyraColors.surfaceSubtle,
    ]);
    expect(_textColor(tester, '+2,846.72'), LyraColors.positive);
    await _setScheme(tester, container, ProfitColorScheme.redUp);
    expect(_textColor(tester, '+2,846.72'), LyraColors.negative);
    final after =
        tester.widget<Container>(cardSurface).decoration as BoxDecoration;
    expect(after.color, before.color);
    expect(after.gradient, before.gradient);
    expect(after.border, before.border);
    expect(tester.takeException(), isNull);
  });

  testWidgets('月度正负柱与选中金额实际绘制颜色同步反转', (tester) async {
    final container = await _pump(tester, const LedgerTrendPreview());
    final plot = find.byKey(const ValueKey('ledger-data-plot'));
    final rect = tester.getRect(plot);
    await tester.tapAt(Offset(rect.left + rect.width / 12, rect.center.dy));
    await tester.pumpAndSettle();
    expect(_textColor(tester, '382.48'), LyraColors.positive);
    final positivePixels = await _plotPixels(tester);
    final positiveOffset = _solidPixel(positivePixels, LyraColors.positive);
    await _setScheme(tester, container, ProfitColorScheme.redUp);
    expect(_textColor(tester, '382.48'), LyraColors.negative);
    _expectPixel(
      await _plotPixels(tester),
      positiveOffset,
      LyraColors.negative,
    );

    await tester.tapAt(Offset(rect.left + rect.width / 4, rect.center.dy));
    await tester.pumpAndSettle();
    expect(_textColor(tester, '-148.62'), LyraColors.positive);
    final negativePixels = await _plotPixels(tester);
    final negativeOffset = _solidPixel(negativePixels, LyraColors.positive);
    await _setScheme(tester, container, ProfitColorScheme.greenUp);
    expect(_textColor(tester, '-148.62'), LyraColors.negative);
    _expectPixel(
      await _plotPixels(tester),
      negativeOffset,
      LyraColors.negative,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('跨零轴累计曲线两侧和选中点使用各自盈亏颜色', (tester) async {
    final container = await _pump(
      tester,
      const LedgerTrendPreview(),
      fixture: _CrossingTrendFixture(),
    );
    await tester.tap(find.text('累计'));
    await tester.pump();
    final scope = container.read(accountScopeProvider).id;
    final read = ledgerReadProvider((
      path: LedgerView.drawdown.path,
      parameters: '',
      scope: scope,
    ));
    container.read(read);
    await _pumpUntilReady(
      tester,
      () => !container.read(read).isLoading,
      '累计收益走势查询',
    );
    await tester.pump();
    await tester.pumpAndSettle();
    expect(_textColor(tester, '12.63'), LyraColors.positive);
    expect(find.byTooltip('原始值：12.6250'), findsOneWidget);
    final before = await _plotPixels(tester);
    final positiveOffset = _solidPixel(before, LyraColors.positive);
    final negativeOffset = _solidPixel(before, LyraColors.negative);
    await _setScheme(tester, container, ProfitColorScheme.redUp);
    final after = await _plotPixels(tester);
    _expectPixel(after, positiveOffset, LyraColors.negative);
    _expectPixel(after, negativeOffset, LyraColors.positive);
    expect(_textColor(tester, '12.63'), LyraColors.negative);
    expect(tester.takeException(), isNull);
  });
}
