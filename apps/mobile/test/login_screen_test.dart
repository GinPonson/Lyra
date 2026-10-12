import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';
import 'package:lyra_mobile/features/auth/auth_workspace.dart';
import 'package:lyra_mobile/features/auth/login_screen.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'support/login_fixture.dart';

Future<ProviderContainer> pumpLogin(
  WidgetTester tester,
  LoginFixture fixture, {
  bool signedIn = false,
  String configuredServer = '',
  String? server = loginFixtureServer,
  Size size = const Size(390, 844),
  double scale = 1,
  AppPreferencesStore? preferences,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.platformDispatcher.textScaleFactorTestValue = scale;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
  final dependencies = await fixture.dependencies(paired: signedIn);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        configuredServerUrlProvider.overrideWithValue(configuredServer),
        sessionDependenciesProvider.overrideWithValue(dependencies),
        appPreferencesStoreProvider.overrideWithValue(
          preferences ??
              MemoryAppPreferencesStore(
                initialValue: AppPreferences(rememberedServerUrl: server),
              ),
        ),
        systemDioFactoryProvider.overrideWithValue(fixture.dio),
      ],
      child: const LyraApp(),
    ),
  );
  await tester.pumpAndSettle();
  return ProviderScope.containerOf(tester.element(find.byType(LyraApp)));
}

Future<void> tapLogin(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pumpAndSettle();
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('本机清理失败在新登录页可见并可重试，清理完成前禁用登录', (tester) async {
    final fixture = LoginFixture();
    final container = await pumpLogin(tester, fixture, signedIn: true);
    fixture.store.failClear = true;
    final logout = expectLater(
      container.read(sessionProvider.notifier).signOut(),
      throwsA(isA<LyraApiError>()),
    );
    await tester.pumpAndSettle();
    await logout;
    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.text('无法清除本机登录，请重试。'), findsOneWidget);
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
          .onPressed,
      isNull,
    );
    fixture.store.failClear = false;
    await tapLogin(tester, find.byKey(const ValueKey('login-retry-sign-out')));
    expect(await fixture.store.read(), isNull);
    expect(find.text('无法清除本机登录，请重试。'), findsNothing);
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets('登录方式读取尚未完成时仍可立即重试清除本机登录', (tester) async {
    final fixture = LoginFixture();
    final container = await pumpLogin(tester, fixture, signedIn: true);
    fixture.catalogGate = Completer<ResponseBody>();
    fixture.store.failClear = true;
    final logout = expectLater(
      container.read(sessionProvider.notifier).signOut(),
      throwsA(isA<LyraApiError>()),
    );
    await tester.pumpAndSettle();
    await logout;
    fixture.store.failClear = false;
    final retry = find.byKey(const ValueKey('login-retry-sign-out'));
    expect(tester.widget<TextButton>(retry).onPressed, isNotNull);
    await tapLogin(tester, retry);
    expect(await fixture.store.read(), isNull);
    expect(find.text('无法清除本机登录，请重试。'), findsNothing);
    fixture.catalogGate!.complete(LoginFixture.json(fixture.catalog()));
    await tester.pumpAndSettle();
  });

  testWidgets('远端退出失败随身份返回登录页后仍有提示，本机凭据已清除', (tester) async {
    final fixture = LoginFixture();
    final container = await pumpLogin(tester, fixture, signedIn: true);
    fixture.logoutOffline = true;
    final logout = expectLater(
      container.read(sessionProvider.notifier).signOut(),
      throwsA(isA<LyraApiError>()),
    );
    await tester.pumpAndSettle();
    await logout;
    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.text('已退出此设备登录，但暂时无法完成全部退出操作。'), findsOneWidget);
    expect(await fixture.store.read(), isNull);
  });

  for (final scenario in [
    (size: const Size(320, 740), scale: 1.6),
    (size: const Size(390, 844), scale: 1.0),
    (size: const Size(1440, 960), scale: 1.0),
  ]) {
    testWidgets(
      '独立登录页 ${scenario.size.width} / ${scenario.scale} 展示三种身份、无财务导航且无溢出',
      (tester) async {
        final fixture = LoginFixture();
        await pumpLogin(
          tester,
          fixture,
          size: scenario.size,
          scale: scenario.scale,
        );
        expect(find.byType(LoginScreen), findsOneWidget);
        expect(find.byType(NavigationBar), findsNothing);
        expect(find.byType(TextField), findsNothing);
        expect(find.textContaining('服务端'), findsNothing);
        expect(find.textContaining('demo.lyra.example'), findsNothing);
        for (final provider in AuthProvider.values) {
          final button = find.byKey(ValueKey('login-${provider.id}'));
          await tester.ensureVisible(button);
          expect(tester.widget<OutlinedButton>(button).onPressed, isNotNull);
          expect(find.text('使用 ${provider.label} 登录'), findsOneWidget);
        }
        expect(tester.takeException(), isNull);
        expect(
          fixture.requests.where(
            (request) => request.uri.path.startsWith('/v1/ledger'),
          ),
          isEmpty,
        );
      },
    );
  }

  for (final provider in AuthProvider.values) {
    testWidgets('${provider.label} 浏览器授权后进入工作台并保留稳定身份，允许没有邮箱', (tester) async {
      final fixture = LoginFixture();
      final container = await pumpLogin(tester, fixture);
      await tapLogin(tester, find.byKey(ValueKey('login-${provider.id}')));
      expect(find.byType(LoginScreen), findsNothing);
      expect(find.byType(NavigationBar), findsOneWidget);
      final saved = (await fixture.store.read())!;
      expect(saved.authMethod, provider.id);
      expect(saved.subject, 'subject-${provider.id}');
      expect(saved.userDisplayName, 'Demo ${provider.label}');
      expect(
        saved.userEmail,
        provider == AuthProvider.google ? 'demo@example.com' : null,
      );
      expect(fixture.openedUrls.single.host, provider.authorizationHost);
      final start = fixture.requests.singleWhere(
        (r) => r.uri.path == '/v1/auth/${provider.id}/start',
      );
      final exchange = fixture.requests.singleWhere(
        (r) => r.uri.path == '/v1/auth/${provider.id}/exchange',
      );
      expect(start.headers['authorization'], isNull);
      expect(exchange.headers['authorization'], isNull);
      expect((start.data as Map)['codeChallengeMethod'], 'S256');
      expect((start.data as Map).containsKey('codeVerifier'), isFalse);
      expect((exchange.data as Map)['codeVerifier'], isNotEmpty);
      expect(
        container.read(sessionProvider).value,
        isA<SessionAuthenticated>(),
      );
      await tapLogin(tester, find.text('我的'));
      expect(find.text('${provider.label} 已登录'), findsOneWidget);
    });
  }

  testWidgets('不可用的登录方式禁用并自动重查，不暴露配置状态', (tester) async {
    final fixture = LoginFixture()..available = {AuthProvider.github};
    await pumpLogin(tester, fixture);
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-linuxdo')))
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-github')))
          .onPressed,
      isNotNull,
    );
    // 三种方式样式统一：只有居中的名称文字，不再有右侧状态标签。
    for (final item in AuthProvider.values) {
      expect(find.text('使用 ${item.label} 登录'), findsOneWidget);
    }
    expect(find.text('暂不可用'), findsNothing);
    expect(fixture.openedUrls, isEmpty);
    expect(find.byKey(const ValueKey('login-reload-providers')), findsNothing);
    fixture.available = AuthProvider.values.toSet();
    // 页面不再提供刷新按钮：目录不可用时按退避自行重查并恢复。
    await tester.pump(const Duration(seconds: 2));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-linuxdo')))
          .onPressed,
      isNotNull,
    );
    // 恢复后三种方式都可点，且依旧没有额外状态文字。
    for (final item in AuthProvider.values) {
      expect(
        tester
            .widget<OutlinedButton>(find.byKey(ValueKey('login-${item.id}')))
            .onPressed,
        isNotNull,
      );
    }
    expect(find.text('暂不可用'), findsNothing);
  });

  testWidgets('登录方式尚在读取时禁止登录，迟到响应不伪装成功', (tester) async {
    final fixture = LoginFixture()..catalogGate = Completer<ResponseBody>();
    await pumpLogin(tester, fixture);
    for (final provider in AuthProvider.values) {
      expect(
        tester
            .widget<OutlinedButton>(
              find.byKey(ValueKey('login-${provider.id}')),
            )
            .onPressed,
        isNull,
      );
    }
    expect(find.text('正在准备登录…'), findsOneWidget);
    fixture.catalogGate!.complete(LoginFixture.json(fixture.catalog()));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
          .onPressed,
      isNotNull,
    );
    expect(fixture.openedUrls, isEmpty);
  });

  testWidgets('首次安装使用预设地址直接登录，无需填写连接信息', (tester) async {
    final fixture = LoginFixture();
    final preferences = MemoryAppPreferencesStore();
    await pumpLogin(
      tester,
      fixture,
      server: null,
      configuredServer: loginFixtureServer,
      preferences: preferences,
    );
    expect(find.byType(TextField), findsNothing);
    expect(find.textContaining('服务端'), findsNothing);
    expect(fixture.requests.single.uri.host, 'demo.lyra.example');
    await tapLogin(tester, find.byKey(const ValueKey('login-google')));
    expect(find.byType(LoginScreen), findsNothing);
    expect((await fixture.store.read())!.serverUrl, loginFixtureServer);
    expect((await preferences.read())!.rememberedServerUrl, loginFixtureServer);
  });

  for (final configured in ['', 'http://remote.example.com']) {
    testWidgets('后台地址缺失或无效时不发请求、不要求用户配置：$configured', (tester) async {
      final fixture = LoginFixture();
      await pumpLogin(
        tester,
        fixture,
        server: null,
        configuredServer: configured,
      );
      expect(find.byType(TextField), findsNothing);
      expect(find.textContaining('服务端'), findsNothing);
      expect(find.textContaining('http'), findsNothing);
      expect(find.text('登录服务暂时不可用。'), findsOneWidget);
      for (final provider in AuthProvider.values) {
        expect(
          tester
              .widget<OutlinedButton>(
                find.byKey(ValueKey('login-${provider.id}')),
              )
              .onPressed,
          isNull,
        );
      }
      // 提示只作为一次性提示出现，页面上不再常驻状态行与刷新按钮。
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      expect(find.text('登录服务暂时不可用。'), findsNothing);
      expect(find.byKey(const ValueKey('login-reload-providers')), findsNothing);
      expect(fixture.requests, isEmpty);
      expect(fixture.openedUrls, isEmpty);
    });
  }
  testWidgets('等待读取旧地址后才发现登录方式，不短暂请求构建默认后台', (tester) async {
    final fixture = LoginFixture();
    final preferences = _DelayedPreferencesStore();
    final container = await pumpLogin(
      tester,
      fixture,
      configuredServer: 'https://new.lyra.example',
      preferences: preferences,
    );
    expect(find.text('正在准备登录…'), findsOneWidget);
    expect(fixture.requests, isEmpty);
    preferences.gate.complete(
      const AppPreferences(rememberedServerUrl: loginFixtureServer),
    );
    await container.read(appPreferencesProvider.future);
    await tester.pump();
    await tester.pumpAndSettle();
    expect(fixture.requests.single.uri.host, 'demo.lyra.example');
    expect(find.textContaining('demo.lyra.example'), findsNothing);
  });

  for (final status in [404, 503]) {
    testWidgets('登录方式查询 $status 只给出用户提示，自动重查恢复后可登录', (tester) async {
      final fixture = LoginFixture()..catalogStatus = status;
      await pumpLogin(tester, fixture);
      expect(find.text('登录服务暂时不可用。'), findsOneWidget);
      expect(
        tester
            .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
            .onPressed,
        isNull,
      );
      fixture.catalogStatus = null;
      // 服务端瞬时失败由页面自行退避重查，用户不需要点任何刷新按钮。
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
      expect(
        tester
            .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
            .onPressed,
        isNotNull,
      );
    });
  }

  testWidgets('等待授权时禁止重复请求，取消停止轮询后可换另一种身份', (tester) async {
    final fixture = LoginFixture()..pending = true;
    await pumpLogin(tester, fixture);
    await tapLogin(tester, find.byKey(const ValueKey('login-linuxdo')));
    expect(find.text('等待 Linux.do 授权…'), findsOneWidget);
    for (final provider in AuthProvider.values) {
      expect(
        tester
            .widget<OutlinedButton>(
              find.byKey(ValueKey('login-${provider.id}')),
            )
            .onPressed,
        isNull,
      );
    }
    await tapLogin(tester, find.text('取消登录'));
    final count = fixture.requests.length;
    await tester.pump(const Duration(seconds: 5));
    expect(fixture.requests.length, count);
    expect(await fixture.store.read(), isNull);
    fixture.pending = false;
    await tapLogin(tester, find.byKey(const ValueKey('login-github')));
    expect((await fixture.store.read())!.authMethod, 'github');
  });

  testWidgets('拒绝授权或无法打开浏览器时保留登录页，可以重试', (tester) async {
    final fixture = LoginFixture()..browserAvailable = false;
    await pumpLogin(tester, fixture);
    await tapLogin(tester, find.byKey(const ValueKey('login-google')));
    expect(find.text('无法打开浏览器，请重试。'), findsOneWidget);
    expect(await fixture.store.read(), isNull);
    fixture.browserAvailable = true;
    fixture.loginError = 'AUTH_FORBIDDEN';
    await tapLogin(tester, find.byKey(const ValueKey('login-google')));
    expect(find.text('此账号暂时无法登录 Lyra，请换一个账号。'), findsOneWidget);
    expect(find.textContaining('服务端'), findsNothing);
    expect(await fixture.store.read(), isNull);
    fixture.loginError = null;
    await tapLogin(tester, find.byKey(const ValueKey('login-google')));
    expect(find.byType(LoginScreen), findsNothing);
  });

  testWidgets('错误第三方授权地址与串线身份不会打开浏览器或写入凭据', (tester) async {
    final fixture = LoginFixture()
      ..authorizationUrlOverride =
          'https://github.com/login/oauth/authorize?state=mixed';
    await pumpLogin(tester, fixture);
    await tapLogin(tester, find.byKey(const ValueKey('login-google')));
    expect(fixture.openedUrls, isEmpty);
    expect(await fixture.store.read(), isNull);
    fixture.authorizationUrlOverride = null;
    fixture.exchangedProvider = AuthProvider.linuxdo;
    await tapLogin(tester, find.byKey(const ValueKey('login-github')));
    expect(await fixture.store.read(), isNull);
    expect(find.byType(LoginScreen), findsOneWidget);
  });

  testWidgets('退出清理二级路由并回独立登录页，旧 Google 凭据仍能恢复', (tester) async {
    final fixture = LoginFixture();
    final container = await pumpLogin(tester, fixture, signedIn: true);
    expect(find.byType(LoginScreen), findsNothing);
    await tapLogin(tester, find.text('我的'));
    await tapLogin(tester, find.byKey(const ValueKey('profile-accounts')));
    expect(find.text('证券账户'), findsWidgets);
    final operation = container.read(sessionProvider.notifier).signOut();
    await tester.pumpAndSettle();
    await operation;
    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(BackButton), findsNothing);
    expect(await fixture.store.read(), isNull);
  });

  testWidgets('旧凭据退出后自动回原账本登录，构建默认地址不接收旧令牌', (tester) async {
    final fixture = LoginFixture();
    final preferences = MemoryAppPreferencesStore();
    await pumpLogin(
      tester,
      fixture,
      signedIn: true,
      configuredServer: 'https://new.lyra.example',
      preferences: preferences,
    );
    await tapLogin(tester, find.text('我的'));
    expect(find.byKey(const ValueKey('profile-connection')), findsNothing);
    await tapLogin(tester, find.byKey(const ValueKey('profile-sign-out')));
    await tapLogin(tester, find.widgetWithText(FilledButton, '退出登录'));
    expect(find.byType(LoginScreen), findsOneWidget);
    expect(await fixture.store.read(), isNull);
    expect((await preferences.read())!.rememberedServerUrl, loginFixtureServer);
    expect(
      fixture.requests.every((r) => r.uri.host == 'demo.lyra.example'),
      isTrue,
    );
    final logout = fixture.requests.singleWhere(
      (r) => r.uri.path == '/v1/auth/logout',
    );
    expect(logout.headers['authorization'], 'Bearer restored-access');
    await tapLogin(tester, find.byKey(const ValueKey('login-linuxdo')));
    expect((await fixture.store.read())!.serverUrl, loginFixtureServer);
    expect((await fixture.store.read())!.authMethod, 'linuxdo');
    expect(
      fixture.requests.where((r) => r.uri.host == 'new.lyra.example'),
      isEmpty,
    );
  });

  for (final duringRestore in [true, false]) {
    testWidgets('旧凭据${duringRestore ? '启动恢复' : '账本刷新'}过期后回原地址，重启和重新登录不转发旧令牌', (
      tester,
    ) async {
      final fixture = LoginFixture()..credentialsExpired = duringRestore;
      final preferences = _ObservedPreferencesStore();
      final container = await pumpLogin(
        tester,
        fixture,
        signedIn: true,
        configuredServer: 'https://new.lyra.example',
        preferences: preferences,
      );
      if (!duringRestore) {
        expect(
          container.read(sessionProvider).value,
          isA<SessionAuthenticated>(),
        );
        fixture.credentialsExpired = true;
        final refresh = container
            .read(sessionProvider.notifier)
            .refreshLedger();
        await tester.pumpAndSettle();
        expect(await refresh, isFalse);
      }
      await preferences.firstWrite.future;
      await tester.pumpAndSettle();
      final session = container.read(sessionProvider).value as SessionSignedOut;
      expect(session.serverUrl, loginFixtureServer);
      expect(container.read(sessionProvider.notifier).client, isNull);
      expect(await fixture.store.read(), isNull);
      expect(
        (await preferences.read())!.rememberedServerUrl,
        loginFixtureServer,
      );
      expect(find.byType(LoginScreen), findsOneWidget);
      expect(find.text('登录已过期，请重新登录。'), findsOneWidget);
      expect(
        fixture.requests.every(
          (request) => request.uri.host == 'demo.lyra.example',
        ),
        isTrue,
      );
      final refresh = fixture.requests.singleWhere(
        (request) => request.uri.path == '/v1/devices/tokens',
      );
      expect((refresh.data as Map)['refreshToken'], 'restored-refresh');
      final directory = fixture.requests.singleWhere(
        (request) => request.uri.path == '/v1/auth/providers',
      );
      expect(directory.headers['authorization'], isNull);

      // 销毁整个容器模拟重启；仅靠持久化偏好恢复地址，不复用临时会话提示。
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      fixture.requests.clear();
      await pumpLogin(
        tester,
        fixture,
        configuredServer: 'https://new.lyra.example',
        preferences: preferences,
      );
      expect(fixture.requests.single.uri.host, 'demo.lyra.example');
      expect(fixture.requests.single.uri.path, '/v1/auth/providers');
      expect(fixture.requests.single.headers['authorization'], isNull);
      fixture.credentialsExpired = false;
      await tapLogin(tester, find.byKey(const ValueKey('login-google')));
      expect((await fixture.store.read())!.serverUrl, loginFixtureServer);
      final start = fixture.requests.singleWhere(
        (request) => request.uri.path == '/v1/auth/google/start',
      );
      expect(start.headers['authorization'], isNull);
      expect(
        fixture.requests.where(
          (request) => request.uri.host == 'new.lyra.example',
        ),
        isEmpty,
      );
    });
  }

  testWidgets('过期后的地址写入等待或失败不阻塞凭据清理，仍可在原地址重试登录', (tester) async {
    final fixture = LoginFixture();
    final preferences = _GatedPreferencesStore()..failWrites = true;
    final container = await pumpLogin(
      tester,
      fixture,
      signedIn: true,
      configuredServer: 'https://new.lyra.example',
      preferences: preferences,
    );
    fixture.credentialsExpired = true;
    final refresh = container.read(sessionProvider.notifier).refreshLedger();
    await tester.pumpAndSettle();
    await preferences.writeStarted.future;
    expect(await fixture.store.read(), isNull);
    expect(container.read(sessionProvider.notifier).client, isNull);
    expect(
      (container.read(sessionProvider).value as SessionSignedOut).serverUrl,
      loginFixtureServer,
    );
    expect(find.byType(LoginScreen), findsOneWidget);
    preferences.writeGate.complete();
    expect(await refresh, isFalse);
    await tester.pumpAndSettle();
    expect(await preferences.read(), isNull);
    expect(container.read(appPreferencesProvider).hasError, isTrue);
    expect(await fixture.store.read(), isNull);
    expect(
      fixture.requests.every(
        (request) => request.uri.host == 'demo.lyra.example',
      ),
      isTrue,
    );
    preferences.failWrites = false;
    fixture.credentialsExpired = false;
    await tapLogin(tester, find.byKey(const ValueKey('login-github')));
    expect((await fixture.store.read())!.serverUrl, loginFixtureServer);
    expect((await fixture.store.read())!.authMethod, 'github');
    expect((await preferences.read())!.rememberedServerUrl, loginFixtureServer);
    expect(
      fixture.requests.where(
        (request) => request.uri.host == 'new.lyra.example',
      ),
      isEmpty,
    );
  });

  testWidgets('过期地址正在保存时，新登录按队列保存的地址与身份最终保留', (tester) async {
    final fixture = LoginFixture();
    final preferences = _GatedPreferencesStore();
    final container = await pumpLogin(
      tester,
      fixture,
      signedIn: true,
      configuredServer: 'https://new.lyra.example',
      preferences: preferences,
    );
    final controller = container.read(sessionProvider.notifier);
    fixture.credentialsExpired = true;
    final refresh = controller.refreshLedger();
    await tester.pumpAndSettle();
    await preferences.writeStarted.future;
    expect(await fixture.store.read(), isNull);
    fixture.credentialsExpired = false;

    // 与登录页相同的顺序：先保存本次地址，成功后再启动该地址的授权。
    final nextLogin = () async {
      await container
          .read(appPreferencesProvider.notifier)
          .setRememberedServerUrl('https://new.lyra.example');
      return controller.signIn(
        provider: AuthProvider.github,
        serverUrl: 'https://new.lyra.example',
        displayName: 'Lyra Mobile',
      );
    }();
    await tester.pumpAndSettle();
    expect(fixture.openedUrls, isEmpty);
    preferences.writeGate.complete();
    await tester.pumpAndSettle();
    expect(await refresh, isFalse);
    expect(await nextLogin, isTrue);
    await tester.pumpAndSettle();
    expect(
      (await preferences.read())!.rememberedServerUrl,
      'https://new.lyra.example',
    );
    expect((await fixture.store.read())!.serverUrl, 'https://new.lyra.example');
    expect((await fixture.store.read())!.authMethod, 'github');
    expect(container.read(sessionProvider).value, isA<SessionAuthenticated>());
    expect(
      fixture.requests
          .where((request) => request.uri.host == 'new.lyra.example')
          .every(
            (request) =>
                request.headers['authorization'] == null ||
                request.headers['authorization'] == 'Bearer github-access',
          ),
      isTrue,
    );
  });

  testWidgets('未知登录错误不展示后台原始技术消息，仍可重试', (tester) async {
    final fixture = LoginFixture()..loginError = 'SYNTHETIC_INTERNAL_ERROR';
    await pumpLogin(tester, fixture);
    await tapLogin(tester, find.byKey(const ValueKey('login-github')));
    expect(find.text('登录未完成，请重试。'), findsOneWidget);
    expect(find.textContaining('服务端'), findsNothing);
    expect(find.textContaining('SYNTHETIC_INTERNAL_ERROR'), findsNothing);
    fixture.loginError = null;
    await tapLogin(tester, find.byKey(const ValueKey('login-github')));
    expect(find.byType(LoginScreen), findsNothing);
  });
}

class _DelayedPreferencesStore extends MemoryAppPreferencesStore {
  final gate = Completer<AppPreferences?>();

  @override
  Future<AppPreferences?> read() => gate.future;
}

class _ObservedPreferencesStore extends MemoryAppPreferencesStore {
  final firstWrite = Completer<void>();

  @override
  Future<void> write(AppPreferences preferences) async {
    await super.write(preferences);
    if (!firstWrite.isCompleted) firstWrite.complete();
  }
}

class _GatedPreferencesStore extends MemoryAppPreferencesStore {
  final writeStarted = Completer<void>();
  final writeGate = Completer<void>();
  bool failWrites = false;

  @override
  Future<void> write(AppPreferences preferences) async {
    if (!writeStarted.isCompleted) writeStarted.complete();
    await writeGate.future;
    if (failWrites) throw StateError('Synthetic preferences-write failure');
    await super.write(preferences);
  }
}
