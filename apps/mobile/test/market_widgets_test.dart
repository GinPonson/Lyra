import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_number.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/ledger/ledger_screen.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';
import 'package:lyra_mobile/features/market/market_widgets.dart';
import 'package:lyra_mobile/features/profile/profile_screen.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'support/ledger_fixture.dart';
import 'support/market_fixture.dart';

class _NewerMarketFixture extends MarketFixture {
  @override
  Map<String, Object?> marketResponse(Map<String, dynamic> query) {
    final body = super.marketResponse(query);
    (body['metadata'] as Map<String, Object?>)['ledgerVersion'] = 13;
    return body;
  }
}

Future<ProviderContainer> _pump(
  WidgetTester tester,
  MarketFixture fixture, {
  Size size = const Size(390, 844),
  double textScale = 1,
  Widget? page,
  AppPreferences? preferences,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dependencies = await fixture.dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          MemoryAppPreferencesStore(
            initialValue: preferences ?? const AppPreferences(),
          ),
        ),
        appVersionProvider.overrideWith((ref) async => '1.0.0 (3)'),
      ],
      child: MaterialApp(
        theme: LyraTheme.dark,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home:
            page ??
            Scaffold(
              body: SingleChildScrollView(
                padding: const EdgeInsets.all(20),
                child: MarketPositionsPanel(
                  ledger: LedgerRead.fromJson(
                    fixture.response('positions', {}),
                  ),
                  initiallyExpanded: true,
                ),
              ),
            ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return ProviderScope.containerOf(tester.element(find.byType(MaterialApp)));
}

String _exposedText(WidgetTester tester) {
  final values = <String>[];
  for (final element in find.byType(Text).evaluate()) {
    final widget = element.widget as Text;
    values.add(widget.data ?? widget.textSpan?.toPlainText() ?? '');
  }
  for (final element in find.byType(RichText).evaluate()) {
    values.add((element.widget as RichText).text.toPlainText());
  }
  for (final element in find.byType(Tooltip).evaluate()) {
    final widget = element.widget as Tooltip;
    values.add(widget.message ?? widget.richMessage?.toPlainText() ?? '');
  }
  void visit(SemanticsNode node) {
    final data = node.getSemanticsData();
    values.addAll([data.label, data.value, data.hint, data.tooltip]);
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
  expect(root, isNotNull);
  visit(root!);
  return values.join('\n');
}

void main() {
  for (final (width, scale) in [(390.0, 1.0), (320.0, 1.6)]) {
    testWidgets('参考行情在 $width 宽、$scale 字号下保留价格口径与独立时点', (tester) async {
      final fixture = MarketFixture();
      await _pump(tester, fixture, size: Size(width, 844), textScale: scale);
      expect(find.text('参考估值'), findsOneWidget);
      expect(find.text('2,326.00'), findsOneWidget);
      expect(find.text('+254.60'), findsOneWidget);
      expect(find.text('参考价 · 买卖中间价'), findsNWidgets(2));
      expect(find.text('未实现盈亏（不含费用）'), findsNWidgets(3));
      expect(find.textContaining('报价时点不同'), findsOneWidget);
      expect(find.textContaining('报价 2026-09-03 20:00 UTC'), findsWidgets);
      expect(find.text('获取 2026-09-08 07:59 UTC'), findsNWidgets(3));
      expect(find.textContaining('NAV'), findsNothing);
      expect(find.textContaining('净值'), findsNothing);
      final request = fixture.requests.singleWhere(
        (request) => request.uri.path == '/v1/market/positions',
      );
      expect(request.queryParameters, {'scope': 'all'});
      expect(request.headers['authorization'], 'Bearer test-only-token');
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('金额隐藏同时撤下合约行权价、买卖价、估值和辅助技术读数', (tester) async {
    final handle = tester.ensureSemantics();
    try {
      final container = await _pump(tester, MarketFixture());
      expect(find.text('2,325.00'), findsOneWidget);
      container.read(amountsHiddenProvider.notifier).setHidden(true);
      await tester.pumpAndSettle();
      final exposed = _exposedText(tester);
      for (final raw in [
        '385.25',
        '202.125',
        '23.25',
        '24.25',
        '23.125',
        '23.375',
        '24.125',
        '24.375',
        '4851.00',
        '2325.00',
        '-4850.00',
        '564.60',
        '460.00',
        '-770.00',
        '2326.00',
        '254.60',
      ]) {
        for (final value in {raw, LyraNumber.amount(raw)}) {
          expect(
            RegExp('(?<![0-9])${RegExp.escape(value)}(?![0-9])')
                .hasMatch(exposed),
            isFalse,
            reason: '$value 不应出现在可读文字或语义中',
          );
        }
      }
      expect(exposed, contains('金额已隐藏'));
      expect(exposed, contains('2026-09-03'));
      expect(find.textContaining(MarketFixture.callSymbol), findsNothing);
      expect(tester.takeException(), isNull);
    } finally {
      handle.dispose();
    }
  });

  testWidgets('已配置但个别报价缺失，不用零金额或完整合计替代缺值', (tester) async {
    await _pump(tester, MarketFixture(unavailable: {MarketFixture.putSymbol}));
    expect(find.text('暂无可用报价，保留历史成本。'), findsOneWidget);
    expect(find.text('2 / 3 项持仓有估值'), findsOneWidget);
    expect(find.text('2,326.00'), findsNothing);
    expect(find.text('0.00'), findsNothing);
    expect(find.text('2,325.00'), findsOneWidget);
    expect(find.textContaining('暂不展示合计'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('stale 且 issue 为空也明确标记过期并隐藏合计', (tester) async {
    await _pump(tester, MarketFixture(stale: {MarketFixture.callSymbol}));
    expect(find.text('包含过期报价，请留意各项报价时间。'), findsOneWidget);
    expect(find.textContaining('日终报价 · 已过期'), findsOneWidget);
    expect(find.textContaining('缓存'), findsNothing);
    expect(find.textContaining('报价 2026-09-03 20:00 UTC'), findsWidgets);
    expect(find.text('2,325.00'), findsOneWidget);
    expect(find.text('2,326.00'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  for (final configured in [false, true]) {
    testWidgets(configured ? '行情请求失败不阻断持仓账本成本' : '行情未开启有通用提示且保留账本成本', (
      tester,
    ) async {
      final fixture = MarketFixture(enabled: configured);
      if (configured) fixture.failures.add('market-positions');
      await _pump(
        tester,
        fixture,
        page: const ReportScreen(view: LedgerView.positions),
      );
      await tester.tap(find.text('AAPL').first);
      await tester.pumpAndSettle();
      expect(find.text('4,286.40'), findsOneWidget);
      expect(
        find.textContaining(configured ? '行情暂不可用' : '行情暂未开启'),
        findsOneWidget,
      );
      expect(find.text('fixture-feed'), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('我的行情入口按需显示状态，无供应商密钥输入项', (tester) async {
    final fixture = MarketFixture(enabled: false);
    await _pump(tester, fixture, page: const Scaffold(body: ProfileScreen()));
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/market/positions',
      ),
      isEmpty,
    );
    final entry = find.byKey(const ValueKey('profile-market-data'));
    await tester.ensureVisible(entry);
    await tester.tap(entry);
    await tester.pumpAndSettle();
    expect(find.textContaining('行情暂未开启'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/market/positions',
      ),
      hasLength(1),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('账户范围变化使用新查询并保留授权客户端', (tester) async {
    final fixture = MarketFixture();
    final container = await _pump(tester, fixture);
    container
        .read(accountScopeProvider.notifier)
        .select(LedgerFixture.account, '长期投资');
    await tester.pumpAndSettle();
    final requests = fixture.requests
        .where((request) => request.uri.path == '/v1/market/positions')
        .toList();
    expect(requests.map((request) => request.queryParameters['scope']), [
      'all',
      LedgerFixture.account,
    ]);
    expect(requests.last.headers['authorization'], 'Bearer test-only-token');
    expect(find.text('2,326.00'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('已恢复的启动账户确定后才请求行情，不先展示全部账户估值', (tester) async {
    const snapshot = CredentialSnapshot(
      serverUrl: 'https://fixture.invalid',
      deviceId: 'test-device',
      accessToken: 'test-only-token',
      refreshToken: 'test-only-refresh',
      authMethod: 'google',
    );
    final fixture = MarketFixture();
    final preferences = const AppPreferences().withIdentity(
      preferencesIdentityKey(snapshot),
      const IdentityAccountPreferences(lastScopeId: LedgerFixture.account),
    );
    await _pump(tester, fixture, preferences: preferences);
    final requests = fixture.requests.where(
      (request) => request.uri.path == '/v1/market/positions',
    );
    expect(requests, hasLength(1));
    expect(requests.single.queryParameters['scope'], LedgerFixture.account);
    expect(find.text('2,326.00'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('估值来自更新的账本版本时要求刷新账本，不混用原持仓', (tester) async {
    await _pump(tester, _NewerMarketFixture());
    expect(find.text('持仓已更新，请刷新账本后查看参考估值。'), findsOneWidget);
    expect(find.text('2,326.00'), findsNothing);
    expect(find.text('4,851.00'), findsNothing);
    expect(tester.takeException(), isNull);
  });
}
