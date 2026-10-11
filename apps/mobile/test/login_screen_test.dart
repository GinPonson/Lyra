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
import 'package:lyra_mobile/features/auth/login_screen.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'support/login_fixture.dart';

Future<ProviderContainer> pumpLogin(
  WidgetTester tester,
  LoginFixture fixture, {
  bool signedIn = false,
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
    expect(find.text('已退出此设备登录。服务端暂时无法撤销会话，账本仍保留在服务端。'), findsOneWidget);
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

  testWidgets('未配置的来源保持不可用，不启动授权；配置可重新读取', (tester) async {
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
    expect(find.text('未配置'), findsNWidgets(2));
    expect(fixture.openedUrls, isEmpty);
    fixture.available = AuthProvider.values.toSet();
    await tapLogin(
      tester,
      find.byKey(const ValueKey('login-refresh-providers')),
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-linuxdo')))
          .onPressed,
      isNotNull,
    );
    expect(find.text('未配置'), findsNothing);
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
    expect(find.text('检测中'), findsNWidgets(3));
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

  testWidgets('未设服务端可验证并保存地址，远程 HTTP 不可用', (tester) async {
    final fixture = LoginFixture();
    final preferences = MemoryAppPreferencesStore();
    await pumpLogin(tester, fixture, server: null, preferences: preferences);
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
          .onPressed,
      isNull,
    );
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'http://remote.example.com',
    );
    await tapLogin(tester, find.byKey(const ValueKey('login-save-server')));
    expect(find.text('请输入 HTTPS 服务端地址（本机开发可使用 HTTP）。'), findsOneWidget);
    expect(fixture.requests, isEmpty);
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      '$loginFixtureServer/',
    );
    await tapLogin(tester, find.byKey(const ValueKey('login-save-server')));
    expect((await preferences.read())!.rememberedServerUrl, loginFixtureServer);
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
          .onPressed,
      isNotNull,
    );
    expect(await fixture.store.read(), isNull);
  });

  for (final status in [404, 503]) {
    testWidgets('登录方式查询 $status 给出重试或更新说明，恢复后可登录', (tester) async {
      final fixture = LoginFixture()..catalogStatus = status;
      await pumpLogin(tester, fixture);
      expect(
        find.text(
          status == 404 ? '服务端尚不支持登录方式查询，请更新服务端。' : '暂时无法读取登录方式，请检查服务端地址与连接。',
        ),
        findsOneWidget,
      );
      expect(
        tester
            .widget<OutlinedButton>(find.byKey(const ValueKey('login-google')))
            .onPressed,
        isNull,
      );
      fixture.catalogStatus = null;
      await tapLogin(
        tester,
        find.byKey(const ValueKey('login-reload-providers')),
      );
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
    expect(find.text('此账号尚未获准访问这个服务端。'), findsOneWidget);
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

  testWidgets('更换服务端先确认，退出后在新服务端专页重新选择身份', (tester) async {
    final fixture = LoginFixture();
    final preferences = MemoryAppPreferencesStore();
    await pumpLogin(tester, fixture, signedIn: true, preferences: preferences);
    await tapLogin(tester, find.text('我的'));
    await tapLogin(tester, find.byKey(const ValueKey('profile-connection')));
    await tapLogin(tester, find.text('更换服务端'));
    await tester.enterText(
      find.byKey(const ValueKey('login-server-url')),
      'https://new.lyra.example',
    );
    await tapLogin(tester, find.byKey(const ValueKey('login-change-server')));
    await tapLogin(tester, find.text('取消').hitTestable());
    expect((await fixture.store.read())!.serverUrl, loginFixtureServer);
    await tapLogin(tester, find.byKey(const ValueKey('login-change-server')));
    await tapLogin(tester, find.widgetWithText(FilledButton, '更换并前往登录').last);
    expect(find.byType(LoginScreen), findsOneWidget);
    expect(await fixture.store.read(), isNull);
    expect(
      (await preferences.read())!.rememberedServerUrl,
      'https://new.lyra.example',
    );
    expect(fixture.openedUrls, isEmpty);
    await tapLogin(tester, find.byKey(const ValueKey('login-linuxdo')));
    expect((await fixture.store.read())!.authMethod, 'linuxdo');
    expect((await fixture.store.read())!.serverUrl, 'https://new.lyra.example');
    final logout = fixture.requests.singleWhere(
      (r) => r.uri.path == '/v1/auth/logout',
    );
    expect(logout.uri.host, 'demo.lyra.example');
    for (final request in fixture.requests.where(
      (r) => r.uri.host == 'new.lyra.example',
    )) {
      expect(request.headers['authorization'], isNot('Bearer restored-access'));
    }
  });
}
