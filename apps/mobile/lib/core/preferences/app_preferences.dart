import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../../app/session.dart';
import '../../features/system/system_repository.dart'
    show normalizeLyraServerUrl;
import '../security/credential_store.dart';
import '../security/secure_storage.dart';

enum StartupAccountMode { rememberLast, all, specific }

enum ProfitColorScheme { greenUp, redUp }

/// 账户 UUID 只在同一服务端和同一稳定登录身份下恢复。
class IdentityAccountPreferences {
  const IdentityAccountPreferences({
    this.specificAccountId,
    this.lastScopeId = 'all',
  });

  final String? specificAccountId;
  final String lastScopeId;

  IdentityAccountPreferences copyWith({
    Object? specificAccountId = _unchanged,
    String? lastScopeId,
  }) => IdentityAccountPreferences(
    specificAccountId: identical(specificAccountId, _unchanged)
        ? this.specificAccountId
        : specificAccountId as String?,
    lastScopeId: lastScopeId ?? this.lastScopeId,
  );

  Map<String, Object?> toJson() => {
    'specificAccountId': specificAccountId,
    'lastScopeId': lastScopeId,
  };

  factory IdentityAccountPreferences.fromJson(Map<String, Object?> json) {
    final specific = json['specificAccountId'];
    final last = json['lastScopeId'];
    if ((specific != null && !_validScopeId(specific, allowAll: false)) ||
        !_validScopeId(last)) {
      throw const FormatException('本机账户设置格式无效');
    }
    return IdentityAccountPreferences(
      specificAccountId: specific as String?,
      lastScopeId: last as String,
    );
  }
}

/// 普通设置与凭据分开持久化；退出登录不会删除这些设置。
class AppPreferences {
  const AppPreferences({
    this.hideAmountsOnStartup = false,
    this.startupAccountMode = StartupAccountMode.rememberLast,
    this.profitColorScheme = ProfitColorScheme.greenUp,
    this.rememberedServerUrl,
    this.identityAccounts = const {},
  });

  final bool hideAmountsOnStartup;
  final StartupAccountMode startupAccountMode;
  final ProfitColorScheme profitColorScheme;
  final String? rememberedServerUrl;
  final Map<String, IdentityAccountPreferences> identityAccounts;

  IdentityAccountPreferences forIdentity(String? identity) => identity == null
      ? const IdentityAccountPreferences()
      : identityAccounts[identity] ?? const IdentityAccountPreferences();

  AppPreferences copyWith({
    bool? hideAmountsOnStartup,
    StartupAccountMode? startupAccountMode,
    ProfitColorScheme? profitColorScheme,
    Object? rememberedServerUrl = _unchanged,
    Map<String, IdentityAccountPreferences>? identityAccounts,
  }) => AppPreferences(
    hideAmountsOnStartup: hideAmountsOnStartup ?? this.hideAmountsOnStartup,
    startupAccountMode: startupAccountMode ?? this.startupAccountMode,
    profitColorScheme: profitColorScheme ?? this.profitColorScheme,
    rememberedServerUrl: identical(rememberedServerUrl, _unchanged)
        ? this.rememberedServerUrl
        : rememberedServerUrl as String?,
    identityAccounts: Map.unmodifiable(
      identityAccounts ?? this.identityAccounts,
    ),
  );

  AppPreferences withIdentity(
    String identity,
    IdentityAccountPreferences preferences,
  ) => copyWith(identityAccounts: {...identityAccounts, identity: preferences});

  Map<String, Object?> toJson() => {
    'version': 1,
    'hideAmountsOnStartup': hideAmountsOnStartup,
    'startupAccountMode': startupAccountMode.name,
    'profitColorScheme': profitColorScheme.name,
    'rememberedServerUrl': rememberedServerUrl,
    'identityAccounts': identityAccounts.map(
      (key, value) => MapEntry(key, value.toJson()),
    ),
  };

  factory AppPreferences.fromJson(Map<String, Object?> json) {
    final hidden = json['hideAmountsOnStartup'];
    final mode = StartupAccountMode.values.where(
      (value) => value.name == json['startupAccountMode'],
    );
    final color = ProfitColorScheme.values.where(
      (value) => value.name == json['profitColorScheme'],
    );
    final server = json['rememberedServerUrl'];
    final identities = json['identityAccounts'];
    if (json['version'] != 1 ||
        hidden is! bool ||
        mode.length != 1 ||
        color.length != 1 ||
        (server != null && server is! String) ||
        identities is! Map<String, Object?>) {
      throw const FormatException('本机设置格式无效，请重试读取');
    }
    final accountSettings = <String, IdentityAccountPreferences>{};
    for (final entry in identities.entries) {
      if (entry.key.isEmpty || entry.value is! Map<String, Object?>) {
        throw const FormatException('本机账户设置格式无效');
      }
      accountSettings[entry.key] = IdentityAccountPreferences.fromJson(
        entry.value as Map<String, Object?>,
      );
    }
    return AppPreferences(
      hideAmountsOnStartup: hidden,
      startupAccountMode: mode.single,
      profitColorScheme: color.single,
      rememberedServerUrl: server as String?,
      identityAccounts: Map.unmodifiable(accountSettings),
    );
  }
}

const _unchanged = Object();

bool _validScopeId(Object? value, {bool allowAll = true}) =>
    value is String &&
    value.isNotEmpty &&
    value.length <= 128 &&
    value.trim() == value &&
    !RegExp(r'[\x00-\x1f\x7f]').hasMatch(value) &&
    (allowAll || value != 'all');

/// 旧凭据没有稳定 subject 时只按设备隔离，绝不使用可能复用的邮箱认领旧设置。
String preferencesIdentityKey(CredentialSnapshot snapshot) {
  final subject = snapshot.googleSubject?.trim();
  return jsonEncode([
    normalizePreferencesServerUrl(snapshot.serverUrl),
    if (snapshot.authMethod == 'google' &&
        subject != null &&
        subject.isNotEmpty) ...[
      'google',
      subject,
    ] else ...[
      'device',
      snapshot.deviceId,
    ],
  ]);
}

String normalizePreferencesServerUrl(String value) {
  final input = value.trim();
  final uri = Uri.tryParse(input);
  if (uri == null || !uri.hasScheme || uri.host.isEmpty) {
    return input.replaceFirst(RegExp(r'/+$'), '');
  }
  return Uri(
    scheme: uri.scheme.toLowerCase(),
    host: uri.host.toLowerCase(),
    port:
        (uri.scheme == 'https' && uri.port == 443) ||
            (uri.scheme == 'http' && uri.port == 80)
        ? null
        : uri.port,
    path: uri.path.replaceFirst(RegExp(r'/+$'), ''),
  ).toString();
}

abstract class AppPreferencesStore {
  Future<AppPreferences?> read();
  Future<void> write(AppPreferences preferences);
}

class MemoryAppPreferencesStore implements AppPreferencesStore {
  MemoryAppPreferencesStore({AppPreferences? initialValue})
    : _value = initialValue;

  AppPreferences? _value;

  @override
  Future<AppPreferences?> read() async => _value;

  @override
  Future<void> write(AppPreferences preferences) async {
    // Clone through the public representation so callers cannot mutate a saved
    // map after the write has completed.
    _value = AppPreferences.fromJson(preferences.toJson());
  }
}

/// 本机偏好的安全存储实现；与凭据存储共用 [kLyraSecureStorage] 的加密参数。
class SecureAppPreferencesStore implements AppPreferencesStore {
  const SecureAppPreferencesStore({FlutterSecureStorage? storage})
    : _storage = storage ?? kLyraSecureStorage;

  final FlutterSecureStorage _storage;
  static const storageKey = 'lyra.preferences.v1';

  @override
  Future<AppPreferences?> read() async {
    final raw = await _storage.read(key: storageKey);
    if (raw == null) return null;
    try {
      final value = jsonDecode(raw);
      if (value is! Map<String, Object?>) {
        throw const FormatException('本机设置格式无效');
      }
      return AppPreferences.fromJson(value);
    } on FormatException {
      throw const FormatException('本机设置格式无效，请重试读取');
    }
  }

  @override
  Future<void> write(AppPreferences preferences) =>
      _storage.write(key: storageKey, value: jsonEncode(preferences.toJson()));
}

final _securePreferencesIdentity = Object();
final _preferencesQueues = Expando<_PreferencesQueue>();

Future<T> _withPreferencesStore<T>(
  AppPreferencesStore store,
  Future<T> Function() operation,
) {
  final identity = store is SecureAppPreferencesStore
      ? _securePreferencesIdentity
      : store;
  return (_preferencesQueues[identity] ??= _PreferencesQueue()).run(operation);
}

class _PreferencesQueue {
  Future<void>? _tail;

  Future<T> run<T>(Future<T> Function() operation) {
    final previous = _tail;
    final result = previous == null
        ? Future<T>.microtask(operation)
        : previous.then((_) => operation());
    late final Future<void> tail;
    void release() {
      if (identical(_tail, tail)) _tail = null;
    }

    tail = result.then<void>(
      (_) => release(),
      onError: (Object error, StackTrace stack) => release(),
    );
    _tail = tail;
    return result;
  }
}

/// 默认内存实现便于 widget 测试和预览；仅生产入口显式启用平台安全存储。
final appPreferencesStoreProvider = Provider<AppPreferencesStore>(
  (ref) => MemoryAppPreferencesStore(),
);

final preferencesIdentityProvider = Provider<String?>((ref) {
  final session = ref.watch(sessionProvider).value;
  return session is SessionAuthenticated
      ? preferencesIdentityKey(session.snapshot)
      : null;
});

final currentAccountPreferencesProvider = Provider<IdentityAccountPreferences>((
  ref,
) {
  final identity = ref.watch(preferencesIdentityProvider);
  final preferences =
      ref.watch(appPreferencesProvider).value ??
      ref.read(appPreferencesProvider.notifier).lastSaved;
  return preferences?.forIdentity(identity) ??
      const IdentityAccountPreferences();
});

class AppPreferencesController extends AsyncNotifier<AppPreferences> {
  int _generation = 0;
  AppPreferences? _lastSaved;
  AppPreferencesStore? _loadedStore;

  /// 写入失败保留最后成功的配色和账户设置，错误仍通过 state 暴露。
  AppPreferences? get lastSaved => _lastSaved;

  @override
  Future<AppPreferences> build() {
    final generation = ++_generation;
    final store = ref.watch(appPreferencesStoreProvider);
    if (!identical(store, _loadedStore)) _lastSaved = null;
    _loadedStore = store;
    return _withPreferencesStore(
      store,
      () async => await store.read() ?? const AppPreferences(),
    ).then((value) {
      if (ref.mounted && generation == _generation) _lastSaved = value;
      return value;
    });
  }

  Future<void> reload() async {
    ref.invalidateSelf();
    await future;
  }

  Future<void> setHideAmountsOnStartup(bool value) =>
      _update((current) => current.copyWith(hideAmountsOnStartup: value));

  Future<void> setProfitColorScheme(ProfitColorScheme value) =>
      _update((current) => current.copyWith(profitColorScheme: value));

  Future<void> setStartupAccountMode(StartupAccountMode value) =>
      _update((current) => current.copyWith(startupAccountMode: value));

  Future<void> setSpecificStartupAccount(String accountId) {
    if (!_validScopeId(accountId, allowAll: false)) {
      return Future.error(const FormatException('请选择有效的证券账户'));
    }
    final identity = ref.read(preferencesIdentityProvider);
    if (identity == null) {
      return Future.error(StateError('请先登录后设置默认账户'));
    }
    return _update(
      (current) => current
          .withIdentity(
            identity,
            current
                .forIdentity(identity)
                .copyWith(specificAccountId: accountId),
          )
          .copyWith(startupAccountMode: StartupAccountMode.specific),
    );
  }

  Future<void> setRememberedServerUrl(String? value) {
    final input = value?.trim();
    if (input == null || input.isEmpty) {
      return _update((current) => current.copyWith(rememberedServerUrl: null));
    }
    final String normalized;
    try {
      normalized = normalizeLyraServerUrl(input);
    } on Object catch (error, stack) {
      return Future.error(error, stack);
    }
    return _update(
      (current) => current.copyWith(
        rememberedServerUrl: normalizePreferencesServerUrl(normalized),
      ),
    );
  }

  /// 调用方捕获原身份；迟到的写入只能更新原身份，不会继承下一次登录。
  Future<void> rememberAccountScope(String identity, String scopeId) {
    if (!_validScopeId(scopeId)) {
      return Future.error(const FormatException('账户范围无效'));
    }
    return _update(
      (current) => current.withIdentity(
        identity,
        current.forIdentity(identity).copyWith(lastScopeId: scopeId),
      ),
    );
  }

  Future<void> _update(
    AppPreferences Function(AppPreferences) transform,
  ) async {
    final generation = _generation;
    final store = ref.read(appPreferencesStoreProvider);
    try {
      final updated = await _withPreferencesStore(store, () async {
        // Re-read inside the shared queue. A failed/corrupt read never falls
        // back to defaults and overwrites another setting or identity.
        final previous = await store.read() ?? const AppPreferences();
        final next = transform(previous);
        await store.write(next);
        return next;
      });
      if (ref.mounted && generation == _generation) {
        _lastSaved = updated;
        state = AsyncData(updated);
      }
    } on Object catch (error, stack) {
      if (ref.mounted && generation == _generation) {
        state = AsyncError<AppPreferences>(error, stack);
      }
      rethrow;
    }
  }
}

final appPreferencesProvider =
    AsyncNotifierProvider<AppPreferencesController, AppPreferences>(
      AppPreferencesController.new,
      retry: (retryCount, error) => null,
    );

final profitColorSchemeProvider = Provider<ProfitColorScheme>((ref) {
  final preferences =
      ref.watch(appPreferencesProvider).value ??
      ref.read(appPreferencesProvider.notifier).lastSaved;
  return preferences?.profitColorScheme ?? ProfitColorScheme.greenUp;
});

/// 金额显隐是本次 App 会话的临时状态，只在首次读到设置时应用启动默认值。
class AmountsHiddenController extends Notifier<bool> {
  bool _startupApplied = false;
  bool _manuallyChanged = false;

  @override
  bool build() {
    final initial = ref.read(appPreferencesProvider);
    _startupApplied = initial.hasValue && !initial.hasError;
    ref.listen(appPreferencesProvider, (_, next) {
      if (!_startupApplied &&
          next.hasValue &&
          !next.hasError &&
          !next.isLoading) {
        _startupApplied = true;
        if (!_manuallyChanged) state = next.requireValue.hideAmountsOnStartup;
      }
    });
    return _startupApplied ? initial.requireValue.hideAmountsOnStartup : true;
  }

  void toggle() => setHidden(!state);

  void setHidden(bool value) {
    _manuallyChanged = true;
    state = value;
  }
}

final amountsHiddenProvider = NotifierProvider<AmountsHiddenController, bool>(
  AmountsHiddenController.new,
);
