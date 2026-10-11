import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'secure_storage.dart';

// 同一底层凭据存储跨 SessionController 生命周期共用队列。所有生产安全存储
// 实例操作同一组键，必须全局串行；独立内存存储互不阻塞。
final _secureCredentialIdentity = Object();
final _credentialQueues = Expando<_CredentialQueue>();

Future<T> withCredentialMutation<T>(
  CredentialStore store,
  Future<T> Function() operation,
) {
  final identity = store is SecureCredentialStore
      ? _secureCredentialIdentity
      : store;
  final queue = _credentialQueues[identity] ??= _CredentialQueue();
  return queue.run(operation);
}

class _CredentialQueue {
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

/// 凭据安全存储抽象；实现负责把令牌放进系统安全存储（Android Keystore）。
abstract class CredentialStore {
  Future<CredentialSnapshot?> read();
  Future<void> write(CredentialSnapshot snapshot);
  Future<void> clear();
}

/// OAuth 登录后获得的服务端身份、令牌与设备私钥。
class CredentialSnapshot {
  const CredentialSnapshot({
    required this.serverUrl,
    required this.deviceId,
    required this.accessToken,
    required this.refreshToken,
    this.devicePrivateKey,
    this.authMethod,
    this.userEmail,
    this.googleSubject,
  });

  final String serverUrl;
  final String deviceId;
  final String accessToken;
  final String refreshToken;

  /// 缺失表示旧版配对凭据，迁移后必须重新登录。
  final String? authMethod;
  final String? userEmail;

  /// Google OIDC 稳定主体；仅用于同一服务端内的本机偏好隔离。
  final String? googleSubject;

  /// 设备 Ed25519 私钥（base64）；留在系统安全存储，不进入日志或缓存。
  final String? devicePrivateKey;
}

/// 基于 flutter_secure_storage 的实现；密钥材料交给系统安全存储。
///
/// Android 上使用 [kLyraSecureStorage] 的 EncryptedSharedPreferences 参数，
/// 不沿用插件的历史默认加密方案。
class SecureCredentialStore implements CredentialStore {
  const SecureCredentialStore({FlutterSecureStorage? storage})
    : _storage = storage ?? kLyraSecureStorage;

  final FlutterSecureStorage _storage;

  static const String _keyServerUrl = 'lyra.serverUrl';
  static const String _keyDeviceId = 'lyra.deviceId';
  static const String _keyAccessToken = 'lyra.accessToken';
  static const String _keyRefreshToken = 'lyra.refreshToken';

  @override
  Future<CredentialSnapshot?> read() async {
    final serverUrl = await _storage.read(key: _keyServerUrl);
    final deviceId = await _storage.read(key: _keyDeviceId);
    final accessToken = await _storage.read(key: _keyAccessToken);
    final refreshToken = await _storage.read(key: _keyRefreshToken);
    if (serverUrl == null ||
        deviceId == null ||
        accessToken == null ||
        refreshToken == null) {
      return null;
    }
    return CredentialSnapshot(
      serverUrl: serverUrl,
      deviceId: deviceId,
      accessToken: accessToken,
      refreshToken: refreshToken,
      devicePrivateKey: await _storage.read(key: _keyDevicePrivateKey),
      authMethod: await _storage.read(key: _keyAuthMethod),
      userEmail: await _storage.read(key: _keyUserEmail),
      googleSubject: await _storage.read(key: _keyGoogleSubject),
    );
  }

  @override
  Future<void> write(CredentialSnapshot snapshot) async {
    await _storage.write(key: _keyServerUrl, value: snapshot.serverUrl);
    await _storage.write(key: _keyDeviceId, value: snapshot.deviceId);
    await _storage.write(key: _keyAccessToken, value: snapshot.accessToken);
    await _storage.write(key: _keyRefreshToken, value: snapshot.refreshToken);
    await _storage.write(key: _keyAuthMethod, value: snapshot.authMethod);
    await _storage.write(key: _keyUserEmail, value: snapshot.userEmail);
    await _storage.write(key: _keyGoogleSubject, value: snapshot.googleSubject);
    final privateKey = snapshot.devicePrivateKey;
    if (privateKey != null) {
      await _storage.write(key: _keyDevicePrivateKey, value: privateKey);
    } else {
      await _storage.delete(key: _keyDevicePrivateKey);
    }
  }

  @override
  Future<void> clear() async {
    await _storage.delete(key: _keyServerUrl);
    await _storage.delete(key: _keyDeviceId);
    await _storage.delete(key: _keyAccessToken);
    await _storage.delete(key: _keyRefreshToken);
    await _storage.delete(key: _keyDevicePrivateKey);
    await _storage.delete(key: _keyAuthMethod);
    await _storage.delete(key: _keyUserEmail);
    await _storage.delete(key: _keyGoogleSubject);
  }

  static const String _keyDevicePrivateKey = 'lyra.devicePrivateKey';
  static const String _keyAuthMethod = 'lyra.authMethod';
  static const String _keyUserEmail = 'lyra.userEmail';
  static const String _keyGoogleSubject = 'lyra.googleSubject';
}

/// 测试与未登录状态使用的内存实现；不落盘。
class MemoryCredentialStore implements CredentialStore {
  CredentialSnapshot? _snapshot;

  @override
  Future<CredentialSnapshot?> read() async => _snapshot;

  @override
  Future<void> write(CredentialSnapshot snapshot) async {
    _snapshot = snapshot;
  }

  @override
  Future<void> clear() async {
    _snapshot = null;
  }
}
