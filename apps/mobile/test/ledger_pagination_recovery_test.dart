import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';

import 'support/ledger_fixture.dart';

/// 为分页恢复测试提供 519 条可见标记唯一的合成交易和不透明游标。
class _PaginatedTradeFixture extends LedgerFixture {
  static const rowCount = 519;
  static const pageSize = 50;

  /// 游标故意包含查询字符，验证页面续读时按原值传递服务端 token。
  final cursors = List<String>.generate(
    10,
    (index) => 'opaque/page/${index + 1}?marker=A+B&continuation=%2F',
    growable: false,
  );

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    if (command != 'trades') return super.payload(command, query);

    final cursor = query['cursor'] as String?;
    final cursorIndex = cursor == null ? -1 : cursors.indexOf(cursor);
    if (cursor != null && cursorIndex < 0) {
      throw StateError('测试收到未知交易游标：$cursor');
    }
    final pageIndex = cursorIndex + 1;
    if (pageIndex >= 11) {
      throw StateError('测试收到未知交易游标：$cursor');
    }
    final start = pageIndex * pageSize;
    final end = (start + pageSize < rowCount) ? start + pageSize : rowCount;
    return {
      'rows': [for (var index = start; index < end; index++) _trade(index)],
      'count': end - start,
      'nextCursor': pageIndex < cursors.length ? cursors[pageIndex] : null,
    };
  }

  /// 构造在交易列表标题中直接可见的唯一行标记。
  Map<String, Object?> _trade(int index) {
    final marker = _marker(index + 1);
    return {
      'accountId': LedgerFixture.account,
      'symbol': marker,
      'underlying': marker,
      'action': 'Buy',
      'assetType': 'stock',
      'quantity': '1',
      'price': '1.00',
      'fees': '0.00',
      'amount': '-1.00',
      'transactionDate': '2026-09-04',
      'recordDate': '2026-09-04',
      'sourceType': 'fixture',
    };
  }
}

/// 返回第 [number] 条交易在真实列表标题中使用的唯一文本。
String _marker(int number) => 'TXN-${number.toString().padLeft(4, '0')}';

/// 在固定窄屏中打开交易页，选择单账户范围并应用买入动作筛选。
Future<void> _openFilteredTransactions(
  WidgetTester tester,
  LedgerFixture fixture,
) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);

  final dependencies = await fixture.dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [sessionDependenciesProvider.overrideWithValue(dependencies)],
      child: const LyraApp(),
    ),
  );
  await tester.pumpAndSettle();
  await tester.tap(find.text('交易'));
  await tester.pumpAndSettle();
  await tester.tap(find.text('全部账户').last);
  await tester.pumpAndSettle();
  await tester.tap(find.text('长期投资'));
  await tester.pumpAndSettle();
  await tester.tap(find.byTooltip('筛选'));
  await tester.pumpAndSettle();
  await tester.tap(find.text('全部动作'));
  await tester.pumpAndSettle();
  await tester.tap(find.text('买入').last);
  await tester.pumpAndSettle();
  await tester.tap(find.text('应用筛选'));
  await tester.pumpAndSettle();
  await _pumpUntil(
    tester,
    () => _renderedMarkers(tester).length >= _PaginatedTradeFixture.pageSize,
    '带账户范围和动作筛选的交易首屏',
  );
}

/// 推进测试帧直到 [ready] 成立，避免依赖永不结束的加载动画。
Future<void> _pumpUntil(
  WidgetTester tester,
  bool Function() ready,
  String description,
) async {
  for (var attempt = 0; attempt < 400; attempt++) {
    if (ready()) return;
    await tester.pump(const Duration(milliseconds: 20));
  }
  fail('$description在测试等待期限内未完成');
}

/// 通过用户滚动继续读取，直到列表可见 [targetCount] 条合并记录。
Future<void> _scrollUntilCount(
  WidgetTester tester,
  int targetCount, {
  bool stopAtRetry = false,
}) async {
  final scrollable = find.byKey(const PageStorageKey<String>('ledger-scroll'));
  for (var attempt = 0; attempt < 240; attempt++) {
    if (_renderedMarkers(tester).length >= targetCount) return;
    if (stopAtRetry && find.text('重新加载').evaluate().isNotEmpty) return;
    await tester.drag(scrollable, const Offset(0, -1200));
    await tester.pump(const Duration(milliseconds: 40));
    await tester.pump(const Duration(milliseconds: 40));
  }
  fail('交易列表滚动后仍未加载到 $targetCount 条记录');
}

/// 将错误操作滚入账本视口，保证后续点击走真实的命中测试路径。
Future<void> _scrollControlIntoView(WidgetTester tester, Finder control) async {
  final scrollable = find.byKey(const PageStorageKey<String>('ledger-scroll'));
  final viewport = tester.getRect(scrollable);
  for (var attempt = 0; attempt < 20; attempt++) {
    final controlRect = tester.getRect(control);
    if (controlRect.top >= viewport.top &&
        controlRect.bottom <= viewport.bottom) {
      return;
    }
    final offset = controlRect.top >= viewport.bottom ? -900.0 : 900.0;
    await tester.drag(scrollable, Offset(0, offset));
    await tester.pump(const Duration(milliseconds: 40));
  }
  fail('交易错误操作仍未滚入可视区域');
}

/// 读取屏幕上所有交易标题，保留渲染次序以验证无遗漏和重复。
List<String> _renderedMarkers(WidgetTester tester) => find
    .byWidgetPredicate(
      (widget) => widget is Text && (widget.data?.startsWith('TXN-') ?? false),
    )
    .evaluate()
    .map((element) => (element.widget as Text).data!)
    .toList(growable: false);

/// 创建模拟连接中断，使失败经过 LyraApiClient 的实际错误转换路径。
DioException _connectionFailure(RequestOptions options) => DioException(
  requestOptions: options,
  type: DioExceptionType.connectionError,
  message: 'synthetic test connection interruption',
);

/// 验证 11 页合并结果、游标和请求范围稳定，以及第八页瞬时故障可恢复。
void main() {
  testWidgets('519 条交易连续滚动无漏重，瞬时连接故障可从原游标恢复', (tester) async {
    final fixture = _PaginatedTradeFixture();
    final failedCursor = fixture.cursors[6];
    var dropped = false;
    fixture.beforeResponse = (options) async {
      if (options.uri.path.endsWith('/trades') &&
          options.queryParameters['action'] == 'Buy' &&
          options.queryParameters['cursor'] == failedCursor &&
          !dropped) {
        dropped = true;
        throw _connectionFailure(options);
      }
    };

    await _openFilteredTransactions(tester, fixture);
    final expectedMarkers = List<String>.generate(
      _PaginatedTradeFixture.rowCount,
      (index) => _marker(index + 1),
      growable: false,
    );
    await _scrollUntilCount(tester, 350);
    expect(_renderedMarkers(tester), expectedMarkers.take(350).toList());

    await _scrollUntilCount(tester, 400);
    expect(find.text('重新加载'), findsNothing);
    expect(find.text('数据未能加载'), findsNothing);

    await _scrollUntilCount(tester, _PaginatedTradeFixture.rowCount);
    expect(_renderedMarkers(tester), expectedMarkers);
    expect(dropped, isTrue);

    final filteredReads = fixture.requests
        .where(
          (request) =>
              request.uri.path.endsWith('/trades') &&
              request.queryParameters['scope'] == LedgerFixture.account &&
              request.queryParameters['action'] == 'Buy',
        )
        .toList(growable: false);
    expect(
      filteredReads
          .map((request) => request.queryParameters['cursor'])
          .toList(),
      [
        null,
        ...fixture.cursors.take(6),
        failedCursor,
        failedCursor,
        ...fixture.cursors.skip(7),
      ],
    );
    for (final request in filteredReads) {
      expect(request.queryParameters['scope'], LedgerFixture.account);
      expect(request.queryParameters['action'], 'Buy');
      expect(request.queryParameters['limit'], '50');
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('持续连接失败显示明确错误，手动恢复沿用失败游标', (tester) async {
    final fixture = _PaginatedTradeFixture();
    final failedCursor = fixture.cursors[1];
    var keepFailing = true;
    fixture.beforeResponse = (options) async {
      if (keepFailing &&
          options.uri.path.endsWith('/trades') &&
          options.queryParameters['action'] == 'Buy' &&
          options.queryParameters['cursor'] == failedCursor) {
        throw _connectionFailure(options);
      }
    };

    await _openFilteredTransactions(tester, fixture);
    await _scrollUntilCount(tester, 100);
    await _scrollUntilCount(tester, 150, stopAtRetry: true);
    expect(find.text('网络连接失败，请检查网络后重试。'), findsOneWidget);
    expect(_renderedMarkers(tester), [
      for (var index = 1; index <= 100; index++) _marker(index),
    ]);

    final beforeRecovery = fixture.requests
        .where(
          (request) =>
              request.uri.path.endsWith('/trades') &&
              request.queryParameters['action'] == 'Buy' &&
              request.queryParameters['cursor'] == failedCursor,
        )
        .length;
    expect(beforeRecovery, 2);
    keepFailing = false;
    await _scrollControlIntoView(tester, find.text('重新加载'));
    await tester.tap(find.text('重新加载'));
    await _pumpUntil(
      tester,
      () => _renderedMarkers(tester).length >= 150,
      '持续失败后的同游标手动恢复',
    );

    final cursorReads = fixture.requests
        .where(
          (request) =>
              request.uri.path.endsWith('/trades') &&
              request.queryParameters['scope'] == LedgerFixture.account &&
              request.queryParameters['action'] == 'Buy' &&
              request.queryParameters['cursor'] == failedCursor,
        )
        .toList(growable: false);
    expect(cursorReads.length, beforeRecovery + 1);
    expect(cursorReads.last.queryParameters['cursor'], failedCursor);
    expect(_renderedMarkers(tester), [
      for (var index = 1; index <= 150; index++) _marker(index),
    ]);
    expect(tester.takeException(), isNull);
  });
}
