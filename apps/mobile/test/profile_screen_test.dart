import 'dart:async';
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
import 'package:lyra_mobile/features/overview/login_panel.dart';
import 'package:lyra_mobile/features/profile/profile_screen.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;
import 'support/ledger_fixture.dart';

const _snapshot = CredentialSnapshot(
  serverUrl: 'https://fixture.invalid',
  deviceId: 'profile-device',
  accessToken: 'profile-access',
  refreshToken: 'profile-refresh',
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

class _ProfileFixture extends LedgerFixture {
  final store = MemoryCredentialStore();
  final openedUrls = <Uri>[];
  bool offline = false;
  bool logoutOffline = false;
  bool pendingLogin = false;
  Completer<ResponseBody>? delayedAccounts;
  final addedAccounts = <Map<String, Object?>>[];

  Dio dio(String url) =>
      Dio(BaseOptions(baseUrl: url))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests.add(options);
          final path = options.uri.path;
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
            if (pendingLogin) return _json({'status': 'pending'}, 202);
            return _json({
              'status': 'complete',
              'user': {
                'provider': 'google',
                'subject': 'new-owner',
                'email': 'new@example.com',
                'displayName': 'New user',
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
              'protocol_version': 2,
              'min_supported_protocol_version': 2,
              'max_supported_protocol_version': 2,
              'schema_version': 4,
            });
          }
          if (path == '/v1/health') return _json({'status': 'healthy'});
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
          if (path == '/v1/ledger/accounts' && delayedAccounts != null) {
            return delayedAccounts!.future;
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
    if (paired) await store.write(_snapshot);
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

class _DeferredPreferencesStore extends MemoryAppPreferencesStore {
  _DeferredPreferencesStore({super.initialValue});
  final writeStarted = Completer<void>();
  final releaseWrite = Completer<void>();
  int writes = 0;

  @override
  Future<void> write(AppPreferences preferences) async {
    if (++writes == 1) {
      writeStarted.complete();
      await releaseWrite.future;
    }
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
  Widget page = const ProfileScreen(),
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
        appVersionProvider.overrideWith((ref) async => '1.0.0 (2)'),
      ],
      child: MaterialApp(
        theme: LyraTheme.dark,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: Scaffold(body: page),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return ProviderScope.containerOf(
    tester.element(find.byType(page.runtimeType)),
  );
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
  _ProfileFixture fixture, {
  bool settle = true,
}) async {
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
      authMethod: 'google',
      userEmail: 'different@example.com',
      googleSubject: 'different-owner',
    ),
  );
  container.invalidate(sessionProvider);
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    await tester.pump(const Duration(milliseconds: 100));
  }
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
        expect(find.text('Google 已登录'), findsOneWidget);
        expect(find.text('Lyra Mobile'), findsNothing);
        expect(find.text('启动时隐藏金额'), findsOneWidget);
        await _tap(tester, find.byKey(const ValueKey('profile-profit-colors')));
        expect(find.text('红涨绿跌'), findsOneWidget);
        expect(tester.takeException(), isNull);
        await _tap(tester, find.byTooltip('关闭'));
        await _tap(tester, find.byKey(const ValueKey('profile-about')));
        expect(find.text('1.0.0 (2)'), findsOneWidget);
        expect(find.text('1.2.3'), findsOneWidget);
        expect(find.text('https://github.com/GinPonson/Lyra'), findsOneWidget);
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets('未登录可保存本机偏好，金额开关失败可重试且不假报成功', (tester) async {
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
    await _tap(tester, find.byKey(const ValueKey('startup-hide-amounts')));
    expect(
      tester
          .widget<Switch>(find.byKey(const ValueKey('startup-hide-amounts')))
          .value,
      isFalse,
    );
    expect(find.text('隐藏金额设置未能保存，请重试。'), findsOneWidget);
    expect((await store.read())!.hideAmountsOnStartup, isFalse);
    expect(find.text('saved.example.com'), findsOneWidget);
    expect(find.textContaining('saved.example.com ·'), findsOneWidget);
    store.failWrites = false;
    await _tap(tester, find.byKey(const ValueKey('startup-hide-amounts')));
    expect((await store.read())!.hideAmountsOnStartup, isTrue);
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

  testWidgets('启动账户指定只保存当前身份，取消选择不改变设置', (tester) async {
    final fixture = _ProfileFixture();
    final store = MemoryAppPreferencesStore();
    await _pumpProfile(tester, fixture, preferencesStore: store);
    await _tap(tester, find.byKey(const ValueKey('profile-startup-account')));
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

  testWidgets('离线保留 Google 登录和本机设置，检测恢复不会退出登录', (tester) async {
    final fixture = _ProfileFixture()..offline = true;
    final store = MemoryAppPreferencesStore();
    await _pumpProfile(tester, fixture, preferencesStore: store);
    expect(find.text('Google 已登录'), findsOneWidget);
    expect(find.text('登录 Lyra'), findsNothing);
    expect(find.textContaining('暂时无法连接，可重新检测'), findsOneWidget);
    await _tap(tester, find.byKey(const ValueKey('startup-hide-amounts')));
    expect((await store.read())!.hideAmountsOnStartup, isTrue);
    await _tap(tester, find.byKey(const ValueKey('profile-connection')));
    fixture.offline = false;
    await _tap(tester, find.text('重新检测连接').hitTestable());
    expect(find.text('连接正常').hitTestable(), findsOneWidget);
    expect((await fixture.store.read())!.deviceId, _snapshot.deviceId);
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/logout',
      ),
      isEmpty,
    );
  });

  testWidgets('退出先确认，可取消；退出后本机偏好与服务端地址保留', (tester) async {
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

  testWidgets('保存服务地址无需登录；非 HTTPS 远程地址被拒绝', (tester) async {
    final fixture = _ProfileFixture();
    final preferences = MemoryAppPreferencesStore();
    await _pumpProfile(
      tester,
      fixture,
      signedIn: false,
      preferencesStore: preferences,
    );
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'http://remote.example.com',
    );
    await _tap(tester, find.text('连接服务端'));
    expect(find.text('请输入 HTTPS 服务端地址（本机开发可使用 HTTP）。'), findsOneWidget);
    expect(await preferences.read(), isNull);
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'https://saved.example.com/',
    );
    await _tap(tester, find.text('连接服务端'));
    expect(
      (await preferences.read())!.rememberedServerUrl,
      'https://saved.example.com',
    );
    expect(fixture.openedUrls, isEmpty);
    expect(await fixture.store.read(), isNull);
  });

  testWidgets('更换服务端取消不改登录；确认后重新选择身份且旧令牌不发往新服务端', (tester) async {
    final fixture = _ProfileFixture();
    final preferences = MemoryAppPreferencesStore();
    await _pumpProfile(
      tester,
      fixture,
      preferencesStore: preferences,
      page: const SingleChildScrollView(child: LoginPanel()),
    );
    await _tap(tester, find.text('更换服务端'));
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'https://new.example.com/',
    );
    await _tap(tester, find.byKey(const ValueKey('login-change-server')));
    await _tap(tester, find.text('取消').hitTestable());
    expect((await fixture.store.read())!.serverUrl, _snapshot.serverUrl);
    await _tap(tester, find.byKey(const ValueKey('login-change-server')));
    await _tap(tester, find.widgetWithText(FilledButton, '更换并前往登录').last);
    expect(await fixture.store.read(), isNull);
    expect(fixture.openedUrls, isEmpty);
    await _tap(tester, find.byKey(const ValueKey('login-google')));
    final saved = (await fixture.store.read())!;
    expect(saved.serverUrl, 'https://new.example.com');
    expect(saved.deviceId, 'new-device');
    expect(saved.accessToken, 'new-access');
    expect(saved.googleSubject, 'new-owner');
    expect(
      (await preferences.read())!.rememberedServerUrl,
      'https://new.example.com',
    );
    expect(fixture.openedUrls, hasLength(1));
    final logout = fixture.requests.singleWhere(
      (request) => request.uri.path == '/v1/auth/logout',
    );
    expect(logout.uri.host, 'fixture.invalid');
    expect(logout.headers['authorization'], 'Bearer profile-access');
    final starts = fixture.requests.where(
      (request) => request.uri.path == '/v1/auth/google/start',
    );
    expect(starts.single.uri.host, 'new.example.com');
    for (final request in fixture.requests.where(
      (request) => request.uri.host == 'new.example.com',
    )) {
      expect(request.headers['authorization'], isNot('Bearer profile-access'));
      expect(request.headers['authorization'], isNot('Bearer profile-refresh'));
    }
  });

  testWidgets('更换服务端的地址保存失败保留原登录，可以重试', (tester) async {
    final fixture = _ProfileFixture();
    final preferences = _FailablePreferencesStore();
    await _pumpProfile(
      tester,
      fixture,
      preferencesStore: preferences,
      page: const SingleChildScrollView(child: LoginPanel()),
    );
    await _tap(tester, find.text('更换服务端'));
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'https://new.example.com',
    );
    preferences.failWrites = true;
    await _tap(tester, find.byKey(const ValueKey('login-change-server')));
    await _tap(tester, find.widgetWithText(FilledButton, '更换并前往登录').last);
    expect((await fixture.store.read())!.deviceId, _snapshot.deviceId);
    expect(
      fixture.requests.where(
        (request) =>
            request.uri.path.startsWith('/v1/auth/') &&
            request.uri.path != '/v1/auth/providers',
      ),
      isEmpty,
    );
    expect(find.text('已登录 Lyra'), findsOneWidget);
    preferences.failWrites = false;
    await _tap(tester, find.byKey(const ValueKey('login-change-server')));
    await _tap(tester, find.widgetWithText(FilledButton, '更换并前往登录').last);
    expect(await fixture.store.read(), isNull);
    await _tap(tester, find.byKey(const ValueKey('login-google')));
    expect((await fixture.store.read())!.serverUrl, 'https://new.example.com');
  });

  testWidgets('更换服务端保存期间身份改变，迟到保存不退出新身份或发起旧登录', (tester) async {
    final fixture = _ProfileFixture();
    final preferences = _DeferredPreferencesStore();
    final container = await _pumpProfile(
      tester,
      fixture,
      preferencesStore: preferences,
      page: const SingleChildScrollView(child: LoginPanel()),
    );
    await _tap(tester, find.text('更换服务端'));
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'https://new.example.com',
    );
    await _tap(tester, find.byKey(const ValueKey('login-change-server')));
    await tester.tap(find.widgetWithText(FilledButton, '更换并前往登录').last);
    await tester.pump(const Duration(milliseconds: 100));
    expect(preferences.writeStarted.isCompleted, isTrue);
    await _replaceIdentity(tester, container, fixture, settle: false);
    preferences.releaseWrite.complete();
    await tester.pumpAndSettle();
    expect((await fixture.store.read())!.deviceId, 'different-device');
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/logout',
      ),
      hasLength(1),
    );
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/google/start',
      ),
      isEmpty,
    );
    expect(find.text('登录状态已变化，已停止此次更换。'), findsOneWidget);
    expect(find.byKey(const ValueKey('login-change-server')), findsNothing);
  });

  testWidgets('取消等待中的地址保存后重新登录，旧尝试完成不会取消新尝试', (tester) async {
    final fixture = _ProfileFixture();
    final preferences = _DeferredPreferencesStore(
      initialValue: const AppPreferences(
        rememberedServerUrl: 'https://new.example.com',
      ),
    );
    await _pumpProfile(
      tester,
      fixture,
      signedIn: false,
      preferencesStore: preferences,
      page: const SingleChildScrollView(child: LoginPanel()),
    );
    await tester.tap(find.text('使用 Google 登录'));
    await tester.pump(const Duration(milliseconds: 100));
    expect(preferences.writeStarted.isCompleted, isTrue);
    await tester.tap(find.text('取消登录'));
    await tester.pump();
    await tester.tap(find.text('使用 Google 登录'));
    await tester.pump(const Duration(milliseconds: 100));
    preferences.releaseWrite.complete();
    await tester.pumpAndSettle();
    expect((await fixture.store.read())!.deviceId, 'new-device');
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/google/start',
      ),
      hasLength(1),
    );
    expect(fixture.openedUrls, hasLength(1));
  });

  testWidgets('服务端离线时显式退出仍清除本机凭据，并说明撤销未完成', (tester) async {
    final fixture = _ProfileFixture()..logoutOffline = true;
    await _pumpProfile(tester, fixture);
    await _tap(tester, find.byKey(const ValueKey('profile-sign-out')));
    await _tap(tester, find.widgetWithText(FilledButton, '退出登录'));
    expect(await fixture.store.read(), isNull);
    expect(
      find.descendant(
        of: find.byType(AlertDialog),
        matching: find.text('已退出此设备登录。服务端暂时无法撤销会话，账本仍保留在服务端。'),
      ),
      findsOneWidget,
    );
    await _tap(tester, find.text('完成'));
    expect(find.text('使用 Google 登录'), findsOneWidget);
  });
}
