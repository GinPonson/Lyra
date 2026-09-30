import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

/// 注入空凭据验证真正未配对界面，不访问设备安全存储或真实服务端。
Widget _app() => ProviderScope(
  overrides: [
    sessionDependenciesProvider.overrideWithValue(
      SessionDependencies(credentialStore: MemoryCredentialStore()),
    ),
  ],
  child: const LyraApp(),
);

void main() {
  for (final size in [
    const Size(320, 740),
    const Size(390, 844),
    const Size(1440, 960),
  ]) {
    testWidgets('账号、股票、交易在 ${size.width} 宽度可导航且无溢出', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(_app());
      await tester.pumpAndSettle();
      expect(find.text('我的账号'), findsOneWidget);
      expect(find.text('连接我的账户'), findsOneWidget);
      expect(find.text('—'), findsNWidgets(3));
      expect(tester.takeException(), isNull);

      // 手机选择底栏，宽屏选择顶栏；两个入口使用同一导航状态。
      await tester.tap(find.text('股票'));
      await tester.pumpAndSettle();
      expect(find.text('我的股票'), findsOneWidget);
      expect(find.text('你的持仓，从这里展开'), findsOneWidget);
      await tester.tap(find.text('期权到期').first);
      await tester.pumpAndSettle();
      expect(find.text('连接账号，开始查看'), findsOneWidget);
      expect(tester.takeException(), isNull);

      await tester.tap(find.text('交易'));
      await tester.pumpAndSettle();
      expect(find.text('交易记录'), findsOneWidget);
      expect(find.text('全部交易'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('连接按钮能定位表单，AI 空态可以返回', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(_app());
    await tester.pumpAndSettle();
    await tester.tap(find.text('连接我的账户'));
    await tester.pumpAndSettle();
    expect(find.text('完成配对').hitTestable(), findsOneWidget);
    await tester.ensureVisible(find.text('AI 账本分析'));
    await tester.tap(find.text('AI 账本分析'));
    await tester.pumpAndSettle();
    expect(find.text('AI 分析'), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.text('我的账号'), findsOneWidget);
  });
}
