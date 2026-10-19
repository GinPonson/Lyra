import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/features/auth/authorization_browser.dart';

/// 原生授权窗口桥接的契约测试；只替换平台通道，不启动浏览器或触达网络。
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('lyra/auth');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  final authorizationUrl = Uri.parse(
    'https://accounts.google.com/o/oauth2/v2/auth?state=synthetic-state',
  );

  tearDown(() => messenger.setMockMethodCallHandler(channel, null));

  test('等待系统授权回跳后返回，取消旧 token 不影响后续窗口', () async {
    final callback = Completer<String?>();
    final calls = <MethodCall>[];
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      expect(call.method, 'authenticate');
      expect(call.arguments, {'url': authorizationUrl.toString()});
      return callback.future;
    });
    final cancel = CancelToken();
    var completed = false;
    final login = authorizeInBrowser(authorizationUrl, cancel)
      ..then((_) => completed = true);
    await pumpEventQueue();
    expect(completed, isFalse);
    callback.complete(authorizationReturnUrl);
    expect(await login, isTrue);
    cancel.cancel();
    await pumpEventQueue();
    expect(calls.map((call) => call.method), ['authenticate']);
  });

  test('取消等待时关闭原生授权窗口，不把取消当成登录成功', () async {
    final callback = Completer<String?>();
    final calls = <String>[];
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call.method);
      if (call.method == 'authenticate') return callback.future;
      callback.complete(null);
      return null;
    });
    final cancel = CancelToken();
    final login = authorizeInBrowser(authorizationUrl, cancel);
    final cancelled = expectLater(
      login,
      throwsA(
        isA<DioException>().having(
          (error) => CancelToken.isCancel(error),
          'isCancel',
          isTrue,
        ),
      ),
    );
    await pumpEventQueue();
    cancel.cancel();
    await cancelled;
    expect(calls, ['authenticate', 'cancel']);
  });

  test('用户关闭授权窗口时返回明确的取消结果', () async {
    messenger.setMockMethodCallHandler(channel, (_) async => null);
    await expectLater(
      authorizeInBrowser(authorizationUrl, CancelToken()),
      throwsA(
        isA<LyraApiError>().having(
          (error) => error.code,
          'code',
          'LOGIN_CANCELLED',
        ),
      ),
    );
  });

  for (final callback in [
    'com.lyrafinance.lyra://auth/other',
    '$authorizationReturnUrl?accessToken=synthetic',
    '$authorizationReturnUrl#unexpected',
  ]) {
    test('拒绝非固定或携带数据的回跳：$callback', () async {
      messenger.setMockMethodCallHandler(channel, (_) async => callback);
      await expectLater(
        authorizeInBrowser(authorizationUrl, CancelToken()),
        throwsA(
          isA<LyraApiError>().having(
            (error) => error.code,
            'code',
            'LOGIN_CALLBACK_INVALID',
          ),
        ),
      );
    });
  }

  for (final code in ['AUTH_BROWSER_UNAVAILABLE', 'AUTH_BROWSER_UNSUPPORTED']) {
    test('浏览器错误 $code 显式传回登录页', () async {
      messenger.setMockMethodCallHandler(channel, (_) async {
        throw PlatformException(code: code);
      });
      await expectLater(
        authorizeInBrowser(authorizationUrl, CancelToken()),
        throwsA(
          isA<LyraApiError>().having(
            (error) => error.code,
            'code',
            code.replaceFirst('AUTH_', 'LOGIN_'),
          ),
        ),
      );
    });
  }

  test('不吞掉未预期的平台错误', () async {
    messenger.setMockMethodCallHandler(channel, (_) async {
      throw PlatformException(code: 'UNEXPECTED_NATIVE_FAILURE');
    });
    await expectLater(
      authorizeInBrowser(authorizationUrl, CancelToken()),
      throwsA(
        isA<PlatformException>().having(
          (error) => error.code,
          'code',
          'UNEXPECTED_NATIVE_FAILURE',
        ),
      ),
    );
  });
}
