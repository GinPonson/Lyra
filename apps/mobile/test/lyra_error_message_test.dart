import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/design/lyra_error_message.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/design/lyra_widgets.dart';

class _DiagnosticOnlyError {
  @override
  String toString() => throw StateError('Diagnostics must not be rendered');
}

void main() {
  test('未知异常使用任务提示，不求值原始异常文本', () {
    expect(
      lyraErrorMessage(_DiagnosticOnlyError(), fallback: '暂时无法读取账户，请重试。'),
      '暂时无法读取账户，请重试。',
    );
  });

  test('已知登录和版本错误保留明确的恢复动作', () {
    for (final scenario in [
      ('CLIENT_VERSION_INCOMPATIBLE', 426, '请更新 Lyra 后重试。'),
      ('DEVICE_CREDENTIAL_EXPIRED', 401, '登录已失效，请重新登录。'),
      ('UNKNOWN_THROTTLE', 429, '操作较频繁，请稍后重试。'),
    ]) {
      expect(
        lyraErrorMessage(
          LyraApiError(
            code: scenario.$1,
            statusCode: scenario.$2,
            message: 'internal protocol /v1 request_id=diagnostic-only',
            retryable: false,
          ),
        ),
        scenario.$3,
      );
    }
  });

  testWidgets('通用错误卡片不显示协议正文、状态码或标识，并保留重试', (tester) async {
    tester.view.physicalSize = const Size(320, 740);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 1.6;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
    const failure = LyraApiError(
      code: 'QUERY_FAILED',
      message:
          'DuckDB schema_v12 https://internal.example/v1/ledger?token=secret',
      statusCode: 503,
      retryable: true,
      requestId: 'request-diagnostic-only',
      details: {'account_id': 'private-account-id'},
    );
    var retries = 0;
    await tester.pumpWidget(
      MaterialApp(
        theme: LyraTheme.dark,
        home: Scaffold(
          body: SingleChildScrollView(
            child: LyraLoadError(error: failure, onRetry: () => retries++),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('数据未能加载'), findsOneWidget);
    expect(find.text('暂时无法读取数据，请重新加载。'), findsOneWidget);
    for (final fragment in [
      'QUERY_FAILED',
      'DuckDB',
      'schema_v12',
      'internal.example',
      '503',
      'request-diagnostic-only',
      'private-account-id',
    ]) {
      expect(find.textContaining(fragment), findsNothing);
    }
    await tester.tap(find.text('重新加载'));
    expect(retries, 1);
    expect(failure.code, 'QUERY_FAILED');
    expect(failure.details?['account_id'], 'private-account-id');
    expect(tester.takeException(), isNull);
  });
}
