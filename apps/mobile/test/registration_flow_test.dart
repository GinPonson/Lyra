import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';

import 'support/login_fixture.dart';

/// 用现有登录夹具创建纯内存会话；可替换 HTTP 工厂以控制取消期间的响应时序。
Future<ProviderContainer> _container(
  LoginFixture fixture, {
  DioFactory? clientFactory,
}) async {
  final original = await fixture.dependencies();
  final container = ProviderContainer(
    overrides: [
      sessionDependenciesProvider.overrideWithValue(
        SessionDependencies(
          credentialStore: original.credentialStore,
          agentSessionStore: original.agentSessionStore,
          openAuthorizationUrl: original.openAuthorizationUrl,
          clientFactory: clientFactory ?? original.clientFactory,
        ),
      ),
    ],
  );
  addTearDown(container.dispose);
  await container.read(sessionProvider.future);
  return container;
}

/// 进入已授权、待邀请码状态，确认它没有生成任何本机身份凭据。
Future<void> _awaitInvite(
  LoginFixture fixture,
  ProviderContainer container, {
  AuthProvider provider = AuthProvider.google,
}) async {
  fixture.loginError = 'INVITE_CODE_REQUIRED';
  fixture.loginErrorStatus = 403;
  final authenticated = await container
      .read(sessionProvider.notifier)
      .signIn(
        provider: provider,
        serverUrl: loginFixtureServer,
        displayName: 'Registration test phone',
      );
  expect(authenticated, isFalse);
  expect(
    container.read(sessionProvider).value,
    isA<SessionRegistrationRequired>(),
  );
  expect(await fixture.store.read(), isNull);
}

/// 按稳定业务码校验错误，避免测试依赖服务端诊断正文或临时异常字符串。
Matcher _error(String code) =>
    isA<LyraApiError>().having((error) => error.code, 'code', code);

void main() {
  for (final provider in AuthProvider.values) {
    test('${provider.label} 授权后补邀请码复用同一 PKCE 事务并安全建立会话', () async {
      final fixture = LoginFixture();
      final container = await _container(fixture);
      await _awaitInvite(fixture, container, provider: provider);
      final pending =
          container.read(sessionProvider).value as SessionRegistrationRequired;
      expect(pending.provider, provider);
      expect(pending.serverUrl, loginFixtureServer);
      final before = fixture.requests.singleWhere(
        (r) => r.uri.path.endsWith('/exchange'),
      );
      fixture.loginError = null;
      expect(
        await container
            .read(sessionProvider.notifier)
            .completeRegistration(inviteCode: '  sensitive-Case-Code  '),
        isTrue,
      );
      final exchanges = fixture.requests
          .where((r) => r.uri.path.endsWith('/exchange'))
          .toList();
      final start = fixture.requests.singleWhere(
        (r) => r.uri.path.endsWith('/start'),
      );
      expect((start.data as Map).containsKey('inviteCode'), isFalse);
      expect(exchanges, hasLength(2));
      expect((before.data as Map).containsKey('inviteCode'), isFalse);
      expect((exchanges.last.data as Map)['inviteCode'], 'sensitive-Case-Code');
      expect(
        (exchanges.last.data as Map)['loginId'],
        (before.data as Map)['loginId'],
      );
      expect(
        (exchanges.last.data as Map)['codeVerifier'],
        (before.data as Map)['codeVerifier'],
      );
      expect(fixture.openedUrls, hasLength(1));
      expect(
        container.read(sessionProvider).value,
        isA<SessionAuthenticated>(),
      );
      final stored = await fixture.store.read();
      expect(stored?.authMethod, provider.id);
      expect(stored?.userId, '${provider.id}-user');
      expect(stored?.devicePrivateKey, isNotEmpty);
      expect(stored?.accessToken, '${provider.id}-access');
    });
  }

  test('错码后保持注册状态，更正重试无需再次打开浏览器', () async {
    final fixture = LoginFixture();
    final container = await _container(fixture);
    await _awaitInvite(fixture, container);
    final pending = container.read(sessionProvider).value;
    fixture.loginError = 'INVITE_CODE_INVALID';
    await expectLater(
      container
          .read(sessionProvider.notifier)
          .completeRegistration(inviteCode: 'used-code'),
      throwsA(_error('INVITE_CODE_INVALID')),
    );
    expect(container.read(sessionProvider).value, same(pending));
    expect(await fixture.store.read(), isNull);
    fixture.loginError = null;
    expect(
      await container
          .read(sessionProvider.notifier)
          .completeRegistration(inviteCode: 'correct-code'),
      isTrue,
    );
    expect(fixture.openedUrls, hasLength(1));
    expect(
      fixture.requests.where((r) => r.uri.path.endsWith('/start')),
      hasLength(1),
    );
  });

  test('空邀请码明确失败且不发送注册请求，仍可填写有效码', () async {
    final fixture = LoginFixture();
    final container = await _container(fixture);
    await _awaitInvite(fixture, container);
    final count = fixture.requests.length;
    await expectLater(
      container
          .read(sessionProvider.notifier)
          .completeRegistration(inviteCode: '  '),
      throwsA(_error('INVITE_CODE_REQUIRED')),
    );
    expect(fixture.requests, hasLength(count));
    expect(
      container.read(sessionProvider).value,
      isA<SessionRegistrationRequired>(),
    );
    expect(await fixture.store.read(), isNull);
  });

  test('主动返回登录撤销待注册证明，旧表单不能继续兑换', () async {
    final fixture = LoginFixture();
    final container = await _container(fixture);
    await _awaitInvite(fixture, container);
    final controller = container.read(sessionProvider.notifier);
    controller.cancelRegistration();
    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
    final count = fixture.requests.length;
    await expectLater(
      controller.completeRegistration(inviteCode: 'old-form-code'),
      throwsA(_error('LOGIN_STATE_INVALID')),
    );
    expect(fixture.requests, hasLength(count));
    expect(await fixture.store.read(), isNull);
  });

  for (final terminal in [
    'LOGIN_EXPIRED',
    'LOGIN_ALREADY_REDEEMED',
    'USER_DISABLED',
  ]) {
    test('注册的 $terminal 终态返回首页，不保留可再提交的证明', () async {
      final fixture = LoginFixture();
      final container = await _container(fixture);
      await _awaitInvite(fixture, container);
      fixture.loginError = terminal;
      fixture.loginErrorStatus = terminal == 'LOGIN_EXPIRED' ? 410 : 403;
      final controller = container.read(sessionProvider.notifier);
      await expectLater(
        controller.completeRegistration(inviteCode: 'code'),
        throwsA(_error(terminal)),
      );
      final state = container.read(sessionProvider).value;
      expect(state, isA<SessionSignedOut>());
      expect((state as SessionSignedOut).message, isNotEmpty);
      expect(await fixture.store.read(), isNull);
      await expectLater(
        controller.completeRegistration(inviteCode: 'code'),
        throwsA(_error('LOGIN_STATE_INVALID')),
      );
    });
  }

  test('注册时网络失败明确抛出并保留已验证尝试，恢复后可重试', () async {
    final fixture = LoginFixture();
    var offline = false;
    final container = await _container(
      fixture,
      clientFactory: (url) => fixture.dio(url)
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (options, handler) {
              if (offline && options.uri.path.endsWith('/exchange')) {
                handler.reject(
                  DioException.connectionError(
                    requestOptions: options,
                    reason: 'synthetic network interruption',
                  ),
                );
              } else {
                handler.next(options);
              }
            },
          ),
        ),
    );
    await _awaitInvite(fixture, container);
    final pending = container.read(sessionProvider).value;
    offline = true;
    await expectLater(
      container
          .read(sessionProvider.notifier)
          .completeRegistration(inviteCode: 'code'),
      throwsA(
        isA<LyraApiError>().having((error) => error.statusCode, 'status', 0),
      ),
    );
    expect(container.read(sessionProvider).value, same(pending));
    expect(await fixture.store.read(), isNull);
    offline = false;
    fixture.loginError = null;
    expect(
      await container
          .read(sessionProvider.notifier)
          .completeRegistration(inviteCode: 'code'),
      isTrue,
    );
    expect(fixture.openedUrls, hasLength(1));
  });

  test('取消注册中的迟到响应不能覆盖后来成功的新身份', () async {
    final fixture = LoginFixture();
    final arrived = Completer<void>();
    final release = Completer<void>();
    addTearDown(() {
      if (!release.isCompleted) release.complete();
    });
    final container = await _container(
      fixture,
      clientFactory: (url) => fixture.dio(url)
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (options, handler) async {
              if (options.uri.path.endsWith('/exchange') &&
                  (options.data as Map).containsKey('inviteCode')) {
                arrived.complete();
                await release.future;
              }
              handler.next(options);
            },
          ),
        ),
    );
    await _awaitInvite(fixture, container);
    final controller = container.read(sessionProvider.notifier);
    fixture.loginError = null;
    final stale = controller.completeRegistration(inviteCode: 'late-code');
    await arrived.future;
    controller.cancelRegistration();
    expect(
      await controller.signIn(
        provider: AuthProvider.linuxdo,
        serverUrl: loginFixtureServer,
        displayName: 'New identity',
      ),
      isTrue,
    );
    release.complete();
    expect(await stale, isFalse);
    expect((await fixture.store.read())?.authMethod, 'linuxdo');
    controller.cancelRegistration();
    expect(container.read(sessionProvider).value, isA<SessionAuthenticated>());
    expect((await fixture.store.read())?.authMethod, 'linuxdo');
  });
}
