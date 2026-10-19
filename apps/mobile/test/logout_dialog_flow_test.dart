import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/features/auth/login_screen.dart';

import 'login_screen_test.dart' as login;
import 'support/login_fixture.dart';

void main() {
  testWidgets('我的页确认退出后根节点切回独立登录页', (tester) async {
    final fixture = LoginFixture();
    final container = await login.pumpLogin(tester, fixture, signedIn: true);
    final controller = container.read(sessionProvider.notifier);
    final authenticatedRootKey = tester
        .widget<MaterialApp>(find.byType(MaterialApp))
        .key;

    expect(container.read(sessionProvider).value, isA<SessionAuthenticated>());
    expect(
      authenticatedRootKey,
      ValueKey<Object>((loginFixtureServer, 'restored-device')),
    );
    expect(find.byType(LoginScreen), findsNothing);
    expect(find.byType(NavigationBar), findsOneWidget);

    await login.tapLogin(tester, find.text('我的'));
    expect(find.byKey(const ValueKey('profile-sign-out')), findsOneWidget);
    await login.tapLogin(
      tester,
      find.byKey(const ValueKey('profile-sign-out')),
    );
    expect(find.text('退出此设备登录？'), findsOneWidget);

    await login.tapLogin(tester, find.widgetWithText(FilledButton, '退出登录'));

    expect(container.read(sessionProvider).value, isA<SessionSignedOut>());
    expect(
      identical(container.read(sessionProvider.notifier), controller),
      isTrue,
    );
    final signedOutRootKey = tester
        .widget<MaterialApp>(find.byType(MaterialApp))
        .key;
    expect(signedOutRootKey, const ValueKey<Object>('signed-out'));
    expect(signedOutRootKey, isNot(authenticatedRootKey));
    expect(await fixture.store.read(), isNull);
    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.text('退出此设备登录？'), findsNothing);
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/logout',
      ),
      hasLength(1),
    );
    expect(
      fixture.requests.where(
        (request) => request.uri.path == '/v1/auth/providers',
      ),
      isNotEmpty,
      reason: '退出后登录根页应从 SessionSignedOut 地址加载登录方式目录',
    );
    expect(tester.takeException(), isNull);
  });
}
