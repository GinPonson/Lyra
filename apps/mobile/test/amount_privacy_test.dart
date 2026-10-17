import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_number.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/design/lyra_widgets.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/ledger/ledger_screen.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';
import 'package:lyra_mobile/features/overview/overview_screen.dart';

import 'support/ledger_fixture.dart';

class _DelayedPreferencesStore implements AppPreferencesStore {
  final value = Completer<AppPreferences?>();

  @override
  Future<AppPreferences?> read() => value.future;

  @override
  Future<void> write(AppPreferences preferences) async {}
}

class _CountingPreferencesStore extends MemoryAppPreferencesStore {
  _CountingPreferencesStore() : super(initialValue: const AppPreferences());

  int writes = 0;

  @override
  Future<void> write(AppPreferences preferences) async {
    ++writes;
    await super.write(preferences);
  }
}

/// 同一标的股票与期权保留分组，合约代码和说明中也包含可识别的价格。
class _OptionPrivacyFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command == 'positions') {
      final stock = (data['rows'] as List).first as Map<String, Object?>;
      data['rows'] = [
        stock,
        {
          ...stock,
          'symbolKey': 'AAPL-2027-03-19-385.25-C',
          'assetType': 'option',
          'strike': '385.25',
          'expiry': '2027-03-19',
          'optionType': 'C',
          'quantity': '1.000100',
          'cost': '18.6500',
          'description': '开仓价格 18.6500，行权价 385.25',
        },
      ];
    }
    if (command == 'trades') {
      final trade = (data['rows'] as List).first as Map<String, Object?>;
      data['rows'] = [
        {
          ...trade,
          'underlying': null,
          'symbol': 'AAPL270319C00385250',
          'assetType': 'option',
          'strike': '385.25',
          'expiry': '2027-03-19',
          'optionType': 'C',
        },
      ];
    }
    return data;
  }
}

/// 检查用户可读文本、当前 Tooltip 和辅助技术读数，不读取内部原始模型。
String _exposedText(WidgetTester tester) {
  final text = <String>[];
  for (final element in find.byType(Text).evaluate()) {
    final widget = element.widget as Text;
    text.add(widget.data ?? widget.textSpan?.toPlainText() ?? '');
  }
  for (final element in find.byType(RichText).evaluate()) {
    text.add((element.widget as RichText).text.toPlainText());
  }
  for (final element in find.byType(Tooltip).evaluate()) {
    final widget = element.widget as Tooltip;
    text.add(widget.message ?? widget.richMessage?.toPlainText() ?? '');
  }
  void visit(SemanticsNode node) {
    final data = node.getSemanticsData();
    text.addAll([data.label, data.value, data.hint, data.tooltip]);
    node.visitChildren((child) {
      visit(child);
      return true;
    });
  }

  final root = tester
      .binding
      .renderViews
      .single
      .owner
      ?.semanticsOwner
      ?.rootSemanticsNode;
  expect(root, isNotNull, reason: '隐私测试必须同时检查辅助技术树');
  visit(root!);
  return text.join('\n');
}

void _expectPrivate(WidgetTester tester, Iterable<String> amounts) {
  final exposed = _exposedText(tester);
  for (final value in amounts) {
    final rawPattern = RegExp('(?<![0-9])${RegExp.escape(value)}(?![0-9])');
    expect(rawPattern.hasMatch(exposed), isFalse, reason: '原始值 $value 不应被读取');
    expect(
      RegExp('(?<![0-9])${RegExp.escape(LyraNumber.amount(value))}(?![0-9])')
          .hasMatch(exposed),
      isFalse,
      reason: '舍入后的 $value 不应被读取',
    );
  }
}

Future<ProviderContainer> _pumpFinancialPage(
  WidgetTester tester,
  Widget page, {
  LedgerFixture? fixture,
  AppPreferencesStore? store,
  Size size = const Size(390, 844),
  double textScale = 1,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dependencies = await (fixture ?? LedgerFixture()).dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          store ??
              MemoryAppPreferencesStore(
                initialValue: const AppPreferences(hideAmountsOnStartup: true),
              ),
        ),
      ],
      child: MaterialApp(
        theme: LyraTheme.dark,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: page,
      ),
    ),
  );
  await tester.pumpAndSettle();
  return ProviderScope.containerOf(tester.element(find.byType(MaterialApp)));
}

void main() {
  testWidgets('隐藏立即撤销已打开的原始值提示、金额语义和方向色，股数仍可见', (tester) async {
    const raw = '-9007199254740993.995';
    const displayed = '-9,007,199,254,740,994.00 USD';
    final container = await _pumpFinancialPage(
      tester,
      const Scaffold(
        body: Column(
          children: [
            LyraAmount(raw, currency: 'USD', size: 12, colorBySign: true),
            LyraAmount('12345', decimals: 0, isMonetary: false),
            LyraAmount('—'),
          ],
        ),
      ),
      store: MemoryAppPreferencesStore(),
      size: const Size(700, 500),
    );
    expect(find.text(displayed), findsOneWidget);
    await tester.longPress(find.text(displayed));
    await tester.pumpAndSettle();
    expect(find.text('原始值：$raw USD'), findsOneWidget);

    container.read(amountsHiddenProvider.notifier).setHidden(true);
    await tester.pumpAndSettle();
    _expectPrivate(tester, [raw]);
    expect(find.text('•••• USD'), findsOneWidget);
    expect(find.text('12,345'), findsOneWidget);
    expect(find.text('—'), findsOneWidget);
    expect(_exposedText(tester), contains('金额已隐藏，USD'));

    container.read(amountsHiddenProvider.notifier).toggle();
    await tester.pumpAndSettle();
    expect(find.text(displayed), findsOneWidget);
    expect(find.byTooltip('原始值：$raw USD'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('偏好读取完成前首帧不闪现金额，完成后按启动设置显示', (tester) async {
    final store = _DelayedPreferencesStore();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [appPreferencesStoreProvider.overrideWithValue(store)],
        child: const MaterialApp(home: Scaffold(body: LyraAmount('178.6017'))),
      ),
    );
    expect(find.text('••••'), findsOneWidget);
    _expectPrivate(tester, ['178.6017']);
    store.value.complete(const AppPreferences());
    await tester.pumpAndSettle();
    expect(find.text('178.60'), findsOneWidget);
    expect(find.byTooltip('原始值：178.6017'), findsOneWidget);
  });

  testWidgets('320 宽放大字体的总览眼睛按钮只改本次显隐并保留数量与笔数', (tester) async {
    final store = _CountingPreferencesStore();
    final container = await _pumpFinancialPage(
      tester,
      const Scaffold(body: OverviewScreen()),
      store: store,
      size: const Size(320, 740),
      textScale: 1.6,
    );
    expect(find.text('总览'), findsOneWidget);
    expect(find.text('+2,846.72'), findsOneWidget);
    expect(find.byTooltip('隐藏金额').hitTestable(), findsOneWidget);
    expect(tester.takeException(), isNull);
    await tester.tap(find.byKey(const ValueKey('toggle-amounts')));
    await tester.pumpAndSettle();
    expect(container.read(amountsHiddenProvider), isTrue);
    expect(find.byTooltip('显示金额').hitTestable(), findsOneWidget);
    expect(find.text('4'), findsOneWidget);
    expect(find.text('86'), findsOneWidget);
    _expectPrivate(tester, ['2846.72', '24850.00', '6584.16', '4286.40']);
    expect(store.writes, 0);
    expect((await store.read())!.hideAmountsOnStartup, isFalse);
    await tester.tap(find.byKey(const ValueKey('toggle-amounts')));
    await tester.pumpAndSettle();
    expect(find.text('+2,846.72'), findsOneWidget);
    expect(store.writes, 0);
    expect(tester.takeException(), isNull);
  });

  for (final (view, amounts, preserved) in [
    (LedgerView.positions, ['4286.40'], ['24', '2026-05-12']),
    (LedgerView.expiring, ['620.00', '6.20', '200'], ['2', '2026-09-18']),
    (LedgerView.trades, ['-2143.32', '178.60', '0.12'], ['12', '2026-09-04']),
    (
      LedgerView.realized,
      ['196.58', '168.20', '192.80', '0.10', '0.12'],
      ['8', '2026-05-12'],
    ),
    (LedgerView.cashflow, ['126.38', '178.60', '0.12'], ['2026-09-04']),
    (LedgerView.monthly, ['382.48', '700.50', '-120.30', '3.20'], ['60.00']),
    (LedgerView.allocation, ['4286.40', '17306.64'], ['24.77']),
    (LedgerView.attribution, ['2194.80'], ['77.10', '62.50']),
    (LedgerView.drawdown, ['724.30', '51.32'], ['2026-09-01']),
    (LedgerView.risk, ['620.00', '0.00'], ['2', '0']),
    (LedgerView.holdingPeriod, ['412.38', '51.55'], ['18.42', '62.50']),
    (LedgerView.extremes, ['412.38'], ['8', '2026-09-04']),
    (LedgerView.streaks, ['821.36'], ['4', '2026-08-12']),
    (LedgerView.stress, ['620.00'], ['2', '100.00', '2026-09-18']),
  ]) {
    testWidgets('${view.title} 的摘要和展开明细隐藏金额，数量日期比例保持原值', (tester) async {
      await _pumpFinancialPage(tester, ReportScreen(view: view));
      final row = find.byType(ExpansionTile).first;
      await tester.ensureVisible(row);
      await tester.tap(row);
      await tester.pumpAndSettle();
      _expectPrivate(tester, amounts);
      for (final value in preserved) {
        expect(find.text(value), findsWidgets, reason: '$value 应保留原精度');
      }
      expect(find.byKey(const ValueKey('ledger-data-plot')), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('隐藏持仓期权标题、完整代码和说明内价格，重新显示恢复原始精度', (tester) async {
    final container = await _pumpFinancialPage(
      tester,
      const ReportScreen(view: LedgerView.positions),
      fixture: _OptionPrivacyFixture(),
    );
    await tester.tap(find.text('AAPL'));
    await tester.pumpAndSettle();
    final contract = find.text('•••• 看涨');
    expect(contract, findsOneWidget);
    await tester.ensureVisible(contract);
    await tester.tap(contract);
    await tester.pumpAndSettle();
    _expectPrivate(tester, ['385.25', '18.6500']);
    expect(find.text('1.0001 张 · 多头'), findsOneWidget);
    expect(find.text('1.000100'), findsOneWidget);
    expect(find.text('2027-03-19'), findsWidgets);
    expect(find.text('说明已隐藏'), findsOneWidget);

    container.read(amountsHiddenProvider.notifier).setHidden(false);
    await tester.pumpAndSettle();
    expect(find.text('385.25 看涨'), findsOneWidget);
    expect(find.text('18.6500'), findsOneWidget);
    expect(find.text('AAPL-2027-03-19-385.25-C'), findsOneWidget);
    expect(find.text('1.000100'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('缺少独立标的时期权原代码不会从交易标题或头像语义漏出', (tester) async {
    await _pumpFinancialPage(
      tester,
      const ReportScreen(view: LedgerView.trades),
      fixture: _OptionPrivacyFixture(),
    );
    await tester.tap(find.byType(ExpansionTile).first);
    await tester.pumpAndSettle();
    expect(_exposedText(tester), isNot(contains('AAPL270319C00385250')));
    _expectPrivate(tester, ['385.25', '-2143.32', '178.60']);
    expect(find.text('期权合约'), findsOneWidget);
    expect(find.text('12'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('隐藏时收益图没有几何、选中金额或坐标语义，账本检查数仍可读', (tester) async {
    final container = await _pumpFinancialPage(
      tester,
      const Scaffold(
        body: SingleChildScrollView(
          child: Column(
            children: [
              LedgerTrendPreview(),
              LedgerPanel(view: LedgerView.audit, onConnect: _nothing),
            ],
          ),
        ),
      ),
      store: MemoryAppPreferencesStore(),
    );
    expect(find.byKey(const ValueKey('ledger-data-plot')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('ledger-data-plot')));
    await tester.pumpAndSettle();
    container.read(amountsHiddenProvider.notifier).setHidden(true);
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('ledger-data-plot')), findsNothing);
    expect(find.byKey(const ValueKey('ledger-amounts-hidden')), findsOneWidget);
    expect(_exposedText(tester), contains('收益图表已收起'));
    expect(_exposedText(tester), isNot(contains('可点选月份查看金额')));
    _expectPrivate(tester, ['724.30', '489.16', '-148.62']);
    expect(find.text('86'), findsOneWidget);
    expect(find.text('账本检查通过'), findsOneWidget);
    await tester.tap(find.text('累计'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('ledger-data-plot')), findsNothing);
    _expectPrivate(tester, ['724.30', '248.16']);
    expect(tester.takeException(), isNull);
  });
}

void _nothing() {}
