import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/features/system/system_repository.dart';

/// 协议 3 服务端；客户端协议同为 3，必须判为兼容。
const _metadata = <String, Object?>{
  'server_version': '0.1.0',
  'protocol_version': 3,
  'min_supported_protocol_version': 3,
  'max_supported_protocol_version': 3,
  'schema_version': 16,
};

void main() {
  test('公开检查保留部署子路径、不发送登录凭据、验证实际服务状态', () async {
    final paths = <String>[];
    final repository = _repository((request) async {
      paths.add(request.uri.path);
      expectSync(request.headers.containsKey('authorization'), isFalse);
      expectSync(request.headers.containsKey('cookie'), isFalse);
      expectSync(request.followRedirects, isFalse);
      return _json(
        request.path.endsWith('/info') ? _metadata : {'status': 'healthy'},
      );
    }, serverUrl: 'https://lyra.example.com/ledger/');
    final result = await repository.check();
    expect(result.status, ServerConnectionStatus.healthy);
    expect(result.serverVersion, '0.1.0');
    expect(result.isCompatible, isTrue);
    expect(paths, ['/ledger/v1/system/info', '/ledger/v1/health']);
    repository.close();
  });

  test('更高协议的服务端仍支持当前协议时保持兼容', () async {
    final repository = _repository(
      (request) async => _json(
        request.path.endsWith('/info')
            ? {
                ..._metadata,
                'protocol_version': 4,
                'min_supported_protocol_version': 3,
                'max_supported_protocol_version': 4,
              }
            : {'status': 'degraded'},
      ),
    );
    final result = await repository.check();
    expect(result.isCompatible, isTrue);
    expect(result.status, ServerConnectionStatus.degraded);
    repository.close();
  });

  test('协议 3 客户端拒绝只支持协议 2 的服务端，且不发健康请求', () async {
    var calls = 0;
    final repository = _repository((request) async {
      calls++;
      return _json({
        ..._metadata,
        'protocol_version': 2,
        'min_supported_protocol_version': 2,
        'max_supported_protocol_version': 2,
      });
    });
    final result = await repository.check();
    expect(result.status, ServerConnectionStatus.incompatible);
    expect(result.isCompatible, isFalse);
    expect(calls, 1);
    repository.close();
  });

  test('503 健康响应保留服务版本与明确不可用状态', () async {
    final repository = _repository(
      (request) async => request.path.endsWith('/info')
          ? _json(_metadata)
          : _json({'status': 'unavailable'}, status: 503),
    );
    final result = await repository.check();
    expect(result.status, ServerConnectionStatus.unavailable);
    expect(result.serverVersion, '0.1.0');
    expect(result.isCompatible, isTrue);
    repository.close();
  });

  test('非 Lyra 地址和不一致健康数据不能报告为连接成功', () async {
    for (final invalidInfo in [true, false]) {
      final repository = _repository((request) async {
        if (request.path.endsWith('/info')) {
          return _json(invalidInfo ? {'status': 'ok'} : _metadata);
        }
        return _json({'status': 'healthy'}, status: 503);
      });
      await expectLater(
        repository.check(),
        throwsA(
          isA<LyraApiError>().having(
            (e) => e.code,
            'code',
            'SERVER_INFO_INVALID',
          ),
        ),
      );
      repository.close();
    }
  });

  test('网络失败只返回连接错误，不含底层连接细节', () async {
    final repository = _repository((request) async {
      throw DioException(
        requestOptions: request,
        type: DioExceptionType.connectionTimeout,
        message: 'private transport internals',
      );
    });
    await expectLater(
      repository.check(),
      throwsA(
        isA<LyraApiError>()
            .having((e) => e.code, 'code', 'SERVER_CHECK_FAILED')
            .having((e) => e.retryable, 'retryable', isTrue)
            .having((e) => e.message, 'message', isNot(contains('private'))),
      ),
    );
    repository.close();
  });

  test('公开检查的 401 不执行凭据刷新或额外请求', () async {
    var requests = 0;
    final repository = _repository((request) async {
      requests++;
      return _json({'code': 'AUTH_REQUIRED'}, status: 401);
    });
    await expectLater(
      repository.check(),
      throwsA(
        isA<LyraApiError>().having((e) => e.statusCode, 'statusCode', 401),
      ),
    );
    expect(requests, 1);
    repository.close();
  });

  test('地址校验拒绝非 HTTPS 远端、凭据和查询参数，允许本机调试', () {
    for (final address in [
      'http://example.com',
      'http://10.0.2.2',
      'https://user:password@example.com',
      'https://example.com?token=secret',
      'https://example.com#fragment',
      'not a server',
    ]) {
      expect(
        () => normalizeLyraServerUrl(address),
        throwsA(isA<LyraApiError>()),
      );
    }
    for (final host in ['localhost', '127.0.0.1', '[::1]']) {
      expect(normalizeLyraServerUrl('http://$host:3141/'), 'http://$host:3141');
    }
    expect(
      normalizeLyraServerUrl(' https://lyra.example.com/ '),
      'https://lyra.example.com',
    );
  });
}

SystemRepository _repository(
  Future<ResponseBody> Function(RequestOptions) response, {
  String serverUrl = 'https://lyra.example.com',
}) => SystemRepository(
  serverUrl: serverUrl,
  dio: Dio()..httpClientAdapter = _Adapter(response),
);

ResponseBody _json(Object body, {int status = 200}) => ResponseBody.fromString(
  jsonEncode(body),
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);

class _Adapter implements HttpClientAdapter {
  _Adapter(this.response);
  final Future<ResponseBody> Function(RequestOptions) response;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) => response(options);

  @override
  void close({bool force = false}) {}
}
