import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// 凭据安全存储抽象；实现负责把令牌放进系统安全存储（Android Keystore）。
abstract class CredentialStore {
  Future<CredentialSnapshot?> read();
  Future<void> write(CredentialSnapshot snapshot);
  Future<void> clear();
}

/// 一组已配对设备的服务端身份、令牌与设备私钥。
class CredentialSnapshot {
  const CredentialSnapshot({
    required this.serverUrl,
    required this.deviceId,
    required this.accessToken,
    required this.refreshToken,
    this.devicePrivateKey,
  });

  final String serverUrl;
  final String deviceId;
  final String accessToken;
  final String refreshToken;

  /// 设备 Ed25519 私钥（base64）；留在系统安全存储，不进入日志或缓存。
  final String? devicePrivateKey;
}

/// 基于 flutter_secure_storage 的实现；密钥材料交给系统安全存储。
class SecureCredentialStore implements CredentialStore {
  const SecureCredentialStore({FlutterSecureStorage? storage})
    : _storage = storage ?? const FlutterSecureStorage();

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
    );
  }

  @override
  Future<void> write(CredentialSnapshot snapshot) async {
    await _storage.write(key: _keyServerUrl, value: snapshot.serverUrl);
    await _storage.write(key: _keyDeviceId, value: snapshot.deviceId);
    await _storage.write(key: _keyAccessToken, value: snapshot.accessToken);
    await _storage.write(key: _keyRefreshToken, value: snapshot.refreshToken);
    final privateKey = snapshot.devicePrivateKey;
    if (privateKey != null) {
      await _storage.write(key: _keyDevicePrivateKey, value: privateKey);
    }
  }

  @override
  Future<void> clear() async {
    await _storage.delete(key: _keyServerUrl);
    await _storage.delete(key: _keyDeviceId);
    await _storage.delete(key: _keyAccessToken);
    await _storage.delete(key: _keyRefreshToken);
    await _storage.delete(key: _keyDevicePrivateKey);
  }

  static const String _keyDevicePrivateKey = 'lyra.devicePrivateKey';
}

/// 测试与未配对状态使用的内存实现；不落盘。
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
