import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';
import 'package:lyra_mobile/features/market/market_settings.dart';
import 'package:lyra_mobile/features/market/market_underlying_quote_widgets.dart';
import 'package:lyra_mobile/features/market/market_workspace.dart'
    show marketSettingsPendingProvider, marketSettingsRevisionProvider;
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'support/ledger_fixture.dart';
import 'support/market_fixture.dart';
import 'support/market_underlying_quote_fixture.dart';

/// Builds a real source position without manufacturing a stock row or OCC fields.
Map<String, Object?> _position({
  String symbolKey = MarketFixture.callSymbol,
  String assetType = 'option',
  String direction = 'long',
  String underlying = 'AAPL',
  String currency = 'USD',
}) => {
  'accountId': LedgerFixture.account,
  'symbolKey': symbolKey,
  'assetType': assetType,
  'direction': direction,
  'underlying': underlying,
  'currency': currency,
};

/// Mounts the preview with the shared session, ledger and market fixtures.
Future<ProviderContainer> _mount(
  WidgetTester tester,
  MarketUnderlyingQuoteFixture fixture, {
  Map<String, Object?>? position,
  Size size = const Size(390, 844),
  double textScale = 1,
  AppPreferences preferences = const AppPreferences(),
  VoidCallback? onOpenHistory,
}) async {
  fixture.positionAssetTypeOverride =
      (position ?? _position())['assetType'] as String;
  fixture.instrumentTicker = ((position ?? _position())['underlying'] as String)
      .toUpperCase();
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
          body: SingleChildScrollView(
            child: MarketUnderlyingQuotePreview(
              position: Map.unmodifiable(position ?? _position()),
              ledgerVersion: 12,
              originScope: 'all',
              onOpenHistory: onOpenHistory ?? () {},
            ),
          ),
        ),
      ),
    ),
  );
  return ProviderScope.containerOf(tester.element(find.byType(MaterialApp)));
}

/// Advances provider and test-adapter futures without requiring animated indicators to settle.
Future<void> _pumpFrames(WidgetTester tester, {int count = 30}) async {
  for (var index = 0; index < count; index++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// Filters the deterministic requests to the stock-summary endpoint.
List<dynamic> _quoteRequests(MarketUnderlyingQuoteFixture fixture) => fixture
    .requests
    .where((request) => request.uri.path == '/v1/market/underlying-quote')
    .toList();

void main() {
  testWidgets('仅有期权持仓也读取正股摘要并保留原始行身份', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    var historyOpened = false;
    await _mount(
      tester,
      fixture,
      position: _position(symbolKey: 'opaque-option-row'),
      onOpenHistory: () => historyOpened = true,
    );
    await _pumpFrames(tester);

    expect(find.text('正股'), findsOneWidget);
    expect(find.text('205.1250 USD'), findsOneWidget);
    expect(find.text('较前收'), findsOneWidget);
    expect(find.text('+2.56 %'), findsOneWidget);
    expect(find.text('延迟至少 15 分钟'), findsOneWidget);
    expect(find.byTooltip('前收交易日 2026-09-03'), findsOneWidget);
    final request = _quoteRequests(fixture).single;
    expect(request.queryParameters, {
      'scope': LedgerFixture.account,
      'symbol_key': 'opaque-option-row',
      'direction': 'long',
      'ledger_version': '12',
      'settings_revision': '7',
    });
    expect(request.queryParameters.containsKey('target'), isFalse);
    expect(request.queryParameters.containsKey('range'), isFalse);
    await tester.tap(find.byKey(const ValueKey('market-underlying-history')));
    expect(historyOpened, isTrue);
    expect(tester.takeException(), isNull);
  });

  testWidgets('股票锚点使用 response 实际延迟值显示', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    await _mount(
      tester,
      fixture,
      position: _position(symbolKey: 'broker-stock-row', assetType: 'stock'),
    );
    await _pumpFrames(tester);

    expect(
      _quoteRequests(fixture).single.queryParameters['symbol_key'],
      'broker-stock-row',
    );
    expect(find.text('205.1250 USD'), findsOneWidget);
    expect(find.text('延迟至少 15 分钟'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('前收不可用时显示正股价格和原因，不伪造涨跌幅', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture()..previousCloseValue = null;
    await _mount(tester, fixture);
    await _pumpFrames(tester);

    expect(find.text('205.1250 USD'), findsOneWidget);
    expect(find.text('前收暂不可用'), findsOneWidget);
    expect(find.text('较前收'), findsNothing);
    expect(find.textContaining('%'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('已知前收错误码映射为简短中文', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture()
      ..previousCloseValue = null
      ..changeIssueValue = 'rate_limited';
    await _mount(tester, fixture);
    await _pumpFrames(tester);

    expect(find.text('涨跌幅读取受限，请稍后重试'), findsOneWidget);
    expect(find.text('rate_limited'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('金额隐藏时不构建精确金额或涨跌幅，也保留走势入口', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    await _mount(
      tester,
      fixture,
      preferences: const AppPreferences(hideAmountsOnStartup: true),
    );
    await _pumpFrames(tester);

    expect(find.text('金额已隐藏'), findsOneWidget);
    expect(find.textContaining('205.1250'), findsNothing);
    expect(find.textContaining('2.5625'), findsNothing);
    expect(find.byTooltip('原始值：205.1250 USD'), findsNothing);
    expect(find.byTooltip('原始值：2.5625 %'), findsNothing);
    expect(
      find.byKey(const ValueKey('market-underlying-history')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('未启用来源和服务端未配置状态分别说明', (tester) async {
    final disabled = MarketUnderlyingQuoteFixture()
      ..issue = 'not_configured'
      ..stockRoutes = [];
    await _mount(tester, disabled);
    await _pumpFrames(tester);
    expect(find.text('尚未启用正股行情来源，请在行情设置中选择。'), findsOneWidget);
    expect(find.text('205.1250 USD'), findsNothing);

    final needsSetup = MarketUnderlyingQuoteFixture()
      ..issue = 'not_configured'
      ..alpacaConfigured = false;
    await _mount(tester, needsSetup);
    await _pumpFrames(tester);
    expect(find.text('当前正股行情来源尚未完成服务端配置。'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('接口失败后点击刷新可重试并恢复数据', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture()
      ..failOnce.add('underlying-quote:first');
    await _mount(tester, fixture);
    await _pumpFrames(tester);

    expect(find.text('正股行情读取失败，请重试。'), findsOneWidget);
    await tester.tap(
      find.byKey(const ValueKey('market-underlying-quote-refresh')),
    );
    await _pumpFrames(tester);

    expect(_quoteRequests(fixture), hasLength(2));
    expect(find.text('205.1250 USD'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('来源保存进行中禁用刷新入口', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    final container = await _mount(tester, fixture);
    await _pumpFrames(tester);
    container
        .read(marketSettingsPendingProvider.notifier)
        .begin(MarketRoutes(stock: ['alpaca'], option: ['alpaca']));
    await tester.pump();

    expect(
      tester
          .widget<IconButton>(
            find.byKey(const ValueKey('market-underlying-quote-refresh')),
          )
          .onPressed,
      isNull,
    );
    expect(_quoteRequests(fixture), hasLength(1));
  });

  testWidgets('账户范围改变后丢弃迟到的正股报价', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    final started = Completer<void>();
    final release = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path == '/v1/market/underlying-quote') {
        if (!started.isCompleted) started.complete();
        await release.future;
      }
    };
    final container = await _mount(tester, fixture);
    await _pumpFrames(tester);
    expect(started.isCompleted, isTrue);

    container
        .read(accountScopeProvider.notifier)
        .select(LedgerFixture.account, '长期投资');
    await _pumpFrames(tester);
    expect(find.text('205.1250 USD'), findsNothing);

    release.complete();
    await _pumpFrames(tester);
    expect(find.text('205.1250 USD'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('设置版本改变后丢弃迟到的正股报价', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    final started = Completer<void>();
    final release = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path == '/v1/market/underlying-quote') {
        if (!started.isCompleted) started.complete();
        await release.future;
      }
    };
    final container = await _mount(tester, fixture);
    await _pumpFrames(tester);
    expect(started.isCompleted, isTrue);

    container.read(marketSettingsRevisionProvider.notifier).observe(8);
    await _pumpFrames(tester);
    expect(find.text('行情来源正在变化，请稍后刷新。'), findsOneWidget);
    release.complete();
    await _pumpFrames(tester);
    expect(find.text('205.1250 USD'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('账本版本改变后丢弃迟到的正股报价', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    final started = Completer<void>();
    final release = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path == '/v1/market/underlying-quote') {
        if (!started.isCompleted) started.complete();
        await release.future;
      }
    };
    final container = await _mount(tester, fixture);
    await _pumpFrames(tester);
    expect(started.isCompleted, isTrue);

    fixture.positionLedgerVersionOverride = 13;
    container.invalidate(
      ledgerReadProvider((
        path: LedgerView.positions.path,
        parameters: '',
        scope: 'all',
      )),
    );
    await _pumpFrames(tester);
    expect(find.text('持仓已变化，请刷新账本后重试。'), findsOneWidget);
    release.complete();
    await _pumpFrames(tester);
    expect(find.text('205.1250 USD'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('会话退出后不显示迟到的正股报价', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    final started = Completer<void>();
    final release = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path == '/v1/market/underlying-quote') {
        if (!started.isCompleted) started.complete();
        await release.future;
      }
    };
    final container = await _mount(tester, fixture);
    await _pumpFrames(tester);
    expect(started.isCompleted, isTrue);

    final signOut = container.read(sessionProvider.notifier).signOut();
    unawaited(signOut.catchError((Object _) {}));
    await _pumpFrames(tester);
    release.complete();
    await _pumpFrames(tester);
    expect(find.text('205.1250 USD'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('窄屏大字号下操作可换行且不溢出', (tester) async {
    final fixture = MarketUnderlyingQuoteFixture();
    await _mount(tester, fixture, size: const Size(320, 740), textScale: 1.6);
    await _pumpFrames(tester);

    expect(find.text('205.1250 USD'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('market-underlying-history')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });
}
