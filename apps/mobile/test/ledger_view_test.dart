import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/features/ledger/ledger_screen.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';

import 'support/ledger_fixture.dart';

class _NewAuditFixture extends LedgerFixture {
  _NewAuditFixture({required this.status, required this.ok});
  final Object? status;
  final bool ok;

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    if (command != 'audit') return super.payload(command, query);
    return {
      'rows': [
        {
          'check': 'future_internal_check_v2',
          'status': status,
          'count': ok ? 0 : 3,
        },
      ],
      'ok': ok,
      'failed': ok ? 0 : 1,
    };
  }
}

class _ExtendedTradeFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command == 'trades') {
      final row = (data['rows'] as List).first as Map<String, Object?>;
      data['rows'] = [
        {
          ...row,
          'action': 'Broker Adjustment',
          'amount': '0.000000000000000123',
          'description': {
            'internal_route': '/v1/internal/example',
            'accountId': LedgerFixture.account,
          },
          'debug_trace': 'internal_trace_id',
        },
      ];
    }
    return data;
  }
}

Future<void> _pumpReport(
  WidgetTester tester,
  LedgerFixture fixture,
  LedgerView view,
) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dependencies = await fixture.dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [sessionDependenciesProvider.overrideWithValue(dependencies)],
      child: MaterialApp(
        theme: LyraTheme.dark,
        home: ReportScreen(view: view),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  for (final passed in [false, true]) {
    testWidgets('新增账本检查保留真实结果：passed=$passed', (tester) async {
      await _pumpReport(
        tester,
        _NewAuditFixture(status: passed ? 'pass' : 'fail', ok: passed),
        LedgerView.audit,
      );
      expect(find.text('其他账本检查'), findsOneWidget);
      expect(find.text(passed ? '通过' : '未通过'), findsOneWidget);
      expect(find.text(passed ? '账本检查通过' : '1 项检查需要处理'), findsOneWidget);
      expect(find.textContaining('future_internal_check'), findsNothing);
      expect(find.textContaining('future internal check'), findsNothing);
      if (!passed) {
        expect(find.text('账本检查通过'), findsNothing);
        expect(find.text('3'), findsOneWidget);
      }
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('未知检查状态明确待确认，不显示原始内容或误报通过', (tester) async {
    await _pumpReport(
      tester,
      _NewAuditFixture(status: {'new_status': 'internal_status_v2'}, ok: true),
      LedgerView.audit,
    );
    expect(find.text('部分检查结果待确认'), findsOneWidget);
    expect(find.text('结果待确认'), findsOneWidget);
    expect(find.text('账本检查通过'), findsNothing);
    await tester.tap(find.byType(ExpansionTile).first);
    await tester.pumpAndSettle();
    expect(find.text('结果待确认'), findsNWidgets(2));
    expect(find.textContaining('internal_status'), findsNothing);
    expect(find.textContaining('new_status'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  for (final (view, title, original) in [
    (LedgerView.risk, '0–30 天', '0-30d'),
    (LedgerView.holdingPeriod, '8–30 天', '8-30d'),
  ]) {
    testWidgets('${view.title} 用可读天数范围保留分组含义', (tester) async {
      await _pumpReport(tester, LedgerFixture(), view);
      expect(find.text(title), findsOneWidget);
      expect(find.text(original), findsNothing);
      expect(find.textContaining('FIFO'), findsNothing);
      expect(find.textContaining(' lot'), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('明细保留券商动作和精确金额，未知结构不展开为内部信息', (tester) async {
    await _pumpReport(tester, _ExtendedTradeFixture(), LedgerView.trades);
    final row = find.byType(ExpansionTile).first;
    await tester.ensureVisible(row);
    await tester.tap(row);
    await tester.pumpAndSettle();
    expect(find.text('Broker Adjustment'), findsOneWidget);
    expect(find.text('0.000000000000000123'), findsOneWidget);
    expect(find.text('暂无法显示'), findsOneWidget);
    expect(find.textContaining('internal_route'), findsNothing);
    expect(find.textContaining('/v1/internal'), findsNothing);
    expect(find.textContaining('internal_trace_id'), findsNothing);
    expect(find.textContaining(LedgerFixture.account), findsNothing);
    expect(find.text('USD'), findsWidgets);
    expect(tester.takeException(), isNull);
  });
}
