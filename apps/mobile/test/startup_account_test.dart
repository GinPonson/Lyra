import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/accounts/account_repository.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;
import 'support/ledger_fixture.dart';

const _accountA = 'a2000000-0000-4000-8000-000000000001';
const _accountB = 'a2000000-0000-4000-8000-000000000002';
const _snapshot = CredentialSnapshot(
  serverUrl: 'https://startup.example',
  deviceId: 'device-1',
  accessToken: 'test-access',
  refreshToken: 'test-refresh',
  authMethod: 'google',
  userEmail: 'owner@example.com',
  googleSubject: 'owner-subject',
);

class _PreferencesStore extends MemoryAppPreferencesStore {
  _PreferencesStore({super.initialValue});
  Completer<void>? gate;
  bool failRead = false;
  bool failWrite = false;

  @override
  Future<AppPreferences?> read() async {
    await gate?.future;
    if (failRead) throw StateError('设置读取失败');
    return super.read();
  }

  @override
  Future<void> write(AppPreferences preferences) async {
    if (failWrite) throw StateError('设置写入失败');
    await super.write(preferences);
  }
}

class _StartupFixture {
  final requests = <RequestOptions>[];
  Completer<void>? accountsGate;
  bool failAccounts = false;
  List<BrokerageAccount> accounts = [
    const BrokerageAccount(
      id: _accountA,
      displayName: '长期账户',
      maskedSuffix: '001',
      baseCurrency: 'USD',
      status: 'active',
    ),
    const BrokerageAccount(
      id: _accountB,
      displayName: '现金账户',
      maskedSuffix: '002',
      baseCurrency: 'USD',
      status: 'active',
    ),
  ];

  Future<SessionDependencies> dependencies(CredentialSnapshot snapshot) async {
    final store = MemoryCredentialStore();
    await store.write(snapshot);
    return SessionDependencies(
      credentialStore: store,
      agentSessionStore: MemoryAgentSessionStore(),
      clientFactory: (url) =>
          Dio(BaseOptions(baseUrl: url))
            ..httpClientAdapter = FakeLyraAdapter((request) async {
              requests.add(request);
              final command = request.uri.path.split('/').last;
              if (command == 'accounts') {
                await accountsGate?.future;
                if (failAccounts) {
                  return _response({
                    'code': 'ACCOUNT_LOOKUP_FAILED',
                    'message': '账户读取失败',
                  }, 503);
                }
                return _response({
                  'data': {
                    'rows': [
                      for (final account in accounts)
                        {
                          'id': account.id,
                          'displayName': account.displayName,
                          'maskedSuffix': account.maskedSuffix,
                          'baseCurrency': account.baseCurrency,
                          'status': account.status,
                        },
                    ],
                  },
                });
              }
              return _response(
                LedgerFixture().response(command, request.queryParameters),
              );
            }),
    );
  }
}

ResponseBody _response(Map<String, Object?> body, [int status = 200]) =>
    ResponseBody.fromString(
      jsonEncode(body),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

AppPreferences _saved({
  StartupAccountMode mode = StartupAccountMode.rememberLast,
  String target = _accountA,
}) => AppPreferences(startupAccountMode: mode).withIdentity(
  preferencesIdentityKey(_snapshot),
  IdentityAccountPreferences(specificAccountId: target, lastScopeId: target),
);

Future<ProviderContainer> _start(
  _StartupFixture fixture,
  AppPreferencesStore preferences, {
  CredentialSnapshot snapshot = _snapshot,
}) async {
  final container = ProviderContainer(
    overrides: [
      sessionDependenciesProvider.overrideWithValue(
        await fixture.dependencies(snapshot),
      ),
      appPreferencesStoreProvider.overrideWithValue(preferences),
    ],
  );
  addTearDown(container.dispose);
  await container.read(sessionProvider.future);
  return container;
}

Future<void> _waitFor(bool Function() predicate) async {
  for (var attempt = 0; attempt < 200; ++attempt) {
    if (predicate()) return;
    await Future<void>.delayed(Duration.zero);
  }
  fail('预期启动状态没有出现');
}

void main() {
  test('默认账户验证完成前不返回汇总、不发当前页财务查询，恢复后使用选定范围', () async {
    final fixture = _StartupFixture()..accountsGate = Completer<void>();
    final store = _PreferencesStore(initialValue: _saved())
      ..gate = Completer<void>();
    final container = await _start(fixture, store);
    container.listen(scopedSummaryProvider, (_, _) {});
    const initialQuery = (
      path: '/v1/ledger/positions',
      parameters: '',
      scope: 'all',
    );
    container.listen(ledgerReadProvider(initialQuery), (_, _) {});
    final obsoleteRead = expectLater(
      container.read(ledgerReadProvider(initialQuery).future),
      throwsStateError,
    );
    await Future<void>.delayed(Duration.zero);
    expect(container.read(scopedSummaryProvider).isLoading, isTrue);
    expect(
      fixture.requests.where(
        (request) => request.uri.path.endsWith('/summary'),
      ),
      hasLength(1),
      reason: '只允许登录恢复原有的 all summary 请求',
    );
    expect(
      fixture.requests.where(
        (request) => request.uri.path.endsWith('/positions'),
      ),
      isEmpty,
    );
    store.gate!.complete();
    await _waitFor(
      () => fixture.requests.any(
        (request) => request.uri.path.endsWith('/accounts'),
      ),
    );
    expect(container.read(scopedSummaryProvider).isLoading, isTrue);
    expect(
      fixture.requests.where(
        (request) => request.uri.path.endsWith('/positions'),
      ),
      isEmpty,
    );
    fixture.accountsGate!.complete();
    await container.read(accountScopeReadyProvider.future);
    expect(container.read(accountScopeProvider), (
      id: _accountA,
      label: '长期账户',
    ));
    await obsoleteRead;
    final summary = await container.read(scopedSummaryProvider.future);
    expect(summary, isNotNull);
    expect(
      fixture.requests
          .where((request) => request.uri.path.endsWith('/summary'))
          .last
          .queryParameters['scope'],
      _accountA,
    );
    const query = (
      path: '/v1/ledger/positions',
      parameters: '',
      scope: _accountA,
    );
    container.listen(ledgerReadProvider(query), (_, _) {});
    await container.read(ledgerReadProvider(query).future);
    expect(
      fixture.requests
          .where((request) => request.uri.path.endsWith('/positions'))
          .map((request) => request.queryParameters['scope']),
      [_accountA],
    );
  });

  test('手动选择优先于迟到恢复，并保存上次范围；改启动设置不改当前范围', () async {
    final fixture = _StartupFixture()..accountsGate = Completer<void>();
    final store = _PreferencesStore(initialValue: _saved());
    final container = await _start(fixture, store);
    container.listen(accountScopeReadyProvider, (_, _) {});
    await _waitFor(
      () => fixture.requests.any(
        (request) => request.uri.path.endsWith('/accounts'),
      ),
    );
    container.read(accountScopeProvider.notifier).select(_accountB, '现金账户');
    await container.read(accountScopeReadyProvider.future);
    fixture.accountsGate!.complete();
    await Future<void>.delayed(Duration.zero);
    await container
        .read(appPreferencesProvider.notifier)
        .setStartupAccountMode(StartupAccountMode.all);
    expect(container.read(accountScopeProvider), (
      id: _accountB,
      label: '现金账户',
    ));
    expect(
      (await store.read())!
          .forIdentity(preferencesIdentityKey(_snapshot))
          .lastScopeId,
      _accountB,
    );
    final restarted = await _start(_StartupFixture(), store);
    await restarted.read(accountScopeReadyProvider.future);
    expect(restarted.read(accountScopeProvider).id, 'all');
    await restarted
        .read(appPreferencesProvider.notifier)
        .setStartupAccountMode(StartupAccountMode.rememberLast);
    final rememberRestart = await _start(_StartupFixture(), store);
    await rememberRestart.read(accountScopeReadyProvider.future);
    expect(rememberRestart.read(accountScopeProvider).id, _accountB);
  });

  for (final archived in [false, true]) {
    test('指定账户${archived ? '已归档' : '不存在'}安全回落全部，不请求无效范围', () async {
      final fixture = _StartupFixture()
        ..accounts = archived
            ? [
                const BrokerageAccount(
                  id: _accountA,
                  displayName: '已归档',
                  maskedSuffix: '001',
                  baseCurrency: 'USD',
                  status: 'archived',
                ),
              ]
            : [];
      final container = await _start(
        fixture,
        MemoryAppPreferencesStore(
          initialValue: _saved(mode: StartupAccountMode.specific),
        ),
      );
      container.listen(scopedSummaryProvider, (_, _) {});
      await container.read(accountScopeReadyProvider.future);
      await container.read(scopedSummaryProvider.future);
      expect(container.read(accountScopeProvider).id, 'all');
      expect(
        fixture.requests.where(
          (request) => request.queryParameters['scope'] == _accountA,
        ),
        isEmpty,
      );
    });
  }

  test('稳定 Google 身份新设备恢复指定账户；另一身份或服务端绝不继承', () async {
    final preferences = MemoryAppPreferencesStore(
      initialValue: _saved(mode: StartupAccountMode.specific),
    );
    for (final (subject, server, expected) in [
      ('owner-subject', 'https://startup.example/', _accountA),
      ('different-owner', 'https://startup.example', 'all'),
      ('owner-subject', 'https://another.example', 'all'),
    ]) {
      final container = await _start(
        _StartupFixture(),
        preferences,
        snapshot: CredentialSnapshot(
          serverUrl: server,
          deviceId: 'new-device',
          accessToken: 'new-access',
          refreshToken: 'new-refresh',
          authMethod: 'google',
          userEmail: 'owner@example.com',
          googleSubject: subject,
        ),
      );
      await container.read(accountScopeReadyProvider.future);
      expect(container.read(accountScopeProvider).id, expected);
    }
  });

  test('切换身份后旧账户查询的迟到结果不能恢复旧账户', () async {
    final oldFixture = _StartupFixture()..accountsGate = Completer<void>();
    final store = MemoryAppPreferencesStore(
      initialValue: _saved(mode: StartupAccountMode.specific),
    );
    final container = await _start(oldFixture, store);
    container.listen(accountScopeReadyProvider, (_, _) {});
    final oldReady = container
        .read(accountScopeReadyProvider.future)
        .then<void>((_) {}, onError: (Object _, StackTrace _) {});
    await _waitFor(
      () => oldFixture.requests.any(
        (request) => request.uri.path.endsWith('/accounts'),
      ),
    );
    final newFixture = _StartupFixture();
    final nextDependencies = await newFixture.dependencies(
      const CredentialSnapshot(
        serverUrl: 'https://startup.example',
        deviceId: 'other-device',
        accessToken: 'other-access',
        refreshToken: 'other-refresh',
        authMethod: 'google',
        userEmail: 'owner@example.com',
        googleSubject: 'other-owner',
      ),
    );
    container.updateOverrides([
      sessionDependenciesProvider.overrideWithValue(nextDependencies),
      appPreferencesStoreProvider.overrideWithValue(store),
    ]);
    await container.read(sessionProvider.future);
    await container.read(accountScopeReadyProvider.future);
    oldFixture.accountsGate!.complete();
    await oldReady;
    await Future<void>.delayed(Duration.zero);
    expect(container.read(accountScopeProvider).id, 'all');
    expect(
      container.read(currentAccountPreferencesProvider).specificAccountId,
      isNull,
    );
  });

  test('设置读取失败阻止错误范围金额显示，重试设置读取可恢复', () async {
    final fixture = _StartupFixture();
    final store = _PreferencesStore(initialValue: _saved())..failRead = true;
    final container = await _start(fixture, store);
    expect(container.read(amountsHiddenProvider), isTrue);
    container.listen(scopedSummaryProvider, (_, _) {});
    await expectLater(
      container.read(scopedSummaryProvider.future),
      throwsStateError,
    );
    expect(container.read(amountsHiddenProvider), isTrue);
    expect(
      fixture.requests.where(
        (request) => request.uri.path.endsWith('/accounts'),
      ),
      isEmpty,
    );
    store.failRead = false;
    await container.read(appPreferencesProvider.notifier).reload();
    await container.read(accountScopeReadyProvider.future);
    expect(container.read(accountScopeProvider).id, _accountA);
    expect(await container.read(scopedSummaryProvider.future), isNotNull);
  });

  test('账户查询失败不假定账户已删除，重新读取后恢复指定账户', () async {
    final fixture = _StartupFixture()..failAccounts = true;
    final container = await _start(
      fixture,
      MemoryAppPreferencesStore(
        initialValue: _saved(mode: StartupAccountMode.specific),
      ),
    );
    container.listen(scopedSummaryProvider, (_, _) {});
    await expectLater(
      container.read(scopedSummaryProvider.future),
      throwsA(anything),
    );
    expect(container.read(accountScopeReadyProvider).hasError, isTrue);
    fixture.failAccounts = false;
    container.invalidate(scopedSummaryProvider);
    expect(await container.read(scopedSummaryProvider.future), isNotNull);
    expect(container.read(accountScopeProvider).id, _accountA);
  });

  for (final (scope, label) in [(_accountB, '现金账户'), ('all', '全部账号')]) {
    test('启动读取失败后主动选择 $label 可查询，后续偏好重试不撤销选择', () async {
      final fixture = _StartupFixture();
      final store = _PreferencesStore(initialValue: _saved())..failRead = true;
      final container = await _start(fixture, store);
      container.listen(scopedSummaryProvider, (_, _) {});
      await expectLater(
        container.read(scopedSummaryProvider.future),
        throwsStateError,
      );
      expect(container.read(accountScopeReadyProvider).hasError, isTrue);

      container.read(accountScopeProvider.notifier).select(scope, label);
      await container.read(accountScopeReadyProvider.future);
      expect(await container.read(scopedSummaryProvider.future), isNotNull);
      final query = (
        path: '/v1/ledger/positions',
        parameters: '',
        scope: scope,
      );
      container.listen(ledgerReadProvider(query), (_, _) {});
      await container.read(ledgerReadProvider(query).future);
      expect(
        fixture.requests
            .where((request) => request.uri.path.endsWith('/positions'))
            .last
            .queryParameters['scope'],
        scope,
      );
      store.failRead = false;
      await container.read(appPreferencesProvider.notifier).reload();
      expect(container.read(accountScopeProvider).id, scope);
      expect(container.read(accountScopeReadyProvider).hasError, isFalse);
    });
  }

  test('记住选择写入失败明确暴露设置错误，但不会撤销用户当前选择', () async {
    final store = _PreferencesStore(initialValue: _saved());
    final container = await _start(_StartupFixture(), store);
    await container.read(accountScopeReadyProvider.future);
    store.failWrite = true;
    container.read(accountScopeProvider.notifier).select(_accountB, '现金账户');
    await _waitFor(() => container.read(appPreferencesProvider).hasError);
    expect(container.read(accountScopeProvider).id, _accountB);
    expect(
      (await store.read())!
          .forIdentity(preferencesIdentityKey(_snapshot))
          .lastScopeId,
      _accountA,
    );
  });
}
