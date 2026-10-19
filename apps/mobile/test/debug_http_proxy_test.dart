import 'dart:async';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/network/debug_http_proxy.dart';

void main() {
  test('未配置代理时不覆盖已有 HttpOverrides', () {
    expect(const String.fromEnvironment('LYRA_DEBUG_HTTP_PROXY'), isEmpty);
    final previousOverrides = HttpOverrides.current;

    configureDebugHttpProxy();

    expect(identical(HttpOverrides.current, previousOverrides), isTrue);
  }, skip: const String.fromEnvironment('LYRA_DEBUG_HTTP_PROXY').isNotEmpty);

  test('代理地址拒绝协议、凭据、路径和无效端口', () {
    for (final address in [
      'http://127.0.0.1:7890',
      'user:password@127.0.0.1:7890',
      '127.0.0.1:7890/path',
      '127.0.0.1:0',
      '127.0.0.1:65536',
      '[not-an-ipv6]:7890',
    ]) {
      expect(
        () => validateDebugHttpProxyAddress(address),
        throwsFormatException,
        reason: '应拒绝 $address',
      );
    }
  });

  test('代理地址接受主机名、IPv4 和 IPv6', () {
    expect(
      validateDebugHttpProxyAddress('proxy.example:7890'),
      'proxy.example:7890',
    );
    expect(validateDebugHttpProxyAddress('127.0.0.1:7890'), '127.0.0.1:7890');
    expect(validateDebugHttpProxyAddress('[::1]:7890'), '[::1]:7890');
    expect(validateDebugHttpProxyAddress('127.0.0.1:007890'), '127.0.0.1:7890');
  });

  test(
    '调试配置将 HTTP 请求发送到本地代理',
    () async {
      const proxyPort = 43871;
      final proxy = await HttpServer.bind(
        InternetAddress.loopbackIPv4,
        proxyPort,
      );
      final requestSeen = Completer<(String, String?)>();
      proxy.listen((request) async {
        requestSeen.complete((request.uri.path, request.headers.host));
        request.response.statusCode = HttpStatus.badGateway;
        await request.response.close();
      });

      final previousOverrides = HttpOverrides.current;
      HttpClient? client;
      try {
        configureDebugHttpProxy();
        client = HttpClient();
        final request = await client.getUrl(
          Uri.parse('http://proxy-check.example/through-proxy'),
        );
        final response = await request.close();
        expect(response.statusCode, HttpStatus.badGateway);
        final received = await requestSeen.future;
        expect(received.$1, '/through-proxy');
        expect(received.$2, 'proxy-check.example');
      } finally {
        client?.close(force: true);
        HttpOverrides.global = previousOverrides;
        await proxy.close(force: true);
      }
    },
    skip:
        const String.fromEnvironment('LYRA_DEBUG_HTTP_PROXY') !=
        '127.0.0.1:43871',
  );

  test(
    '编译期非法代理配置在启动配置时明确失败',
    () {
      const configuredProxy = String.fromEnvironment('LYRA_DEBUG_HTTP_PROXY');
      expect(configuredProxy, isNotEmpty);
      expect(configureDebugHttpProxy, throwsFormatException);
    },
    skip: const String.fromEnvironment('LYRA_DEBUG_HTTP_PROXY') != 'not-a-host',
  );
}
