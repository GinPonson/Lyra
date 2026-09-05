import 'dart:async';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

/// 按请求路径返回固定响应的假 HTTP 适配器；不触达真实网络。
class FakeLyraAdapter implements HttpClientAdapter {
  FakeLyraAdapter(this.handler);

  final Future<ResponseBody> Function(RequestOptions options) handler;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    return handler(options);
  }

  @override
  void close({bool force = false}) {}
}

const String _pairResponse = '''
{
  "deviceId": "d1000000-0000-4000-8000-000000000001",
  "displayName": "Test phone",
  "accessToken": "access-token-1",
  "accessTokenExpiresAt": "2026-09-02T10:15:00.000Z",
  "refreshToken": "refresh-token-1",
  "refreshTokenExpiresAt": "2026-10-02T10:00:00.000Z"
}
''';

const String _summaryResponse = '''
{
  "metadata": {
    "accountScope": {"kind": "all"},
    "accountIds": ["a2000000-0000-4000-8000-000000000001"],
    "currencyGroups": [{"currency": "USD", "accountIds": ["a2000000-0000-4000-8000-000000000001"]}],
    "aggregateCurrency": "USD",
    "ledgerVersion": 3,
    "asOf": "2026-09-02T10:00:00.000Z",
    "fetchedAt": "2026-09-02T10:00:00.000Z"
  },
  "data": {
    "aggregateCurrency": "USD",
    "groups": [{
      "currency": "USD",
      "accountIds": ["a2000000-0000-4000-8000-000000000001"],
      "data": {
        "netDeposits": "1000.00",
        "realizedPnlTotal": "12.34",
        "realizedPnlStock": "10.00",
        "realizedPnlOption": "2.34",
        "totalFees": "1.00",
        "dividends": "0.65",
        "interest": "0.10",
        "taxes": "0",
        "cashBalance": "500.00",
        "openPositions": 1,
        "transactionCount": 3
      }
    }]
  }
}
''';

/// 反复向下拖拽总览滚动视图直到目标构建并滚入可视区；sliver 懒构建需要触发。
Future<void> scrollUntilVisible(WidgetTester tester, Finder target) async {
  for (int attempt = 0; attempt < 15; attempt++) {
    if (tester.any(target)) {
      await tester.ensureVisible(target);
      await tester.pumpAndSettle();
      return;
    }
    await tester.drag(find.byType(CustomScrollView), const Offset(0, -260));
    await tester.pumpAndSettle();
  }
  throw StateError('滚动后仍未找到目标组件');
}

void main() {
  testWidgets('配对表单兑换配对码后总览展示真实账本汇总', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final store = MemoryCredentialStore();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              clientFactory: (String serverUrl) {
                return Dio(
                  BaseOptions(baseUrl: serverUrl),
                )..httpClientAdapter = FakeLyraAdapter((RequestOptions options) async {
                    final path = options.uri.path;
                    if (path == '/v1/devices/pair') {
                      return ResponseBody.fromString(
                        _pairResponse,
                        201,
                        headers: <String, List<String>>{
                          Headers.contentTypeHeader: <String>[Headers.jsonContentType],
                          'x-request-id': <String>['req-pair'],
                        },
                      );
                    }
                    if (path == '/v1/ledger/summary') {
                      return ResponseBody.fromString(
                        _summaryResponse,
                        200,
                        headers: <String, List<String>>{
                          Headers.contentTypeHeader: <String>[Headers.jsonContentType],
                          'x-request-id': <String>['req-summary'],
                        },
                      );
                    }
                    return ResponseBody.fromString('unexpected', 404);
                  });
              },
            ),
          ),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();

    // 未配对状态展示配对表单；面板在折叠列表下方，需要滚动到可见区。
    await scrollUntilVisible(tester, find.text('配对 Lyra 服务端'));
    expect(find.text('配对 Lyra 服务端'), findsOneWidget);

    await tester.enterText(
      find.widgetWithText(TextField, '服务端地址'),
      'http://127.0.0.1:3141',
    );
    await tester.enterText(
      find.widgetWithText(TextField, '配对码'),
      'ABCDE-FGHIJ',
    );
    await scrollUntilVisible(tester, find.text('完成配对'));
    await tester.tap(find.text('完成配对'));
    await tester.pumpAndSettle();

    // 已配对状态展示真实汇总：十进制字符串原样展示，不伪造。先滚回顶部。
    await tester.drag(find.byType(CustomScrollView), const Offset(0, 800));
    await tester.pumpAndSettle();
    expect(find.text('已配对'), findsOneWidget);
    expect(
      find.byKey(const ValueKey<String>('overview-hero-value')),
      findsOneWidget,
    );
    expect(find.text('12.34'), findsOneWidget);
    expect(find.text('1000.00'), findsOneWidget);
    expect(find.text('500.00'), findsOneWidget);
    expect(find.textContaining('账本版本 3'), findsOneWidget);

    // 凭据已进入安全存储抽象。
    final stored = await store.read();
    expect(stored, isNotNull);
    expect(stored?.deviceId, 'd1000000-0000-4000-8000-000000000001');
  });
}
