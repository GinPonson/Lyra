import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';
import 'package:lyra_mobile/features/market/market_settings.dart';
import 'package:lyra_mobile/features/market/market_settings_widgets.dart';
import 'package:lyra_mobile/features/market/market_widgets.dart';
import 'package:lyra_mobile/features/market/market_workspace.dart';
import 'package:lyra_mobile/features/profile/profile_screen.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'support/market_settings_fixture.dart';

Future<ProviderContainer> _pump(
  WidgetTester tester,
  MarketSettingsFixture fixture, {
  Widget? page,
  Size size = const Size(390, 844),
  double scale = 1,
  bool paired = true,
  bool includePositions = false,
  bool settle = true,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  final dependencies = await fixture.dependencies(paired: paired);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          MemoryAppPreferencesStore(),
        ),
        appVersionProvider.overrideWith((ref) async => '1.0.0 (3)'),
      ],
      child: MaterialApp(
        theme: LyraTheme.dark,
        home:
            page ??
            Scaffold(
              body: SingleChildScrollView(
                padding: const EdgeInsets.all(20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const MarketSettingsPanel(),
                    if (includePositions)
                      MarketPositionsPanel(
                        ledger: LedgerRead.fromJson(
                          fixture.response('positions', {}),
                        ),
                        initiallyExpanded: true,
                      ),
                  ],
                ),
              ),
            ),
      ),
    ),
  );
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    for (var count = 0; count < 12; count++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
  }
  return ProviderScope.containerOf(tester.element(find.byType(MaterialApp)));
}

Finder _selector(String asset, {bool backup = false}) => find.byWidgetPredicate(
  (widget) =>
      widget is DropdownButtonFormField<String> &&
      widget.key is ValueKey<String> &&
      (widget.key! as ValueKey<String>).value.startsWith(
        'market-source-$asset-${backup ? 'backup' : 'primary'}-',
      ),
);

DropdownButtonFormField<String> _field(
  WidgetTester tester,
  String asset, {
  bool backup = false,
}) => tester.widget<DropdownButtonFormField<String>>(
  _selector(asset, backup: backup),
);

DropdownButton<String> _button(
  WidgetTester tester,
  String asset, {
  bool backup = false,
}) => tester.widget<DropdownButton<String>>(
  find.descendant(
    of: _selector(asset, backup: backup),
    matching: find.byWidgetPredicate(
      (widget) => widget is DropdownButton<String>,
    ),
  ),
);

Future<void> _select(
  WidgetTester tester,
  String asset,
  String id, {
  bool backup = false,
}) async {
  final selector = _selector(asset, backup: backup);
  await tester.ensureVisible(selector);
  await tester.tap(selector);
  await tester.pumpAndSettle();
  final item = find.byWidgetPredicate(
    (widget) => widget is DropdownMenuItem<String> && widget.value == id,
  );
  expect(item, findsOneWidget);
  final text = find.descendant(of: item, matching: find.byType(Text));
  await tester.ensureVisible(text);
  await tester.tap(text);
  await tester.pumpAndSettle();
  expect(tester.takeException(), isNull);
}

Future<void> _save(WidgetTester tester) async {
  final button = find.byKey(const ValueKey('market-settings-save'));
  await tester.ensureVisible(button);
  await tester.tap(button);
  await tester.pumpAndSettle();
}

Future<void> _open(WidgetTester tester) async {
  final entry = find.byKey(const ValueKey('profile-market-data'));
  await tester.ensureVisible(entry);
  await tester.tap(entry);
  await tester.pumpAndSettle();
}

void main() {
  test('目录与主备来源保持独立，不将未知或未配置来源自动替换', () {
    final settings = MarketSettings.fromJson(
      MarketSettingsFixture.settingsPayload(
        stock: ['removed-feed'],
        option: ['second-feed'],
        secondConfigured: false,
      ),
    );
    expect(settings.routes.stock, ['removed-feed']);
    expect(settings.source('removed-feed'), isNull);
    expect(settings.canSelect('stock', 'first-feed'), isTrue);
    expect(settings.canSelect('option', 'second-feed'), isFalse);
    expect(settings.accepts(settings.routes), isFalse);
    expect(settings.providers.first.timingLabel, '延迟 16 分钟');
  });

  test('来源契约拒绝重复与过长路由，关闭保留空列表', () {
    expect(
      () => MarketRoutes(stock: ['a', 'a'], option: []),
      throwsFormatException,
    );
    expect(
      () => MarketRoutes(stock: ['a', 'b', 'c'], option: []),
      throwsFormatException,
    );
    final routes = MarketRoutes(stock: ['a', 'b'], option: []);
    expect(routes.replace('stock', []), MarketRoutes(stock: [], option: []));
    expect(routes.toJson(), {
      'stock': ['a', 'b'],
      'option': [],
    });
  });

  testWidgets('我的入口读取动态目录，分别保存主备源并在重新打开后还原', (tester) async {
    final fixture = MarketSettingsFixture();
    final container = await _pump(
      tester,
      fixture,
      page: const Scaffold(body: ProfileScreen()),
    );
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/market/settings',
      ),
      isEmpty,
    );
    await _open(tester);
    final ledgerReads = fixture.requests
        .where((request) => request.uri.path == '/v1/ledger/positions')
        .length;
    await _select(tester, 'stock', 'second-feed');
    await _select(tester, 'stock', 'first-feed', backup: true);
    await _select(tester, 'option', 'first-feed');
    await _save(tester);
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
    final put = fixture.requests.singleWhere(
      (request) => request.method == 'PUT',
    );
    expect(put.uri.path, '/v1/market/settings');
    expect(put.headers['authorization'], 'Bearer test-only-token');
    expect(put.data, {
      'expectedRevision': 0,
      'routes': {
        'stock': ['second-feed', 'first-feed'],
        'option': ['first-feed'],
      },
    });
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/ledger/positions',
      ),
      hasLength(ledgerReads),
    );
    expect(container.read(marketSettingsRevisionProvider), 1);
    expect(find.byType(TextField), findsNothing);
    await tester.ensureVisible(find.byTooltip('关闭'));
    await tester.tap(find.byTooltip('关闭'));
    await tester.pumpAndSettle();
    await _open(tester);
    expect(_field(tester, 'stock').initialValue, 'second-feed');
    expect(_field(tester, 'stock', backup: true).initialValue, 'first-feed');
    expect(_field(tester, 'option').initialValue, 'first-feed');
    expect(
      fixture.requests.where(
        (request) =>
            request.uri.path == '/v1/market/settings' &&
            request.method == 'GET',
      ),
      hasLength(2),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('未配置来源可见但禁用，资产不支持的来源不进入候选', (tester) async {
    final settings = MarketSettingsFixture.settingsPayload(
      option: [],
      secondConfigured: false,
    );
    (settings['providers']! as List).add({
      'id': 'stocks-only',
      'displayName': 'Stocks Only',
      'assetTypes': ['stock'],
      'configured': true,
      'timing': {'kind': 'delayed', 'delayMinutes': 20},
      'cacheTtlSeconds': 900,
    });
    final fixture = MarketSettingsFixture(initialSettings: settings);
    await _pump(tester, fixture);
    expect(
      _button(
        tester,
        'stock',
      ).items!.firstWhere((item) => item.value == 'second-feed').enabled,
      isFalse,
    );
    expect(
      _button(
        tester,
        'option',
      ).items!.any((item) => item.value == 'stocks-only'),
      isFalse,
    );
    final selector = _selector('stock');
    await tester.ensureVisible(selector);
    await tester.tap(selector);
    await tester.pumpAndSettle();
    expect(find.text('Second Data · 未配置'), findsWidgets);
    final item = find.byWidgetPredicate(
      (widget) =>
          widget is DropdownMenuItem<String> && widget.value == 'second-feed',
    );
    await tester.tap(item);
    await tester.pumpAndSettle();
    expect(_field(tester, 'stock').initialValue, 'first-feed');
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();
    expect(
      fixture.requests.where((request) => request.method == 'PUT'),
      isEmpty,
    );
  });

  testWidgets('主备不重复，取消还原草稿，关闭主来源同时清空备用', (tester) async {
    final fixture = MarketSettingsFixture(
      initialSettings: MarketSettingsFixture.settingsPayload(
        stock: ['first-feed', 'second-feed'],
      ),
    );
    await _pump(tester, fixture);
    expect(
      _button(
        tester,
        'stock',
        backup: true,
      ).items!.any((item) => item.value == 'first-feed'),
      isFalse,
    );
    await _select(tester, 'stock', 'second-feed');
    expect(_field(tester, 'stock', backup: true).initialValue, '');
    final cancel = find.byKey(const ValueKey('market-settings-cancel'));
    await tester.ensureVisible(cancel);
    await tester.tap(cancel);
    await tester.pumpAndSettle();
    expect(_field(tester, 'stock').initialValue, 'first-feed');
    expect(_field(tester, 'stock', backup: true).initialValue, 'second-feed');
    expect(
      fixture.requests.where((request) => request.method == 'PUT'),
      isEmpty,
    );
    await _select(tester, 'stock', '');
    await _select(tester, 'option', '');
    expect(_field(tester, 'stock', backup: true).initialValue, '');
    expect(_field(tester, 'stock', backup: true).onChanged, isNull);
    await _save(tester);
    expect(fixture.settings['routes'], {'stock': [], 'option': []});
  });

  testWidgets('失效的已保存来源保留显示，必须主动改选才能保存', (tester) async {
    final settings = MarketSettingsFixture.settingsPayload(
      stock: ['removed-feed'],
    );
    settings['effectiveRoutes'] = {
      'stock': [],
      'option': ['second-feed'],
    };
    final fixture = MarketSettingsFixture(initialSettings: settings);
    await _pump(tester, fixture);
    expect(_field(tester, 'stock').initialValue, 'removed-feed');
    expect(find.text('原来源已不可用'), findsWidgets);
    expect(
      tester
          .widget<FilledButton>(
            find.byKey(const ValueKey('market-settings-save')),
          )
          .onPressed,
      isNull,
    );
    await _select(tester, 'stock', 'first-feed');
    await _save(tester);
    expect((fixture.settings['routes']! as Map)['stock'], ['first-feed']);
    expect(tester.takeException(), isNull);
  });

  testWidgets('来源读取失败可以重试，不依赖持仓读取成功', (tester) async {
    final fixture = MarketSettingsFixture()..failRead = true;
    fixture.failures.add('positions');
    await _pump(tester, fixture, page: const Scaffold(body: ProfileScreen()));
    await _open(tester);
    expect(find.textContaining('暂时无法读取行情来源'), findsOneWidget);
    expect(find.textContaining('暂时无法读取持仓'), findsOneWidget);
    fixture.failRead = false;
    await tester.tap(find.byKey(const ValueKey('market-settings-retry')));
    await tester.pumpAndSettle();
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
  });

  testWidgets('保存失败保留选择与旧报价状态，重试成功后才推进设置版本', (tester) async {
    final fixture = MarketSettingsFixture()
      ..saveFailure = (code: 'QUERY_FAILED', status: 503);
    final container = await _pump(tester, fixture);
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    expect(find.textContaining('保存失败，选择已保留'), findsOneWidget);
    expect(find.text('已保存，新的行情来源已生效。'), findsNothing);
    expect(_field(tester, 'stock').initialValue, 'second-feed');
    expect(container.read(marketSettingsRevisionProvider), 0);
    expect((fixture.settings['routes']! as Map)['stock'], ['first-feed']);
    fixture.saveFailure = null;
    await _save(tester);
    expect(container.read(marketSettingsRevisionProvider), 1);
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
  });

  testWidgets('旧服务端没有设置接口时明确提示更新版本', (tester) async {
    final fixture = MarketSettingsFixture()
      ..failRead = true
      ..readFailureStatus = 404;
    await _pump(tester, fixture);
    expect(find.text('服务端版本较旧，请更新服务端后使用界面切源。'), findsOneWidget);
    expect(find.textContaining('请检查服务端连接'), findsNothing);
  });

  testWidgets('409 保留草稿并要求重新加载，不自动覆盖另一设备选择', (tester) async {
    final fixture = MarketSettingsFixture();
    await _pump(tester, fixture);
    await _select(tester, 'stock', 'second-feed');
    fixture.settings = MarketSettingsFixture.settingsPayload(
      revision: 1,
      stock: [],
      option: ['first-feed'],
    );
    await _save(tester);
    expect(find.textContaining('其他设备已修改行情来源'), findsOneWidget);
    expect(_field(tester, 'stock').initialValue, 'second-feed');
    expect(
      fixture.requests.where((request) => request.method == 'PUT'),
      hasLength(1),
    );
    await tester.ensureVisible(
      find.byKey(const ValueKey('market-settings-reload')),
    );
    await tester.tap(find.byKey(const ValueKey('market-settings-reload')));
    await tester.pumpAndSettle();
    expect(_field(tester, 'stock').initialValue, '');
    expect(_field(tester, 'option').initialValue, 'first-feed');
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    final put = fixture.requests.lastWhere(
      (request) => request.method == 'PUT',
    );
    expect((put.data as Map)['expectedRevision'], 1);
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
  });

  testWidgets('缺少凭据错误保留草稿，并可重新加载最新配置状态', (tester) async {
    final fixture = MarketSettingsFixture()
      ..saveFailure = (code: 'MARKET_PROVIDER_NOT_CONFIGURED', status: 400);
    await _pump(tester, fixture);
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    expect(find.textContaining('所选来源尚未配置'), findsOneWidget);
    expect(_field(tester, 'stock').initialValue, 'second-feed');
    fixture.settings = MarketSettingsFixture.settingsPayload(
      option: [],
      secondConfigured: false,
    );
    await tester.ensureVisible(
      find.byKey(const ValueKey('market-settings-reload')),
    );
    await tester.tap(find.byKey(const ValueKey('market-settings-reload')));
    await tester.pumpAndSettle();
    expect(
      _button(
        tester,
        'stock',
      ).items!.firstWhere((item) => item.value == 'second-feed').enabled,
      isFalse,
    );
  });

  testWidgets('保存等待期间禁止重复提交、取消或变更选择', (tester) async {
    final gate = Completer<void>();
    final fixture = MarketSettingsFixture()..saveResponseGate = gate;
    await _pump(tester, fixture);
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    expect(find.text('保存中…'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(
            find.byKey(const ValueKey('market-settings-save')),
          )
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(
            find.byKey(const ValueKey('market-settings-cancel')),
          )
          .onPressed,
      isNull,
    );
    expect(_field(tester, 'stock').onChanged, isNull);
    expect(
      fixture.requests.where((request) => request.method == 'PUT'),
      hasLength(1),
    );
    gate.complete();
    await tester.pumpAndSettle();
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
  });

  testWidgets('同会话刷新账本不丢失保存中的草稿与成功结果', (tester) async {
    final gate = Completer<void>();
    final fixture = MarketSettingsFixture()..saveResponseGate = gate;
    final container = await _pump(tester, fixture);
    final client = container.read(marketAuthenticatedClientProvider);
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    final refreshing = container.read(sessionProvider.notifier).refreshLedger();
    await tester.pumpAndSettle();
    expect(await refreshing, isTrue);
    expect(
      identical(client, container.read(marketAuthenticatedClientProvider)),
      isTrue,
    );
    expect(find.text('保存中…'), findsOneWidget);
    expect(_field(tester, 'stock').initialValue, 'second-feed');
    expect(
      fixture.requests.where(
        (request) =>
            request.uri.path == '/v1/market/settings' &&
            request.method == 'GET',
      ),
      hasLength(1),
    );
    gate.complete();
    await tester.pumpAndSettle();
    expect(container.read(marketSettingsRevisionProvider), 1);
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
  });

  testWidgets('保存后刷新所有行情查询，旧来源迟到响应不能覆盖新版本', (tester) async {
    final fixture = MarketSettingsFixture();
    final container = await _pump(tester, fixture);
    final gate = Completer<void>();
    fixture.nextPositionGate = gate;
    const allQuery = (scope: 'all', underlying: null, ledgerVersion: 12);
    const oneQuery = (scope: 'all', underlying: 'AAPL', ledgerVersion: 12);
    final allSubscription = container.listen(
      marketPositionsProvider(allQuery),
      (_, _) {},
    );
    final oneSubscription = container.listen(
      marketPositionsProvider(oneQuery),
      (_, _) {},
    );
    addTearDown(allSubscription.close);
    addTearDown(oneSubscription.close);
    await tester.pumpAndSettle();
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/market/positions',
      ),
      hasLength(2),
    );
    await _select(tester, 'stock', 'second-feed');
    await _save(tester);
    final fresh = container
        .read(marketPositionsProvider(allQuery))
        .requireValue;
    expect(fresh.settingsRevision, 1);
    expect(fresh.rows.first.quote!.providerId, 'second-feed');
    expect(
      container
          .read(marketPositionsProvider(oneQuery))
          .requireValue
          .settingsRevision,
      1,
    );
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/market/positions',
      ),
      hasLength(4),
    );
    gate.complete();
    await tester.pumpAndSettle();
    expect(
      container
          .read(marketPositionsProvider(allQuery))
          .requireValue
          .settingsRevision,
      1,
    );
    expect(
      container
          .read(marketPositionsProvider(allQuery))
          .requireValue
          .rows
          .first
          .quote!
          .providerId,
      'second-feed',
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('已知来源版本之后返回旧版本行情时不展示旧估值', (tester) async {
    final fixture = MarketSettingsFixture(
      initialSettings: MarketSettingsFixture.settingsPayload(revision: 2),
    )..positionRevisionOverride = 1;
    await _pump(tester, fixture, includePositions: true);
    expect(find.text('行情来源已切换，请刷新行情。'), findsOneWidget);
    expect(find.text('2,326.00'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('保存中关闭面板仍接收同会话成功结果并淘汰旧行情', (tester) async {
    final fixture = MarketSettingsFixture();
    final container = await _pump(
      tester,
      fixture,
      page: const Scaffold(body: ProfileScreen()),
    );
    await _open(tester);
    const query = (scope: 'all', underlying: null, ledgerVersion: 12);
    final subscription = container.listen(
      marketPositionsProvider(query),
      (_, _) {},
    );
    addTearDown(subscription.close);
    final quoteGate = Completer<void>();
    fixture.nextPositionGate = quoteGate;
    container.invalidate(marketPositionsProvider(query));
    for (var count = 0; count < 10; count++) {
      await tester.pump(const Duration(milliseconds: 30));
    }
    // The settings controls remain usable while the independent quote read waits.
    final controller = container.read(marketSettingsProvider.notifier);
    final saveGate = Completer<void>();
    fixture.saveResponseGate = saveGate;
    final save = controller.save(
      expectedRevision: 0,
      routes: MarketRoutes(stock: ['second-feed'], option: ['first-feed']),
    );
    for (var count = 0; count < 4; count++) {
      await tester.pump(const Duration(milliseconds: 30));
    }
    await tester.ensureVisible(find.byTooltip('关闭'));
    await tester.tap(find.byTooltip('关闭'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('market-source-settings')), findsNothing);
    saveGate.complete();
    await tester.pumpAndSettle();
    expect((await save).revision, 1);
    expect(container.read(marketSettingsRevisionProvider), 1);
    expect(
      container
          .read(marketPositionsProvider(query))
          .requireValue
          .rows
          .first
          .quote!
          .providerId,
      'second-feed',
    );
    quoteGate.complete();
    await tester.pumpAndSettle();
    expect(
      container
          .read(marketPositionsProvider(query))
          .requireValue
          .settingsRevision,
      1,
    );
    expect(tester.takeException(), isNull);
  });

  for (final pending in ['read', 'save']) {
    testWidgets('切换服务端后忽略旧会话迟到的设置 $pending 响应', (tester) async {
      final gate = Completer<void>();
      final fixture = MarketSettingsFixture();
      if (pending == 'read') fixture.nextReadGate = gate;
      final container = await _pump(tester, fixture, settle: pending != 'read');
      if (pending == 'save') {
        fixture.saveResponseGate = gate;
        await _select(tester, 'stock', 'second-feed');
        await _save(tester);
      }
      final other = MarketSettingsFixture(
        initialSettings: MarketSettingsFixture.settingsPayload(
          stock: [],
          option: ['first-feed'],
        ),
      );
      final dependencies = await other.dependencies();
      await dependencies.credentialStore.write(
        const CredentialSnapshot(
          serverUrl: 'https://other-fixture.invalid',
          deviceId: 'other-device',
          accessToken: 'other-synthetic-token',
          refreshToken: 'other-synthetic-refresh',
          authMethod: 'google',
        ),
      );
      container.updateOverrides([
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          container.read(appPreferencesStoreProvider),
        ),
        appVersionProvider.overrideWith((ref) async => '1.0.0 (3)'),
      ]);
      await tester.pumpAndSettle();
      expect(_field(tester, 'stock').initialValue, '');
      expect(_field(tester, 'option').initialValue, 'first-feed');
      gate.complete();
      await tester.pumpAndSettle();
      expect(_field(tester, 'stock').initialValue, '');
      expect(container.read(marketSettingsRevisionProvider), 0);
      expect(find.text('已保存，新的行情来源已生效。'), findsNothing);
      expect(
        other.requests
            .where((request) => request.uri.path == '/v1/market/settings')
            .single
            .headers['authorization'],
        'Bearer other-synthetic-token',
      );
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('320 宽大字号下可操作所有选择与保存，空持仓不阻断设置', (tester) async {
    final fixture = MarketSettingsFixture(
      emptyPositions: true,
      initialSettings: MarketSettingsFixture.settingsPayload(
        stock: [],
        option: [],
      ),
    );
    await _pump(
      tester,
      fixture,
      size: const Size(320, 740),
      scale: 1.6,
      page: const Scaffold(body: ProfileScreen()),
    );
    await _open(tester);
    await _select(tester, 'stock', 'second-feed');
    await _select(tester, 'option', 'first-feed');
    await _save(tester);
    expect(find.text('已保存，新的行情来源已生效。'), findsOneWidget);
    expect(find.textContaining('暂无持仓'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('未登录时提示登录且不请求设置或显示密钥输入', (tester) async {
    final fixture = MarketSettingsFixture();
    await _pump(tester, fixture, paired: false);
    expect(find.text('登录后可以切换行情来源。'), findsOneWidget);
    expect(find.byType(DropdownButtonFormField<String>), findsNothing);
    expect(find.byType(TextField), findsNothing);
    expect(fixture.requests, isEmpty);
  });
}
