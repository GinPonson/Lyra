import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';
import 'package:lyra_mobile/features/market/market_history_models.dart';
import 'package:lyra_mobile/features/market/market_history_widgets.dart';
import 'package:lyra_mobile/features/market/market_settings.dart';
import 'package:lyra_mobile/features/market/market_workspace.dart'
    show marketSettingsPendingProvider;
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'support/ledger_fixture.dart';
import 'support/market_fixture.dart';
import 'support/market_history_fixture.dart';

/// Returns a fixture holding with the section-level currency included.
Map<String, Object?> _position(MarketHistoryFixture fixture, String symbol) {
  final rows = (fixture.payload('positions', {})['rows']! as List)
      .cast<Map<String, Object?>>();
  return {
    ...rows.singleWhere((row) => row['symbolKey'] == symbol),
    'currency': 'USD',
  };
}

/// Installs the session fixture and a page that opens one history sheet.
Future<ProviderContainer> _pump(
  WidgetTester tester,
  MarketHistoryFixture fixture, {
  String symbol = 'AAPL',
  MarketHistoryTarget target = MarketHistoryTarget.position,
  Map<String, Object?>? positionOverride,
  Size size = const Size(390, 844),
  double textScale = 1,
  bool includeCurrency = true,
  AppPreferences preferences = const AppPreferences(),
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
          MemoryAppPreferencesStore(initialValue: preferences),
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
        home: Scaffold(
          body: Builder(
            builder: (context) => Center(
              child: FilledButton(
                key: const ValueKey('open-market-history'),
                onPressed: () => showMarketHistorySheet(
                  context,
                  position: {
                    ..._position(fixture, symbol),
                    ...?positionOverride,
                    if (!includeCurrency) 'currency': null,
                  },
                  ledgerVersion: 12,
                  originScope: 'all',
                  target: target,
                ),
                child: const Text('打开行情走势'),
              ),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return ProviderScope.containerOf(tester.element(find.byType(MaterialApp)));
}

/// Opens the history sheet through its public ledger-row entry point.
Future<void> _open(WidgetTester tester) async {
  await tester.tap(find.byKey(const ValueKey('open-market-history')));
  await tester.pumpAndSettle();
}

/// Advances async state without waiting for deliberately infinite animations.
Future<void> _pumpFrames(WidgetTester tester, {int count = 20}) async {
  for (var index = 0; index < count; index++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// Filters recorded HTTP calls to the history endpoint.
List<dynamic> _historyRequests(MarketHistoryFixture fixture) => fixture.requests
    .where((request) => request.uri.path == '/v1/market/history')
    .toList();

void main() {
  testWidgets('默认展示股票1月历史，并按账户与设置版本请求', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture);
    await _open(tester);

    expect(find.byKey(const ValueKey('market-history-plot')), findsOneWidget);
    expect(find.text('202.1250 USD'), findsOneWidget);
    expect(
      tester
          .widget<ChoiceChip>(
            find.byKey(const ValueKey('market-history-range-1m')),
          )
          .selected,
      isTrue,
    );
    final request = _historyRequests(fixture).single;
    expect(request.queryParameters, {
      'scope': LedgerFixture.account,
      'symbol_key': 'AAPL',
      'direction': 'long',
      'target': 'position',
      'range': '1m',
      'ledger_version': '12',
      'settings_revision': '7',
    });
    expect(find.textContaining('Alpaca · 延迟至少 16 分钟 · 拆股复权'), findsOneWidget);
    expect(find.text('日收盘价 '), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('切换周期使用对应区间并保留精确期权合约身份', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture, symbol: MarketFixture.callSymbol);
    await _open(tester);
    await tester.tap(find.byKey(const ValueKey('market-history-range-1d')));
    await tester.pumpAndSettle();

    final request = _historyRequests(fixture).last;
    expect(request.queryParameters['symbol_key'], MarketFixture.callSymbol);
    expect(request.queryParameters['direction'], 'long');
    expect(request.queryParameters['range'], '1d');
    expect(find.text('23.2500 USD'), findsOneWidget);
    expect(find.text('15分钟收盘价 '), findsOneWidget);
    expect(find.textContaining('原始合约行情'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('看跌期权历史按自己的 symbol_key 与 short 方向读取', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture, symbol: MarketFixture.putSymbol);
    await _open(tester);

    final request = _historyRequests(fixture).single;
    expect(request.queryParameters['symbol_key'], MarketFixture.putSymbol);
    expect(request.queryParameters['direction'], 'short');
    expect(find.text('24.2500 USD'), findsOneWidget);
    expect(find.textContaining('US:option'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('仅有期权锚点也能看正股走势且不显示合约身份', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(
      tester,
      fixture,
      symbol: MarketFixture.putSymbol,
      target: MarketHistoryTarget.underlying,
      // Treat the source contract as expired; the underlying series stays current.
      positionOverride: {
        'expiry': '2024-06-21',
        'strike': 'not-an-occ-strike',
        'optionType': null,
      },
    );
    await _open(tester);

    final request = _historyRequests(fixture).single;
    expect(request.queryParameters['target'], 'underlying');
    expect(request.queryParameters['symbol_key'], MarketFixture.putSymbol);
    expect(request.queryParameters['direction'], 'short');
    expect(find.text('正股走势'), findsOneWidget);
    expect(find.text('正股 · AAPL'), findsOneWidget);
    expect(find.textContaining('2024-06-21'), findsNothing);
    expect(find.textContaining('385.25'), findsNothing);
    expect(find.text('202.1250 USD'), findsOneWidget);
    expect(find.textContaining('Alpaca · 延迟至少 16 分钟 · 拆股复权'), findsOneWidget);
    expect(find.textContaining('已到期合约历史行情'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('有股票锚点时正股走势继续绑定实际股票行', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture, target: MarketHistoryTarget.underlying);
    await _open(tester);

    final request = _historyRequests(fixture).single;
    expect(request.queryParameters['target'], 'underlying');
    expect(request.queryParameters['symbol_key'], 'AAPL');
    expect(find.text('正股 · AAPL'), findsOneWidget);
    expect(find.text('202.1250 USD'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('股票锚点以 underlying 作为正股标题和序列标识', (tester) async {
    final fixture = MarketHistoryFixture()
      ..stockUnderlyingsBySymbol['broker-stock-id'] = 'AAPL';
    await _pump(
      tester,
      fixture,
      target: MarketHistoryTarget.underlying,
      positionOverride: {'symbolKey': 'broker-stock-id', 'underlying': 'AAPL'},
    );
    await _open(tester);

    final request = _historyRequests(fixture).single;
    expect(request.queryParameters['symbol_key'], 'broker-stock-id');
    expect(find.text('正股 · AAPL'), findsOneWidget);
    expect(find.text('202.1250 USD'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('正股走势的未配置提示按股票来源路由判断', (tester) async {
    final fixture = MarketHistoryFixture()
      ..unavailableIssues[MarketFixture.callSymbol] = 'not_configured'
      ..stockRoutes = [];
    await _pump(
      tester,
      fixture,
      symbol: MarketFixture.callSymbol,
      target: MarketHistoryTarget.underlying,
    );
    await _open(tester);

    expect(find.text('尚未启用行情来源，请在行情设置中选择。'), findsOneWidget);
    expect(
      _historyRequests(fixture).single.queryParameters['target'],
      'underlying',
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('手动刷新先重读来源版本且不写入设置', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture);
    await _open(tester);
    expect(_historyRequests(fixture), hasLength(1));

    fixture.settingsRevision = 8;
    await tester.tap(find.byKey(const ValueKey('market-history-refresh')));
    await tester.pumpAndSettle();

    expect(_historyRequests(fixture), hasLength(2));
    expect(
      _historyRequests(fixture).last.queryParameters['settings_revision'],
      '8',
    );
    expect(
      fixture.requests
          .where((request) => request.uri.path == '/v1/market/settings')
          .map((request) => request.method),
      everyElement('GET'),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('来源保存进行中禁用历史刷新', (tester) async {
    final fixture = MarketHistoryFixture();
    final container = await _pump(tester, fixture);
    await _open(tester);
    container
        .read(marketSettingsPendingProvider.notifier)
        .begin(MarketRoutes(stock: ['alpaca'], option: ['alpaca']));
    await tester.pump();

    expect(
      tester
          .widget<IconButton>(
            find.byKey(const ValueKey('market-history-refresh')),
          )
          .onPressed,
      isNull,
    );
    expect(_historyRequests(fixture), hasLength(1));
    container.read(marketSettingsPendingProvider.notifier).finish();
  });

  testWidgets('单根 bar 保持为单点且时间轴可访问', (tester) async {
    final fixture = MarketHistoryFixture();
    fixture.barsBySymbol['AAPL'] = [
      {
        'timestamp': '2026-09-04T19:30:00.000Z',
        'open': '202.0000',
        'high': '202.1250',
        'low': '201.8750',
        'close': '202.062500',
        'volume': 1,
      },
    ];
    await _pump(tester, fixture);
    await _open(tester);

    expect(find.byKey(const ValueKey('market-history-plot')), findsOneWidget);
    expect(find.text('202.062500 USD'), findsOneWidget);
    expect(find.textContaining('区间开始 2026-09-04 UTC'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('触摸图表选择实际 bar 时间和原精度收盘价', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture);
    await _open(tester);
    final plot = find.byKey(const ValueKey('market-history-plot'));
    final rect = tester.getRect(plot);
    await tester.tapAt(Offset(rect.left + 58, rect.center.dy));
    await tester.pump();

    expect(find.text('202.0000 USD'), findsOneWidget);
    expect(find.textContaining('区间开始 2026-08-20 UTC'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('隐藏金额时不暴露图形、精确值或辅助语义，并可在 sheet 内恢复', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(
      tester,
      fixture,
      preferences: const AppPreferences(hideAmountsOnStartup: true),
    );
    await _open(tester);

    expect(find.byKey(const ValueKey('market-history-plot')), findsNothing);
    expect(find.textContaining('202.1250'), findsNothing);
    expect(find.byKey(const ValueKey('toggle-amounts')), findsOneWidget);
    expect(find.text('显示金额后查看行情走势'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('toggle-amounts')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('market-history-plot')), findsOneWidget);
    expect(find.text('202.1250 USD'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('history_not_supported 显示为来源能力状态', (tester) async {
    final unsupported = MarketHistoryFixture();
    unsupported.unavailableIssues['AAPL'] = 'history_not_supported';
    await _pump(tester, unsupported);
    await _open(tester);
    expect(find.text('所选来源暂不支持历史行情'), findsOneWidget);
    expect(find.byKey(const ValueKey('market-history-plot')), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('no_data 与来源能力受限的说明不同', (tester) async {
    final noData = MarketHistoryFixture();
    noData.unavailableIssues['AAPL'] = 'no_data';
    await _pump(tester, noData);
    await _open(tester);
    expect(find.text('暂无历史行情'), findsOneWidget);
    expect(find.text('所选周期没有可用区间数据。'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('已配置可选来源但未设置路由时提示尚未启用', (tester) async {
    final notEnabled = MarketHistoryFixture();
    notEnabled.unavailableIssues['AAPL'] = 'not_configured';
    notEnabled.stockRoutes = [];
    await _pump(tester, notEnabled);
    await _open(tester);
    expect(find.text('尚未启用行情来源，请在行情设置中选择。'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('选中的来源未配置时说明服务端仍需配置', (tester) async {
    final notConfigured = MarketHistoryFixture();
    notConfigured.unavailableIssues['AAPL'] = 'not_configured';
    notConfigured.alpacaConfigured = false;
    notConfigured.stockRoutes = ['alpaca'];
    await _pump(tester, notConfigured);
    await _open(tester);
    expect(find.text('当前选中的行情来源尚未完成服务端配置。'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('缺失币种时明确停止且不调用历史行情接口', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture, includeCurrency: false);
    await tester.tap(find.byKey(const ValueKey('open-market-history')));
    await tester.pumpAndSettle();

    expect(find.text('持仓币种不可用，无法安全匹配历史行情。'), findsOneWidget);
    expect(_historyRequests(fixture), isEmpty);
    expect(tester.takeException(), isNull);
  });

  testWidgets('切周期后迟到的旧响应不会替换当前周期', (tester) async {
    final fixture = MarketHistoryFixture();
    final firstStarted = Completer<void>();
    final releaseFirst = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path == '/v1/market/history' &&
          options.queryParameters['range'] == '1m') {
        if (!firstStarted.isCompleted) firstStarted.complete();
        await releaseFirst.future;
      }
    };
    await _pump(tester, fixture);
    await tester.tap(find.byKey(const ValueKey('open-market-history')));
    await tester.pump();
    await _pumpFrames(tester);
    expect(firstStarted.isCompleted, isTrue);
    await tester.tap(find.byKey(const ValueKey('market-history-range-1d')));
    await _pumpFrames(tester);
    expect(find.text('204.1250 USD'), findsOneWidget);

    releaseFirst.complete();
    await _pumpFrames(tester);
    expect(find.text('204.1250 USD'), findsOneWidget);
    expect(_historyRequests(fixture).map((r) => r.queryParameters['range']), [
      '1m',
      '1d',
    ]);
    expect(tester.takeException(), isNull);
  });

  testWidgets('账户范围变化后丢弃迟到的历史响应', (tester) async {
    final fixture = MarketHistoryFixture();
    final requestStarted = Completer<void>();
    final releaseResponse = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path == '/v1/market/history') {
        if (!requestStarted.isCompleted) requestStarted.complete();
        await releaseResponse.future;
      }
    };
    final container = await _pump(tester, fixture);
    await tester.tap(find.byKey(const ValueKey('open-market-history')));
    await tester.pump();
    await _pumpFrames(tester);
    expect(requestStarted.isCompleted, isTrue);

    container
        .read(accountScopeProvider.notifier)
        .select(LedgerFixture.account, '长期投资');
    await _pumpFrames(tester);
    expect(find.text('账户查看范围已变化，请关闭并重新打开行情走势。'), findsOneWidget);
    expect(find.text('202.1250 USD'), findsNothing);

    releaseResponse.complete();
    await _pumpFrames(tester);
    expect(find.text('202.1250 USD'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('窄屏大字仍能滚动阅读且不溢出', (tester) async {
    final fixture = MarketHistoryFixture();
    await _pump(tester, fixture, size: const Size(320, 740), textScale: 1.6);
    await _open(tester);

    expect(find.byKey(const ValueKey('market-history-plot')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
