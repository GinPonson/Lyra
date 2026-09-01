import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';

void main() {
  testWidgets('五主导航在手机宽度可切换且总览不伪造金额', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(const LyraApp());
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
