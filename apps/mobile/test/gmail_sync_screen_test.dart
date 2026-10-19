import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/features/gmail/gmail_sync_screen.dart';
import 'package:lyra_mobile/features/profile/brokerage_accounts_screen.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;
import 'support/ledger_fixture.dart';

const _connectionId = 'b3000000-0000-4000-8000-000000000001';

void main() {
  testWidgets('证券账户页进入嘉信邮件；首扫须显式选范围且结果分清冲突', (tester) async {
    tester.view.physicalSize = const Size(320, 740);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final fixture = _GmailScreenFixture();
    await _pumpGmailScreen(tester, fixture, textScale: 2);

    expect(find.byKey(const ValueKey('profile-gmail-sync')), findsOneWidget);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));

    expect(find.text('授权 Gmail 只读权限，Lyra 仅筛选嘉信成交确认邮件。'), findsOneWidget);
    expect(find.text('trades@example.com'), findsOneWidget);
    expect(find.text('待首次扫描 · 尚未同步'), findsOneWidget);
    expect(find.text('首次扫描范围'), findsOneWidget);
    expect(find.text('全部嘉信成交邮件（推荐）'), findsOneWidget);
    expect(find.text('自定义起始日期'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('gmail-sync-$_connectionId')),
      findsNothing,
    );
    expect(_syncRequests(fixture), isEmpty);
    expect(tester.takeException(), isNull);

    await _tap(tester, find.byKey(const ValueKey('gmail-date-$_connectionId')));
    expect(find.text('选择扫描起始日期'), findsOneWidget);
    expect(find.byType(CalendarDatePicker), findsOneWidget);
    await _tap(tester, find.text('取消'));
    expect(_syncRequests(fixture), isEmpty);

    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-scan-all-$_connectionId')),
    );
    expect(find.text('同步全部嘉信邮件'), findsOneWidget);
    await _tap(tester, find.byKey(const ValueKey('gmail-sync-$_connectionId')));
    final request = _syncRequests(fixture).single;
    expect(request.data, {
      'explicitAccountId': LedgerFixture.account,
      'scanAll': true,
    });
    expect(find.text('新增交易 2 笔'), findsOneWidget);
    expect(find.text('账户映射冲突 0 封'), findsOneWidget);
    expect(find.text('同日成交顺序冲突 1 封'), findsOneWidget);
    expect(find.text('部分同日成交顺序无法确认，请导入包含该日全部成交的嘉信 CSV。'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('active 连接可显式重新扫描，选全部后发送重扫范围', (tester) async {
    final fixture = _GmailScreenFixture()..connectionStatus = 'active';
    await _pumpGmailScreen(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));

    expect(
      find.byKey(const ValueKey('gmail-rescan-$_connectionId')),
      findsOneWidget,
    );
    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-rescan-$_connectionId')),
    );
    expect(
      find.byKey(const ValueKey('gmail-cancel-rescan-$_connectionId')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('gmail-sync-$_connectionId')),
      findsNothing,
    );

    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-scan-all-$_connectionId')),
    );
    expect(find.text('重新扫描全部嘉信邮件'), findsOneWidget);
    await _tap(tester, find.byKey(const ValueKey('gmail-sync-$_connectionId')));

    expect(_syncRequests(fixture).single.data, {
      'explicitAccountId': LedgerFixture.account,
      'scanAll': true,
    });
    expect(find.text('立即同步'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('新同步清除旧摘要；部分失败后刷新账本与连接状态', (tester) async {
    final fixture = _GmailScreenFixture();
    await _pumpGmailScreen(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));
    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-scan-all-$_connectionId')),
    );
    await _tap(tester, find.byKey(const ValueKey('gmail-sync-$_connectionId')));

    expect(find.text('新增交易 2 笔'), findsOneWidget);
    fixture.failSyncAtAttempt = 2;
    final beforeFailedSync = fixture.requests.length;

    await _tap(tester, find.byKey(const ValueKey('gmail-sync-$_connectionId')));

    expect(_syncRequests(fixture), hasLength(2));
    expect(_syncRequests(fixture).last.data, {
      'explicitAccountId': LedgerFixture.account,
    });
    expect(find.text('新增交易 2 笔'), findsNothing);
    expect(find.text('部分交易无法与现有持仓对应，请核对开仓和平仓记录后再试。'), findsOneWidget);
    final postFailureReads = fixture.requests.skip(beforeFailedSync);
    expect(
      postFailureReads.any(
        (request) => request.uri.path == '/v1/ledger/summary',
      ),
      isTrue,
    );
    expect(
      postFailureReads.any(
        (request) => request.uri.path == '/v1/google/connections',
      ),
      isTrue,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('多个证券账户需要用户选择，写请求使用选中的现有账户', (tester) async {
    final fixture = _GmailScreenFixture()..multipleAccounts = true;
    await _pumpGmailScreen(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));

    await _tap(tester, find.byKey(const ValueKey('gmail-account-selector')));
    await _tap(tester, find.text('退休账户 · 尾号 ••951'));
    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-scan-all-$_connectionId')),
    );
    await _tap(tester, find.byKey(const ValueKey('gmail-sync-$_connectionId')));

    expect(_syncRequests(fixture).single.data, {
      'explicitAccountId': _secondAccountId,
      'scanAll': true,
    });
    expect(tester.takeException(), isNull);
  });

  testWidgets('断开 Gmail 前显示当前邮箱并要求用户明确确认', (tester) async {
    final fixture = _GmailScreenFixture();
    await _pumpGmailScreen(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));
    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-disconnect-$_connectionId')),
    );

    expect(find.text('断开 trades@example.com？'), findsOneWidget);
    expect(find.text('将撤销该邮箱的只读授权，并停止后续邮件同步。'), findsOneWidget);
    await _tap(tester, find.text('保留连接'));
    expect(
      fixture.requests.where((request) => request.method == 'DELETE'),
      isEmpty,
    );
    expect(find.text('trades@example.com'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('连接状态明确解释 Gmail API 错误、授权取消和权限缺失', (tester) async {
    final fixture = _GmailScreenFixture();
    await _pumpGmailScreen(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));

    for (final (code, message) in [
      ('GMAIL_API_DISABLED', 'Google Cloud 项目尚未启用 Gmail API，请启用后重试。'),
      ('GMAIL_RATE_LIMITED', 'Gmail 请求达到限额，请稍后再试。'),
      ('GMAIL_ACCESS_DENIED', 'Google 拒绝访问 Gmail，请检查邮箱权限或管理员策略。'),
      ('GMAIL_SCOPE_MISSING', '未勾选 Gmail 只读权限，请重新连接并授予该权限。'),
      ('GMAIL_AUTHORIZATION_DENIED', '授权已取消，尚未授予 Gmail 只读权限。'),
      ('GOOGLE_AUTH_TRANSACTION_EXPIRED', 'Google 授权已超时，请重新连接。'),
      ('GOOGLE_IDENTITY_UNVERIFIED', 'Google 邮箱身份未验证，请完成邮箱验证后重新连接。'),
      ('GOOGLE_TOKEN_EXCHANGE_FAILED', 'Google 授权未能完成，请重新连接；若持续失败，请检查服务端配置。'),
    ]) {
      fixture.connectionErrorCode = code;
      await _tap(tester, find.byKey(const ValueKey('gmail-refresh')));
      expect(find.text(message), findsOneWidget);
      expect(find.textContaining('GMAIL_'), findsNothing);
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('退出 Lyra 后不显示迟到的 Gmail 同步计数或邮箱', (tester) async {
    final fixture = _GmailScreenFixture();
    final started = Completer<void>();
    final release = Completer<void>();
    fixture.beforeResponse = (options) async {
      if (options.uri.path.endsWith('/sync')) {
        if (!started.isCompleted) started.complete();
        await release.future;
      }
    };
    await _pumpGmailScreen(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-gmail-sync')));
    await _tap(
      tester,
      find.byKey(const ValueKey('gmail-scan-all-$_connectionId')),
    );
    final container = ProviderScope.containerOf(
      tester.element(find.byType(GmailSyncScreen)),
    );
    final syncButton = find.byKey(const ValueKey('gmail-sync-$_connectionId'));
    await tester.ensureVisible(syncButton);
    await tester.tap(syncButton);
    // 同步中的加载指示器不会 settle；推进有限帧来观察等待和退出状态。
    for (var frame = 0; frame < 6; frame++) {
      await tester.pump(const Duration(milliseconds: 30));
    }
    expect(started.isCompleted, isTrue);
    final signOut = container.read(sessionProvider.notifier).signOut();
    unawaited(signOut.catchError((Object _) {}));
    for (var frame = 0; frame < 6; frame++) {
      await tester.pump(const Duration(milliseconds: 30));
    }
    release.complete();
    for (var frame = 0; frame < 6; frame++) {
      await tester.pump(const Duration(milliseconds: 30));
    }
    expect(find.text('新增交易 2 笔'), findsNothing);
    expect(find.text('trades@example.com'), findsNothing);
    expect(_syncRequests(fixture), hasLength(1));
    expect(tester.takeException(), isNull);
  });
}

/// 以已配对的合成会话运行证券账户入口及 Gmail 页面。
Future<void> _pumpGmailScreen(
  WidgetTester tester,
  _GmailScreenFixture fixture, {
  double textScale = 1,
}) async {
  final dependencies = await fixture.dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [sessionDependenciesProvider.overrideWithValue(dependencies)],
      child: MaterialApp(
        theme: LyraTheme.dark,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: const BrokerageAccountsScreen(),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

/// 点击窄屏滚动页中的控件并等待异步请求或导航结束。
Future<void> _tap(WidgetTester tester, Finder target) async {
  await tester.ensureVisible(target);
  await tester.pumpAndSettle();
  await tester.tap(target);
  await tester.pumpAndSettle();
}

/// 取出任何同步写请求，供首扫门禁断言使用。
Iterable<RequestOptions> _syncRequests(_GmailScreenFixture fixture) =>
    fixture.requests.where(
      (request) =>
          request.uri.path.endsWith('/sync') && request.method == 'POST',
    );

/// 返回合成登录、账户与 Gmail 合同，不连接真实服务。
class _GmailScreenFixture extends LedgerFixture {
  String connectionStatus = 'pending_initial_scan';
  String? connectionErrorCode;
  bool multipleAccounts = false;
  int syncRequestCount = 0;
  int? failSyncAtAttempt;

  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    if (command == 'me') {
      return {
        'user': {
          'userId': 'fixture-user',
          'displayName': 'Fixture User',
          'status': 'active',
          'role': 'member',
          'identity': {
            'provider': 'google',
            'subject': 'fixture-google-subject',
            'email': 'fixture@example.com',
          },
        },
      };
    }
    if (command == 'connections') {
      return {
        'configured': true,
        'connections': [
          {
            'id': _connectionId,
            'googleEmail': 'trades@example.com',
            'status': connectionStatus,
            'lastErrorCode': connectionErrorCode,
            'lastSyncAt': null,
            'createdAt': '2026-10-01T08:00:00Z',
            'updatedAt': '2026-10-01T08:00:00Z',
          },
        ],
      };
    }
    if (command == 'sync') {
      connectionStatus = 'active';
      return {
        'mode': 'bounded_rescan',
        'truncated': false,
        'insertedTransactions': 2,
        'existingTransactions': 3,
        'conflictMessages': 1,
        'replayOrderConflictMessages': 1,
        'rejectedMessages': 2,
        'connection': {
          'id': _connectionId,
          'status': 'active',
          'lastSyncAt': '2026-10-18T08:30:00Z',
        },
      };
    }
    if (command == 'logout') return const <String, Object?>{};
    return super.response(command, query);
  }

  /// 在需要验证账户选择时加入第二条合成记录，不改变生产账户数据。
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (!multipleAccounts || command != 'accounts') return data;
    final rows = (data['rows'] as List<Object?>).cast<Map<String, Object?>>();
    return {
      ...data,
      'rows': [
        ...rows,
        {
          'id': _secondAccountId,
          'displayName': '退休账户',
          'maskedSuffix': '••951',
          'baseCurrency': 'USD',
          'status': 'active',
        },
      ],
      'count': rows.length + 1,
    };
  }

  /// 构造默认读模型请求并记录每条请求供 UI 合同断言使用。
  @override
  Future<SessionDependencies> dependencies({bool paired = true}) async {
    final base = await super.dependencies(paired: paired);
    return SessionDependencies(
      credentialStore: base.credentialStore,
      clientFactory: (url) => Dio(BaseOptions(baseUrl: url))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests.add(options);
          await beforeResponse?.call(options);
          final command = options.uri.path.split('/').last;
          final isSync =
              options.method == 'POST' && options.uri.path.endsWith('/sync');
          if (isSync) syncRequestCount++;
          final failedSync = isSync && failSyncAtAttempt == syncRequestCount;
          final body = failedSync
              ? {
                  'code': 'LEDGER_REBUILD_FAILED',
                  'message': '测试中存在无法匹配的账本交易',
                  'retryable': false,
                }
              : response(command, options.queryParameters);
          return ResponseBody.fromString(
            jsonEncode(body),
            failedSync ? 409 : 200,
            headers: {
              Headers.contentTypeHeader: [Headers.jsonContentType],
            },
          );
        }),
    );
  }
}

const _secondAccountId = 'a2000000-0000-4000-8000-000000000002';
