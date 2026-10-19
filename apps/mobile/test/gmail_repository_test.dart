import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/gmail/gmail_models.dart';
import 'package:lyra_mobile/features/gmail/gmail_repository.dart';

import 'support/gmail_fixture.dart';

const _connectionId = 'b3000000-0000-4000-8000-000000000001';
const _accountId = 'a2000000-0000-4000-8000-000000000001';
// The public poll ID is a UUID; the 43-character OAuth state remains server-owned.
const _authorizationId = 'b5000000-0000-4000-8000-000000000001';

void main() {
  test('连接列表读取公开邮箱状态并严格校验服务端枚举', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson({
        'configured': true,
        'connections': [gmailConnectionJson()],
      }),
    );
    final (_, repository, _) = await _repository(adapter);

    final result = await repository.connections();

    expect(result.configured, isTrue);
    expect(result.rows.single.googleEmail, 'trades@example.com');
    expect(result.rows.single.status, GmailConnectionStatus.pendingInitialScan);
    expect(adapter.requests.single.uri.path, '/v1/google/connections');
    expect(
      () => GmailConnectionStatus.parse('pending_initial_sync'),
      throwsFormatException,
    );
  });

  test('首次扫描缺少用户确认范围时不发送网络写请求', () async {
    final adapter = GmailFixtureAdapter((_) async => gmailJson({}));
    final (_, repository, _) = await _repository(adapter);

    await expectLater(
      repository.sync(
        connectionId: _connectionId,
        explicitAccountId: _accountId,
        status: GmailConnectionStatus.pendingInitialScan,
        cancelToken: CancelToken(),
      ),
      throwsA(
        isA<LyraApiError>().having(
          (error) => error.code,
          'code',
          'GMAIL_RESYNC_CONFIRMATION_REQUIRED',
        ),
      ),
    );
    expect(adapter.requests, isEmpty);
  });

  test('自定义日期重扫显式带账户、false 扫描标志和用户选择的日期', () async {
    final adapter = GmailFixtureAdapter((options) async {
      expect(options.method, 'POST');
      return gmailJson(_syncResult(mode: 'bounded_rescan'));
    });
    final (_, repository, _) = await _repository(adapter);

    final result = await repository.sync(
      connectionId: _connectionId,
      explicitAccountId: _accountId,
      status: GmailConnectionStatus.needsResync,
      scanAll: false,
      confirmedRescanAfterDate: '2025-10-27',
      cancelToken: CancelToken(),
    );

    expect(result.insertedTransactions, 2);
    expect(result.existingTransactions, 3);
    expect(result.rejectedMessages, 2);
    expect(result.conflictMessages, 1);
    expect(result.replayOrderConflictMessages, 0);
    expect(
      adapter.requests.single.uri.path,
      '/v1/google/connections/$_connectionId/sync',
    );
    expect(adapter.requests.single.data, {
      'explicitAccountId': _accountId,
      'scanAll': false,
      'confirmedRescanAfterDate': '2025-10-27',
    });
  });

  test('全量首次扫描显式发送 scanAll true 且不带日期', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson(_syncResult(mode: 'bounded_rescan')),
    );
    final (_, repository, _) = await _repository(adapter);

    await repository.sync(
      connectionId: _connectionId,
      explicitAccountId: _accountId,
      status: GmailConnectionStatus.pendingInitialScan,
      scanAll: true,
      cancelToken: CancelToken(),
    );

    expect(adapter.requests.single.data, {
      'explicitAccountId': _accountId,
      'scanAll': true,
    });
  });

  test('active 连接只发增量账户，不发送任何扫描日期', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson(_syncResult(mode: 'incremental')),
    );
    final (_, repository, _) = await _repository(adapter);

    await repository.sync(
      connectionId: _connectionId,
      explicitAccountId: _accountId,
      status: GmailConnectionStatus.active,
      cancelToken: CancelToken(),
    );

    expect(adapter.requests.single.data, {'explicitAccountId': _accountId});
  });

  test('active 连接显式全量重扫时发送范围且不走增量合同', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson(_syncResult(mode: 'bounded_rescan')),
    );
    final (_, repository, _) = await _repository(adapter);

    await repository.sync(
      connectionId: _connectionId,
      explicitAccountId: _accountId,
      status: GmailConnectionStatus.active,
      scanAll: true,
      cancelToken: CancelToken(),
    );

    expect(adapter.requests.single.data, {
      'explicitAccountId': _accountId,
      'scanAll': true,
    });
  });

  test('active 连接显式自定义日期重扫发送 false 与确认日期', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson(_syncResult(mode: 'bounded_rescan')),
    );
    final (_, repository, _) = await _repository(adapter);

    await repository.sync(
      connectionId: _connectionId,
      explicitAccountId: _accountId,
      status: GmailConnectionStatus.active,
      scanAll: false,
      confirmedRescanAfterDate: '2024-02-29',
      cancelToken: CancelToken(),
    );

    expect(adapter.requests.single.data, {
      'explicitAccountId': _accountId,
      'scanAll': false,
      'confirmedRescanAfterDate': '2024-02-29',
    });
  });

  test('全量重扫不能同时发送自定义日期', () async {
    final adapter = GmailFixtureAdapter((_) async => gmailJson({}));
    final (_, repository, _) = await _repository(adapter);

    await expectLater(
      repository.sync(
        connectionId: _connectionId,
        explicitAccountId: _accountId,
        status: GmailConnectionStatus.pendingInitialScan,
        scanAll: true,
        confirmedRescanAfterDate: '2025-10-27',
        cancelToken: CancelToken(),
      ),
      throwsFormatException,
    );
    expect(adapter.requests, isEmpty);
  });

  test('同步结果使用交易写入计数，不把邮件数量当成新增交易', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson({
        ..._syncResult(mode: 'incremental'),
        'importedMessages': 80,
        'alreadyImportedMessages': 70,
      }),
    );
    final (_, repository, _) = await _repository(adapter);

    final result = await repository.sync(
      connectionId: _connectionId,
      explicitAccountId: _accountId,
      status: GmailConnectionStatus.active,
      cancelToken: CancelToken(),
    );

    expect(result.insertedTransactions, 2);
    expect(result.existingTransactions, 3);
  });

  test('同步响应连接 ID 必须匹配请求，错连接计数不能作为成功展示', () async {
    final body = _syncResult(mode: 'incremental');
    body['connection'] = {
      ...(body['connection'] as Map<String, Object?>),
      'id': 'b3000000-0000-4000-8000-000000000002',
    };
    final adapter = GmailFixtureAdapter((_) async => gmailJson(body));
    final (_, repository, _) = await _repository(adapter);

    await expectLater(
      repository.sync(
        connectionId: _connectionId,
        explicitAccountId: _accountId,
        status: GmailConnectionStatus.active,
        cancelToken: CancelToken(),
      ),
      throwsA(isA<FormatException>()),
    );
  });

  test('同日顺序冲突必须是总冲突的子集', () {
    expect(
      () => GmailSyncResult.fromJson({
        ..._syncResult(mode: 'incremental'),
        'replayOrderConflictMessages': 2,
      }),
      throwsFormatException,
    );
  });

  test('同步超时只尝试一次 POST，结果不确定由界面明确提示', () async {
    var attempts = 0;
    final adapter = GmailFixtureAdapter((options) async {
      attempts++;
      expect(options.receiveTimeout, const Duration(minutes: 10));
      throw DioException(
        requestOptions: options,
        type: DioExceptionType.receiveTimeout,
      );
    });
    final (_, repository, _) = await _repository(adapter);

    await expectLater(
      repository.sync(
        connectionId: _connectionId,
        explicitAccountId: _accountId,
        status: GmailConnectionStatus.active,
        cancelToken: CancelToken(),
      ),
      throwsA(
        isA<LyraApiError>().having(
          (error) => error.code,
          'code',
          'CLIENT_REQUEST_TIMEOUT',
        ),
      ),
    );
    expect(attempts, 1);
  });

  test('授权使用固定 Google HTTPS 端点并轮询 pending 到 complete', () async {
    var pollCount = 0;
    final adapter = GmailFixtureAdapter((options) async {
      if (options.method == 'POST') {
        expect(options.uri.path, '/v1/google/authorization-transactions');
        expect(options.data, isEmpty);
        return gmailJson({
          'authorizationId': _authorizationId,
          'authorizationUrl':
              'https://accounts.google.com/o/oauth2/v2/auth?state=opaque',
          'expiresAt': DateTime.now()
              .add(const Duration(minutes: 2))
              .toUtc()
              .toIso8601String(),
          'pollIntervalSeconds': 1,
        }, 201);
      }
      expect(
        options.uri.path,
        '/v1/google/authorization-transactions/$_authorizationId',
      );
      pollCount++;
      if (pollCount == 1) return gmailJson({'status': 'pending'}, 202);
      return gmailJson({
        'status': 'complete',
        'connection': gmailConnectionJson(status: 'pending_initial_scan'),
      });
    });
    final (_, repository, _) = await _repository(
      adapter,
      authorizationLauncher: (url, token) async {
        expect(url.scheme, 'https');
        expect(url.host, 'accounts.google.com');
        expect(url.path, '/o/oauth2/v2/auth');
        expect(token.isCancelled, isFalse);
        return true;
      },
      delay: (_) async {},
    );

    final connection = await repository.authorize(cancelToken: CancelToken());

    expect(connection.googleEmail, 'trades@example.com');
    expect(pollCount, 2);
  });

  test('拒绝带 userinfo 的授权地址且不打开授权窗口', () async {
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson({
        'authorizationId': _authorizationId,
        'authorizationUrl':
            'https://attacker@example.com/o/oauth2/v2/auth?state=opaque',
        'expiresAt': DateTime.now()
            .add(const Duration(minutes: 2))
            .toUtc()
            .toIso8601String(),
        'pollIntervalSeconds': 1,
      }, 201),
    );
    var opened = false;
    final (_, repository, unauthorized) = await _repository(
      adapter,
      store: store,
      authorizationLauncher: (_, _) async {
        opened = true;
        throw const LyraApiError(
          code: 'LOGIN_CANCELLED',
          message: '已取消登录。',
          statusCode: 0,
          retryable: false,
        );
      },
    );

    await expectLater(
      repository.authorize(cancelToken: CancelToken()),
      throwsA(isA<FormatException>()),
    );
    expect(opened, isFalse);
    expect(adapter.requests, hasLength(1));
    expect(unauthorized.value, isFalse);
    expect(await store.read(), isNotNull);
  });

  test('用户取消 Gmail 授权时不调用 Lyra 退出逻辑', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson({
        'authorizationId': _authorizationId,
        'authorizationUrl':
            'https://accounts.google.com/o/oauth2/v2/auth?state=opaque',
        'expiresAt': DateTime.now()
            .add(const Duration(minutes: 2))
            .toUtc()
            .toIso8601String(),
        'pollIntervalSeconds': 1,
      }, 201),
    );
    final (client, repository, unauthorized) = await _repository(
      adapter,
      authorizationLauncher: (_, _) async => throw const LyraApiError(
        code: 'LOGIN_CANCELLED',
        message: '已取消登录。',
        statusCode: 0,
        retryable: false,
      ),
    );

    await expectLater(
      repository.authorize(cancelToken: CancelToken()),
      throwsA(isA<LyraApiError>()),
    );
    expect(unauthorized.value, isFalse);
    expect(client.accessToken, 'test-access');
    expect(adapter.requests, hasLength(1));
  });

  test('断开连接显式发送 revoke true 请求体', () async {
    final adapter = GmailFixtureAdapter(
      (_) async => gmailJson({
        'connection': gmailConnectionJson(status: 'disconnected'),
      }),
    );
    final (_, repository, _) = await _repository(adapter);

    final disconnected = await repository.disconnect(
      connectionId: _connectionId,
      cancelToken: CancelToken(),
    );

    expect(disconnected.status, GmailConnectionStatus.disconnected);
    expect(adapter.requests.single.method, 'DELETE');
    expect(adapter.requests.single.data, {'revoke': true});
  });
}

const _snapshot = CredentialSnapshot(
  serverUrl: 'https://fixture.invalid',
  deviceId: 'gmail-test-device',
  accessToken: 'test-access',
  refreshToken: 'test-refresh',
  userId: 'gmail-test-user',
  authMethod: 'google',
);

/// 创建一个只写入合成凭据并隔离网络的客户端与 Gmail 仓储。
Future<(LyraApiClient, GmailRepository, _CallFlag)> _repository(
  GmailFixtureAdapter adapter, {
  MemoryCredentialStore? store,
  GmailAuthorizationLauncher? authorizationLauncher,
  GmailDelay? delay,
  Future<void> Function()? onUnauthorized,
}) async {
  final credentialStore = store ?? MemoryCredentialStore();
  if (await credentialStore.read() == null) {
    await credentialStore.write(_snapshot);
  }
  final unauthorized = _CallFlag();
  final client = LyraApiClient(
    serverUrl: _snapshot.serverUrl,
    deviceId: _snapshot.deviceId,
    credentialStore: credentialStore,
    tokenRefresher: () async => null,
    onUnauthorized: () async {
      unauthorized.value = true;
      await onUnauthorized?.call();
    },
    dio: Dio(BaseOptions(baseUrl: _snapshot.serverUrl))
      ..httpClientAdapter = adapter,
  );
  return (
    client,
    GmailRepository(
      client,
      authorizationLauncher: authorizationLauncher,
      delay: delay,
    ),
    unauthorized,
  );
}

/// 构造带去重交易数的测试摘要，并附带可能误导的邮件计数。
Map<String, Object?> _syncResult({required String mode}) => {
  'mode': mode,
  'truncated': false,
  'importedMessages': 70,
  'alreadyImportedMessages': 60,
  'insertedTransactions': 2,
  'existingTransactions': 3,
  'conflictMessages': 1,
  'replayOrderConflictMessages': 0,
  'rejectedMessages': 2,
  'connection': {
    'id': _connectionId,
    'status': 'active',
    'historyId': 'private-test-cursor',
    'lastSyncAt': '2026-10-18T08:30:00Z',
  },
};

/// 记录测试期间是否误触发 Lyra 设备登出回调。
class _CallFlag {
  bool value = false;
}
