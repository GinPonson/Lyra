import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';

import 'support/ledger_fixture.dart';

/// 已登录的主导航回归；未登录路由由 login_screen_test 覆盖。
Future<Widget> _app() async => ProviderScope(
  overrides: [
    sessionDependenciesProvider.overrideWithValue(
      await LedgerFixture().dependencies(),
    ),
    appPreferencesStoreProvider.overrideWithValue(MemoryAppPreferencesStore()),
  ],
  child: const LyraApp(),
);

void main() {
  for (final size in [
    const Size(320, 740),
    const Size(390, 844),
    const Size(1440, 960),
  ]) {
    testWidgets('总览、股票、交易、我的在 ${size.width} 宽度可导航且无溢出', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(await _app());
      await tester.pumpAndSettle();
      expect(find.text('总览'), findsNWidgets(2));
      expect(find.byKey(const ValueKey('overview-hero-value')), findsOneWidget);
      expect(tester.takeException(), isNull);

      // 手机选择底栏，宽屏选择顶栏；两个入口使用同一导航状态。
      await tester.tap(find.text('股票'));
      await tester.pumpAndSettle();
      expect(find.text('我的股票'), findsOneWidget);
      expect(find.byKey(const ValueKey('stocks-screen')), findsOneWidget);
      await tester.tap(find.text('期权到期').first);
      await tester.pumpAndSettle();
      expect(find.text('期权到期'), findsWidgets);
      expect(tester.takeException(), isNull);

      await tester.tap(find.text('交易'));
      await tester.pumpAndSettle();
      expect(find.text('交易记录'), findsOneWidget);
      expect(find.text('全部交易'), findsOneWidget);
      expect(tester.takeException(), isNull);

      await tester.tap(find.text('我的'));
      await tester.pumpAndSettle();
      expect(find.text('使用偏好'), findsOneWidget);
      expect(find.text('启动时隐藏金额'), findsOneWidget);
      expect(find.text('服务与应用'), findsOneWidget);
      expect(find.text('Google 已登录'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('已登录工作台进入 AI 二级页并可返回', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(await _app());
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('AI 账本分析'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('AI 账本分析'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('agent-screen')), findsOneWidget);
    expect(find.text('Lyra 助手'), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.text('总览'), findsNWidgets(2));
  });
}
