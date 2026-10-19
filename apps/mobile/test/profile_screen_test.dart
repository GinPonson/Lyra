import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/profile/profile_screen.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;
import 'support/ledger_fixture.dart';

const _snapshot = CredentialSnapshot(
  serverUrl: 'https://fixture.invalid',
  deviceId: 'profile-device',
  accessToken: 'profile-access',
  refreshToken: 'profile-refresh',
  userId: 'profile-user',
  authMethod: 'google',
  userEmail: 'owner@example.com',
  googleSubject: 'profile-owner',
);

ResponseBody _json(Object value, [int status = 200]) => ResponseBody.fromString(
  jsonEncode(value),
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);

/// Profile 页面测试夹具，可为长身份布局指定测试凭据。
class _ProfileFixture extends LedgerFixture {
  /// 使用 [snapshot] 作为模拟登录凭据，默认采用常规 Profile 用户。
  _ProfileFixture({this.snapshot = _snapshot});

  final CredentialSnapshot snapshot;
  final store = MemoryCredentialStore();
  final openedUrls = <Uri>[];
  bool offline = false;
  bool logoutOffline = false;
  bool accountsUnavailable = false;
  final addedAccounts = <Map<String, Object?>>[];

  Dio dio(String url) =>
      Dio(BaseOptions(baseUrl: url))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests.add(options);
          final path = options.uri.path;
          if (accountsUnavailable && path == '/v1/ledger/accounts') {
            return _json({
              'code': 'ACCOUNT_READ_FAILED',
              'message': 'DuckDB /internal/account.ts request_id=private-trace',
              'retryable': true,
            }, 503);
          }
          if (path == '/v1/auth/logout') {
            if (logoutOffline) {
              throw DioException(
                requestOptions: options,
                type: DioExceptionType.connectionTimeout,
              );
            }
            return ResponseBody.fromString('', 204);
          }
          if (path == '/v1/auth/providers') {
            return _json({
              'providers': [
                {'id': 'google', 'name': 'Google', 'available': true},
                {'id': 'linuxdo', 'name': 'Linux.do', 'available': false},
                {'id': 'github', 'name': 'GitHub', 'available': false},
              ],
            });
          }
          if (path == '/v1/auth/google/start') {
            return _json({
              'loginId': 'profile-login',
              'authorizationUrl':
                  'https://accounts.google.com/o/oauth2/v2/auth?state=profile',
              'expiresAt': DateTime.now()
                  .add(const Duration(minutes: 10))
                  .toUtc()
                  .toIso8601String(),
              'pollIntervalSeconds': 2,
            }, 201);
          }
          if (path == '/v1/auth/google/exchange') {
            return _json({
              'status': 'complete',
              'user': {
                'userId': 'new-owner-user',
                'displayName': 'New user',
                'status': 'active',
                'role': 'member',
                'identity': {
                  'provider': 'google',
                  'subject': 'new-owner',
                  'email': 'new@example.com',
                },
              },
              'deviceId': 'new-device',
              'displayName': 'Lyra Mobile',
              'accessToken': 'new-access',
              'refreshToken': 'new-refresh',
              'accessTokenExpiresAt': '2026-10-12T10:00:00Z',
              'refreshTokenExpiresAt': '2026-11-12T10:00:00Z',
            });
          }
          if (offline) {
            throw DioException(
              requestOptions: options,
              type: DioExceptionType.connectionTimeout,
            );
          }
          if (path == '/v1/system/info') {
            // 公开连接检测不读取当前登录的访问凭据。
            expectSync(options.headers['authorization'], isNull);
            return _json({
              'server_version': '1.2.3',
              'protocol_version': 3,
              'min_supported_protocol_version': 3,
              'max_supported_protocol_version': 3,
              'schema_version': 16,
            });
          }
          if (path == '/v1/health') return _json({'status': 'healthy'});
          if (path == '/v1/market/settings') {
            return _json({
              'revision': 0,
              'routes': {'stock': <String>[], 'option': <String>[]},
              'effectiveRoutes': {'stock': <String>[], 'option': <String>[]},
              'providers': <Object?>[],
              'updatedAt': null,
            });
          }
          if (path == '/v1/ledger/accounts' && options.method == 'POST') {
            final body = (options.data as Map).cast<String, Object?>();
            final account = {
              'id': 'a2000000-0000-4000-8000-000000000002',
              'displayName': body['displayName'],
              'maskedSuffix': '678',
              'baseCurrency': body['baseCurrency'],
              'status': 'active',
            };
            addedAccounts.add(account);
            return _json({'account': account}, 201);
          }
          return _json(response(path.split('/').last, options.queryParameters));
        });

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final original = super.payload(command, query);
    if (command != 'accounts') return original;
    return {
      ...original,
      'rows': [...original['rows'] as List, ...addedAccounts],
      'count': 1 + addedAccounts.length,
    };
  }

  @override
  Future<SessionDependencies> dependencies({bool paired = true}) async {
    if (paired) await store.write(snapshot);
    return SessionDependencies(
      credentialStore: store,
      agentSessionStore: MemoryAgentSessionStore(),
      clientFactory: dio,
      openAuthorizationUrl: (url) async {
        openedUrls.add(url);
        return true;
      },
    );
  }
}

class _FailablePreferencesStore extends MemoryAppPreferencesStore {
  _FailablePreferencesStore({super.initialValue});
  bool failWrites = false;

  @override
  Future<void> write(AppPreferences preferences) async {
    if (failWrites) throw StateError('test storage unavailable');
    await super.write(preferences);
  }
}

Future<ProviderContainer> _pumpProfile(
  WidgetTester tester,
  _ProfileFixture fixture, {
  bool signedIn = true,
  AppPreferencesStore? preferencesStore,
  Size size = const Size(390, 844),
  double textScale = 1,
  Future<String> Function()? loadAppVersion,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dependencies = await fixture.dependencies(paired: signedIn);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          preferencesStore ?? MemoryAppPreferencesStore(),
        ),
        systemDioFactoryProvider.overrideWithValue(fixture.dio),
        appVersionProvider.overrideWith(
          (ref) => loadAppVersion?.call() ?? Future.value('1.0.0 (2)'),
        ),
      ],
      child: MaterialApp(
        theme: LyraTheme.dark,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: const Scaffold(body: ProfileScreen()),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return ProviderScope.containerOf(tester.element(find.byType(ProfileScreen)));
}

Future<void> _tap(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pumpAndSettle();
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

Future<void> _replaceIdentity(
  WidgetTester tester,
  ProviderContainer container,
  _ProfileFixture fixture,
) async {
  final logout = expectLater(
    container.read(sessionProvider.notifier).signOut(),
    completes,
  );
  await tester.pump(const Duration(milliseconds: 100));
  await logout;
  await fixture.store.write(
    const CredentialSnapshot(
      serverUrl: 'https://fixture.invalid',
      deviceId: 'different-device',
      accessToken: 'different-access',
      refreshToken: 'different-refresh',
      userId: 'different-user',
      authMethod: 'google',
      userEmail: 'different@example.com',
      googleSubject: 'different-owner',
    ),
  );
  container.invalidate(sessionProvider);
  await tester.pumpAndSettle();
}

void main() {
  for (final configuration in [
    (const Size(320, 740), 1.0),
    (const Size(390, 844), 1.0),
    (const Size(320, 740), 2.0),
    (const Size(1440, 960), 2.0),
  ]) {
    testWidgets(
      '我的页 ${configuration.$1.width} 宽 / ${configuration.$2} 字号可读且无溢出',
      (tester) async {
        await _pumpProfile(
          tester,
          _ProfileFixture(),
          size: configuration.$1,
          textScale: configuration.$2,
        );
        expect(find.text('owner@example.com'), findsOneWidget);
        expect(find.text('Google'), findsOneWidget);
        expect(find.text('Lyra Mobile'), findsNothing);
        expect(find.text('账户与数据'), findsNothing);
        expect(find.text('使用偏好'), findsNothing);
        expect(find.text('应用'), findsNothing);
        expect(find.text('启动时隐藏金额'), findsNothing);
        expect(
          find.byKey(const ValueKey('startup-hide-amounts')),
          findsNothing,
        );
        expect(find.byType(Switch), findsNothing);
        expect(find.text('1 个'), findsOneWidget);
        expect(find.text('暂无可用来源'), findsOneWidget);
        expect(find.text('未开启'), findsNothing);
        expect(find.textContaining('切换行情来源'), findsNothing);
        expect(find.textContaining('服务端'), findsNothing);
        expect(find.textContaining('fixture.invalid'), findsNothing);
        expect(find.textContaining('正在检测'), findsNothing);
        await _tap(tester, find.byKey(const ValueKey('profile-profit-colors')));
        expect(find.text('红涨绿跌'), findsOneWidget);
        expect(tester.takeException(), isNull);
        await _tap(tester, find.byTooltip('关闭'));
        await _tap(tester, find.byKey(const ValueKey('profile-about')));
        expect(find.text('App 版本'), findsOneWidget);
        expect(find.text('GitHub 项目'), findsOneWidget);
        expect(find.text('1.0.0 (2)'), findsOneWidget);
        expect(find.text('1.2.3'), findsNothing);
        expect(find.textContaining('服务端'), findsNothing);
        expect(find.textContaining('fixture.invalid'), findsNothing);
        expect(find.text('https://github.com/GinPonson/Lyra'), findsOneWidget);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('长邮箱在窄屏大字号下自然换行并保留完整身份', (tester) async {
    const longEmail = 'averylonginvestmentidentity@exampleportfolio.com';
    final fixture = _ProfileFixture(
      snapshot: const CredentialSnapshot(
        serverUrl: 'https://fixture.invalid',
        deviceId: 'profile-device',
        accessToken: 'profile-access',
        refreshToken: 'profile-refresh',
        userId: 'profile-user',
        authMethod: 'google',
        userEmail: longEmail,
        googleSubject: 'profile-owner',
      ),
    );
    await _pumpProfile(
      tester,
      fixture,
      size: const Size(320, 740),
      textScale: 2,
    );

    final identity = tester.widget<Text>(
      find.byKey(const ValueKey('profile-identity-name')),
    );
    expect(identity.semanticsLabel, longEmail);
    expect(identity.data, contains('\u200B'));
    expect(identity.data!.replaceAll('\u200B', ''), longEmail);
    expect(
      identity.data!.split('\u200B').every((part) => part.length > 1),
      isTrue,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('关于页的 App 版本读取失败可重试，不查询连接状态', (tester) async {
    final fixture = _ProfileFixture();
    var versionAvailable = false;
    await _pumpProfile(
      tester,
      fixture,
      loadAppVersion: () async {
        if (!versionAvailable) throw StateError('test version unavailable');
        return '1.0.0 (2)';
      },
    );
    await _tap(tester, find.byKey(const ValueKey('profile-about')));
    expect(find.text('暂时无法读取版本'), findsOneWidget);
    expect(find.text('https://github.com/GinPonson/Lyra'), findsOneWidget);
    expect(find.textContaining('服务端'), findsNothing);

    versionAvailable = true;
    await _tap(tester, find.byTooltip('重新读取 App 版本'));
    expect(find.text('1.0.0 (2)'), findsOneWidget);
    expect(find.text('暂时无法读取版本'), findsNothing);
    expect(find.byTooltip('重新读取 App 版本'), findsNothing);
    expect(
      fixture.requests.where(
        (request) =>
            request.uri.path == '/v1/system/info' ||
            request.uri.path == '/v1/health',
      ),
      isEmpty,
    );
  });

  testWidgets('未登录可管理本机偏好，启动账户写入失败可重试', (tester) async {
    final fixture = _ProfileFixture();
    final store = _FailablePreferencesStore(
      initialValue: const AppPreferences(
        rememberedServerUrl: 'https://saved.example.com',
      ),
    );
    await _pumpProfile(
      tester,
      fixture,
      signedIn: false,
      preferencesStore: store,
    );
    expect(find.text('使用 Google 登录'), findsOneWidget);
    store.failWrites = true;
    await _tap(tester, find.byKey(const ValueKey('profile-startup-account')));
    await _tap(tester, find.text('全部证券账户').hitTestable());
    expect(find.text('启动账户未能保存，请重试。'), findsOneWidget);
    expect(
      (await store.read())!.startupAccountMode,
      StartupAccountMode.rememberLast,
    );
    expect(find.textContaining('saved.example.com'), findsNothing);
    expect(find.textContaining('服务端'), findsNothing);
    store.failWrites = false;
    await _tap(tester, find.text('全部证券账户').hitTestable());
    expect((await store.read())!.startupAccountMode, StartupAccountMode.all);
    await _tap(tester, find.byKey(const ValueKey('profile-profit-colors')));
    await _tap(tester, find.text('红涨绿跌').hitTestable());
    expect((await store.read())!.profitColorScheme, ProfitColorScheme.redUp);
    await _tap(tester, find.byKey(const ValueKey('profile-startup-account')));
    expect(find.text('登录后可以从你的证券账户中选择。'), findsOneWidget);
    await _tap(tester, find.text('全部证券账户').hitTestable());
    expect((await store.read())!.startupAccountMode, StartupAccountMode.all);
    expect(await fixture.store.read(), isNull);
    expect(
      fixture.requests.where(
        (request) =>
            request.uri.path.startsWith('/v1/auth/') &&
            request.uri.path != '/v1/auth/providers',
      ),
      isEmpty,
    );
  });

  testWidgets('证券账户入口复用列表和创建，保存后刷新列表', (tester) async {
    final fixture = _ProfileFixture();
    await _pumpProfile(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-accounts')));
    expect(find.text('长期投资'), findsOneWidget);
    await _tap(tester, find.byKey(const ValueKey('profile-add-account')));
    await tester.enterText(find.byKey(const ValueKey('account-name')), '退休投资');
    await tester.enterText(
      find.byKey(const ValueKey('account-identifier')),
      'XXX678',
    );
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(find.text('退休投资'), findsOneWidget);
    expect(fixture.addedAccounts, hasLength(1));
    final creation = fixture.requests.singleWhere(
      (request) =>
          request.uri.path == '/v1/ledger/accounts' && request.method == 'POST',
    );
    expect(creation.headers['idempotency-key'], isNotEmpty);
    expect(creation.data, {
      'displayName': '退休投资',
      'accountIdentifier': 'XXX678',
      'baseCurrency': 'USD',
    });
  });

  testWidgets('证券账户读取失败隐藏后台正文，重新加载恢复列表', (tester) async {
    final fixture = _ProfileFixture()..accountsUnavailable = true;
    await _pumpProfile(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-accounts')));
    expect(find.text('暂时无法读取证券账户，请重新加载。'), findsOneWidget);
    for (final text in [
      'ACCOUNT_READ_FAILED',
      'DuckDB',
      '/internal',
      'request_id',
    ]) {
      expect(find.textContaining(text), findsNothing);
    }
    fixture.accountsUnavailable = false;
    await _tap(tester, find.text('重新加载账户'));
    expect(find.text('长期投资'), findsOneWidget);
    expect(find.text('暂时无法读取证券账户，请重新加载。'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('启动账户指定只保存当前身份，取消选择不改变设置', (tester) async {
    final fixture = _ProfileFixture();
    final store = MemoryAppPreferencesStore();
    await _pumpProfile(tester, fixture, preferencesStore: store);
    await _tap(tester, find.byKey(const ValueKey('profile-startup-account')));
    expect(find.text('下次启动时生效，当前查看范围保持不变。'), findsOneWidget);
    expect(find.textContaining('服务端'), findsNothing);
    await _tap(tester, find.text('长期投资').hitTestable());
    final saved = (await store.read())!;
    expect(saved.startupAccountMode, StartupAccountMode.specific);
    expect(
      saved.forIdentity(preferencesIdentityKey(_snapshot)).specificAccountId,
      LedgerFixture.account,
    );
    await _tap(tester, find.byKey(const ValueKey('profile-startup-account')));
    await _tap(tester, find.byTooltip('关闭'));
    expect((await store.read())!.toJson(), saved.toJson());
  });

  testWidgets('离线保留登录和本机偏好，不显示连接检测信息', (tester) async {
    final fixture = _ProfileFixture()..offline = true;
    final store = MemoryAppPreferencesStore();
    final container = await _pumpProfile(
      tester,
      fixture,
      preferencesStore: store,
    );
    // 有界传输恢复等待期间没有动画帧；等待两项读取实际失败，再核对离线状态。
    for (
      var attempt = 0;
      attempt < 40 &&
          (find.text('暂时无法读取账户').evaluate().isEmpty ||
              find.text('行情来源读取失败').evaluate().isEmpty);
      attempt++
    ) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    final startupAccountModeBefore = container
        .read(appPreferencesProvider)
        .value
        ?.startupAccountMode;
    expect(startupAccountModeBefore, isNotNull);
    expect(find.text('Google'), findsOneWidget);
    expect(find.text('登录 Lyra'), findsNothing);
    expect(find.text('暂时无法读取账户'), findsOneWidget);
    expect(find.text('行情来源读取失败'), findsOneWidget);
    expect(find.textContaining('服务端'), findsNothing);
    expect(find.textContaining('fixture.invalid'), findsNothing);
    expect(find.textContaining('检测'), findsNothing);
    // 离线状态不应改变页面已加载的本机启动账户偏好。
    expect(
      container.read(appPreferencesProvider).value?.startupAccountMode,
      startupAccountModeBefore,
    );
    expect((await fixture.store.read())!.deviceId, _snapshot.deviceId);
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/logout',
      ),
      isEmpty,
    );
    expect(
      fixture.requests.where(
        (request) =>
            request.uri.path == '/v1/system/info' ||
            request.uri.path == '/v1/health',
      ),
      isEmpty,
    );
  });

  testWidgets('退出先确认，可取消；退出后本机偏好保留', (tester) async {
    final fixture = _ProfileFixture();
    final preferences = MemoryAppPreferencesStore(
      initialValue: const AppPreferences(
        hideAmountsOnStartup: true,
        profitColorScheme: ProfitColorScheme.redUp,
      ),
    );
    await _pumpProfile(tester, fixture, preferencesStore: preferences);
    await _tap(tester, find.byKey(const ValueKey('profile-sign-out')));
    expect(find.text('退出此设备登录？'), findsOneWidget);
    await _tap(tester, find.text('取消').hitTestable());
    expect((await fixture.store.read())!.deviceId, _snapshot.deviceId);
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/logout',
      ),
      isEmpty,
    );
    await _tap(tester, find.byKey(const ValueKey('profile-sign-out')));
    await _tap(tester, find.widgetWithText(FilledButton, '退出登录'));
    expect(await fixture.store.read(), isNull);
    expect((await preferences.read())!.hideAmountsOnStartup, isTrue);
    expect(
      (await preferences.read())!.profitColorScheme,
      ProfitColorScheme.redUp,
    );
    expect(
      (await preferences.read())!.rememberedServerUrl,
      _snapshot.serverUrl,
    );
    expect(find.text('使用 Google 登录'), findsOneWidget);
    expect(find.textContaining('fixture.invalid'), findsNothing);
  });

  testWidgets('退出确认期间身份变化，不退出后来登录的身份', (tester) async {
    final fixture = _ProfileFixture();
    final container = await _pumpProfile(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-sign-out')));
    await _replaceIdentity(tester, container, fixture);
    final existingLogouts = fixture.requests
        .where((request) => request.uri.path == '/v1/auth/logout')
        .length;
    await _tap(tester, find.widgetWithText(FilledButton, '退出登录'));
    expect(find.text('登录状态已变化，请关闭后重试。'), findsOneWidget);
    expect((await fixture.store.read())!.deviceId, 'different-device');
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/logout',
      ),
      hasLength(existingLogouts),
    );
  });

  testWidgets('离线时仍可退出此设备，并完成退出提示', (tester) async {
    final fixture = _ProfileFixture()..logoutOffline = true;
    await _pumpProfile(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-sign-out')));
    await _tap(tester, find.widgetWithText(FilledButton, '退出登录'));
    expect(await fixture.store.read(), isNull);
    expect(
      find.descendant(
        of: find.byType(AlertDialog),
        matching: find.textContaining('已退出此设备'),
      ),
      findsOneWidget,
    );
    expect(find.textContaining('服务端'), findsNothing);
    await _tap(tester, find.text('完成'));
    expect(find.text('使用 Google 登录'), findsOneWidget);
  });
}
