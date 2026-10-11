import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

class _ControlledStore extends MemoryAppPreferencesStore {
  _ControlledStore({super.initialValue});

  Completer<void>? readGate;
  Completer<void>? writeGate;
  bool failRead = false;
  bool failWrite = false;
  int reads = 0;
  int writes = 0;
  int activeWrites = 0;
  int maxActiveWrites = 0;

  @override
  Future<AppPreferences?> read() async {
    ++reads;
    await readGate?.future;
    if (failRead) throw StateError('设置读取失败');
    return super.read();
  }

  @override
  Future<void> write(AppPreferences preferences) async {
    ++writes;
    ++activeWrites;
    if (activeWrites > maxActiveWrites) maxActiveWrites = activeWrites;
    try {
      await writeGate?.future;
      if (failWrite) throw StateError('设置保存失败');
      await super.write(preferences);
    } finally {
      --activeWrites;
    }
  }
}

ProviderContainer _container(AppPreferencesStore store, {String? identity}) =>
    ProviderContainer(
      overrides: [
        appPreferencesStoreProvider.overrideWithValue(store),
        preferencesIdentityProvider.overrideWithValue(identity),
      ],
    );

CredentialSnapshot _snapshot({
  String server = 'https://lyra.example',
  String device = 'device-1',
  String? subject = 'google-owner',
}) => CredentialSnapshot(
  serverUrl: server,
  deviceId: device,
  accessToken: 'test-access',
  refreshToken: 'test-refresh',
  authMethod: 'google',
  userEmail: 'same@example.com',
  googleSubject: subject,
);

Future<void> _waitFor(bool Function() predicate) async {
  for (var attempt = 0; attempt < 100; ++attempt) {
    if (predicate()) return;
    await Future<void>.delayed(Duration.zero);
  }
  fail('预期异步状态没有出现');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('三种登录主体按提供方隔离，旧 Google 偏好身份键保持一致', () {
    CredentialSnapshot snapshot(String provider, {String subject = '123'}) =>
        CredentialSnapshot(
          serverUrl: 'https://lyra.example',
          deviceId: 'new-device',
          accessToken: 'access',
          refreshToken: 'refresh',
          authMethod: provider,
          providerSubject: subject,
          userEmail: 'same@example.com',
        );
    final google = preferencesIdentityKey(snapshot('google'));
    final github = preferencesIdentityKey(snapshot('github'));
    final linuxdo = preferencesIdentityKey(snapshot('linuxdo'));
    expect({google, github, linuxdo}, hasLength(3));
    expect(google, preferencesIdentityKey(_snapshot(subject: '123')));
    expect(
      preferencesIdentityKey(snapshot('github', subject: '456')),
      isNot(github),
    );
  });

  test('通用身份与昵称跨安全存储恢复，退出一起清理，不影响 Google 历史字段', () async {
    FlutterSecureStorage.setMockInitialValues({});
    const store = SecureCredentialStore();
    await store.write(_snapshot());
    expect((await store.read())!.subject, 'google-owner');
    await store.write(
      const CredentialSnapshot(
        serverUrl: 'https://lyra.example',
        deviceId: 'github-device',
        accessToken: 'access',
        refreshToken: 'refresh',
        authMethod: 'github',
        providerSubject: '2345',
        userDisplayName: 'GitHub name',
      ),
    );
    final restored = (await store.read())!;
    expect(restored.subject, '2345');
    expect(restored.userDisplayName, 'GitHub name');
    expect(restored.googleSubject, isNull);
    expect(restored.userEmail, isNull);
    await store.clear();
    expect(
      await const FlutterSecureStorage().read(key: 'lyra.providerSubject'),
      isNull,
    );
    expect(
      await const FlutterSecureStorage().read(key: 'lyra.userDisplayName'),
      isNull,
    );
  });

  test('首次无存储正常采用默认值，加载前隐藏金额', () async {
    final container = _container(MemoryAppPreferencesStore());
    addTearDown(container.dispose);
    expect(container.read(amountsHiddenProvider), isTrue);
    final preferences = await container.read(appPreferencesProvider.future);
    expect(preferences.hideAmountsOnStartup, isFalse);
    expect(preferences.startupAccountMode, StartupAccountMode.rememberLast);
    expect(preferences.profitColorScheme, ProfitColorScheme.greenUp);
    expect(preferences.rememberedServerUrl, isNull);
    expect(container.read(amountsHiddenProvider), isFalse);
  });

  test('异步加载与串行多字段更新不会被迟到读取或写入覆盖，重启保留结果', () async {
    final store = _ControlledStore()..readGate = Completer<void>();
    final first = _container(store, identity: 'owner-a');
    final loading = first.read(appPreferencesProvider.future);
    final controller = first.read(appPreferencesProvider.notifier);
    final hidden = controller.setHideAmountsOnStartup(true);
    final color = controller.setProfitColorScheme(ProfitColorScheme.redUp);
    final selected = controller.setSpecificStartupAccount('account-a');
    final remembered = controller.rememberAccountScope('owner-a', 'account-b');
    final address = controller.setRememberedServerUrl(
      ' HTTPS://LYRA.EXAMPLE:443/ ',
    );
    await _waitFor(() => store.reads == 1);
    expect(store.writes, 0);
    store.readGate!.complete();
    await loading;
    await Future.wait([hidden, color, selected, remembered, address]);
    final result = first.read(appPreferencesProvider).requireValue;
    expect(result.hideAmountsOnStartup, isTrue);
    expect(result.profitColorScheme, ProfitColorScheme.redUp);
    expect(result.startupAccountMode, StartupAccountMode.specific);
    expect(result.rememberedServerUrl, 'https://lyra.example');
    expect(result.forIdentity('owner-a').specificAccountId, 'account-a');
    expect(result.forIdentity('owner-a').lastScopeId, 'account-b');
    expect(store.maxActiveWrites, 1);
    first.dispose();
    final restarted = _container(store, identity: 'owner-a');
    addTearDown(restarted.dispose);
    expect(
      (await restarted.read(appPreferencesProvider.future)).toJson(),
      result.toJson(),
    );
  });

  test('旧容器销毁后进行中的写入先完成，新容器读取最新保存状态', () async {
    final store = _ControlledStore();
    final first = _container(store);
    await first.read(appPreferencesProvider.future);
    store.writeGate = Completer<void>();
    final writing = first
        .read(appPreferencesProvider.notifier)
        .setHideAmountsOnStartup(true);
    await _waitFor(() => store.activeWrites == 1);
    first.dispose();
    final second = _container(store);
    addTearDown(second.dispose);
    final restored = second.read(appPreferencesProvider.future);
    store.writeGate!.complete();
    await writing;
    expect((await restored).hideAmountsOnStartup, isTrue);
    expect(store.maxActiveWrites, 1);
  });

  test('加载失败保持隐藏且不覆盖存储，重试成功后才应用启动设置', () async {
    final store = _ControlledStore(
      initialValue: const AppPreferences(
        profitColorScheme: ProfitColorScheme.redUp,
      ),
    )..failRead = true;
    final container = _container(store);
    addTearDown(container.dispose);
    expect(container.read(amountsHiddenProvider), isTrue);
    await expectLater(
      container.read(appPreferencesProvider.future),
      throwsStateError,
    );
    expect(container.read(appPreferencesProvider).hasError, isTrue);
    expect(container.read(amountsHiddenProvider), isTrue);
    await expectLater(
      container
          .read(appPreferencesProvider.notifier)
          .setHideAmountsOnStartup(true),
      throwsStateError,
    );
    expect(store.writes, 0);
    store.failRead = false;
    await container.read(appPreferencesProvider.notifier).reload();
    expect(container.read(amountsHiddenProvider), isFalse);
    expect(container.read(profitColorSchemeProvider), ProfitColorScheme.redUp);
  });

  test('保存失败暴露错误并保留旧配色，随后成功保存不携带失败设置', () async {
    final store = _ControlledStore(
      initialValue: const AppPreferences(
        profitColorScheme: ProfitColorScheme.redUp,
      ),
    );
    final container = _container(store);
    addTearDown(container.dispose);
    await container.read(appPreferencesProvider.future);
    expect(container.read(profitColorSchemeProvider), ProfitColorScheme.redUp);
    store.failWrite = true;
    await expectLater(
      container
          .read(appPreferencesProvider.notifier)
          .setHideAmountsOnStartup(true),
      throwsStateError,
    );
    expect(container.read(appPreferencesProvider).hasError, isTrue);
    expect(container.read(profitColorSchemeProvider), ProfitColorScheme.redUp);
    store.failWrite = false;
    await container
        .read(appPreferencesProvider.notifier)
        .setRememberedServerUrl('https://new.example/');
    final saved = container.read(appPreferencesProvider).requireValue;
    expect(saved.hideAmountsOnStartup, isFalse);
    expect(saved.profitColorScheme, ProfitColorScheme.redUp);
    expect(saved.rememberedServerUrl, 'https://new.example');
  });

  test('当前显隐不随其他设置或下次启动设置更新而重置', () async {
    final container = _container(MemoryAppPreferencesStore());
    addTearDown(container.dispose);
    container.read(amountsHiddenProvider);
    await container.read(appPreferencesProvider.future);
    final privacy = container.read(amountsHiddenProvider.notifier);
    privacy.toggle();
    expect(container.read(amountsHiddenProvider), isTrue);
    await container
        .read(appPreferencesProvider.notifier)
        .setProfitColorScheme(ProfitColorScheme.redUp);
    expect(container.read(amountsHiddenProvider), isTrue);
    privacy.setHidden(false);
    await container
        .read(appPreferencesProvider.notifier)
        .setHideAmountsOnStartup(true);
    expect(container.read(amountsHiddenProvider), isFalse);
  });

  test('保存服务端地址沿用登录的 HTTPS 边界，非法地址不覆盖原值', () async {
    final container = _container(
      MemoryAppPreferencesStore(
        initialValue: const AppPreferences(
          rememberedServerUrl: 'https://original.example',
        ),
      ),
    );
    addTearDown(container.dispose);
    await container.read(appPreferencesProvider.future);
    final controller = container.read(appPreferencesProvider.notifier);
    await expectLater(
      controller.setRememberedServerUrl('http://remote.example'),
      throwsA(
        isA<LyraApiError>().having(
          (error) => error.code,
          'code',
          'SERVER_URL_INVALID',
        ),
      ),
    );
    expect(
      container.read(appPreferencesProvider).requireValue.rememberedServerUrl,
      'https://original.example',
    );
    await controller.setRememberedServerUrl('http://localhost:8080/');
    expect(
      container.read(appPreferencesProvider).requireValue.rememberedServerUrl,
      'http://localhost:8080',
    );
  });

  test('手动显示优先于迟到的启动隐藏设置', () async {
    final store = _ControlledStore(
      initialValue: const AppPreferences(hideAmountsOnStartup: true),
    )..readGate = Completer<void>();
    final container = _container(store);
    addTearDown(container.dispose);
    expect(container.read(amountsHiddenProvider), isTrue);
    container.read(amountsHiddenProvider.notifier).toggle();
    store.readGate!.complete();
    await container.read(appPreferencesProvider.future);
    expect(container.read(amountsHiddenProvider), isFalse);
  });

  test('稳定 subject 跨新设备恢复，不同服务器或身份不继承，旧凭据只回落设备身份', () async {
    final key = preferencesIdentityKey(_snapshot());
    expect(
      preferencesIdentityKey(
        _snapshot(device: 'new-device', server: 'https://LYRA.EXAMPLE:443/'),
      ),
      key,
    );
    expect(
      preferencesIdentityKey(_snapshot(subject: 'other-owner')),
      isNot(key),
    );
    expect(
      preferencesIdentityKey(_snapshot(server: 'https://other.example')),
      isNot(key),
    );
    expect(preferencesIdentityKey(_snapshot(subject: null)), isNot(key));
    expect(
      preferencesIdentityKey(_snapshot(subject: null, device: 'another')),
      isNot(preferencesIdentityKey(_snapshot(subject: null))),
    );
    final store = MemoryAppPreferencesStore();
    final first = _container(store, identity: key);
    await first.read(appPreferencesProvider.future);
    await first
        .read(appPreferencesProvider.notifier)
        .setSpecificStartupAccount('private-account');
    first.dispose();
    final other = _container(
      store,
      identity: preferencesIdentityKey(_snapshot(subject: 'other-owner')),
    );
    addTearDown(other.dispose);
    await other.read(appPreferencesProvider.future);
    expect(
      other.read(currentAccountPreferencesProvider).specificAccountId,
      isNull,
    );
    expect(other.read(currentAccountPreferencesProvider).lastScopeId, 'all');
  });

  test('安全存储独立 JSON 键在退出后保留，Google subject 与凭据一起清除', () async {
    FlutterSecureStorage.setMockInitialValues({});
    const credentials = SecureCredentialStore();
    const preferences = SecureAppPreferencesStore();
    await credentials.write(_snapshot());
    expect((await credentials.read())?.googleSubject, 'google-owner');
    await preferences.write(
      const AppPreferences(
        hideAmountsOnStartup: true,
        rememberedServerUrl: 'https://lyra.example',
      ),
    );
    await credentials.clear();
    expect(await credentials.read(), isNull);
    final saved = await preferences.read();
    expect(saved?.hideAmountsOnStartup, isTrue);
    expect(saved?.rememberedServerUrl, 'https://lyra.example');
    expect(
      await const FlutterSecureStorage().read(key: 'lyra.googleSubject'),
      isNull,
    );
  });

  test('未知或损坏 JSON 明确失败，不将损坏偏好当作首次安装', () async {
    FlutterSecureStorage.setMockInitialValues({
      SecureAppPreferencesStore.storageKey: '{broken',
    });
    await expectLater(
      const SecureAppPreferencesStore().read(),
      throwsFormatException,
    );
    expect(
      () => AppPreferences.fromJson({'version': 2}),
      throwsFormatException,
    );
  });
}
