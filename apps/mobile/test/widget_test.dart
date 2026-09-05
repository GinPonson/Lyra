import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

void main() {
  testWidgets('五主导航在手机宽度可切换且总览不伪造金额', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    // 会话依赖用内存存储覆盖，保证测试不触达平台通道或真实服务端。
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(credentialStore: MemoryCredentialStore()),
          ),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();

    for (final String label in <String>['总览', '持仓', '分析', '流水', 'Agent']) {
      expect(find.text(label), findsWidgets);
    }
    expect(
      find.byKey(const ValueKey<String>('overview-hero-value')),
      findsOneWidget,
    );
    expect(find.text('—'), findsWidgets);

    await tester.tap(find.text('持仓').last);
    await tester.pumpAndSettle();
    expect(
      find.byKey(const ValueKey<String>('section-title-持仓')),
      findsOneWidget,
    );
    expect(find.text('等待服务端数据'), findsOneWidget);
  });
}
