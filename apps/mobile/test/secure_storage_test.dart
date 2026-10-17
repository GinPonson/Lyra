import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/security/secure_storage.dart';

void main() {
  test('生产安全存储启用 EncryptedSharedPreferences 且不自动清空数据', () {
    final options = kLyraSecureStorage.aOptions.toMap();
    // 历史默认是自实现 RSA-ECB 包裹 + AES-CBC；这里必须显式改用
    // Jetpack Security 的 EncryptedSharedPreferences（Keystore 主密钥 + AES-GCM）。
    expect(options['encryptedSharedPreferences'], 'true');
    // 密钥失效时不能静默清空：由会话控制器降级为可见的未登录状态。
    expect(options['resetOnError'], 'false');
  });

  test('不改动存储命名空间，避免已有密文读不出来', () {
    final options = kLyraSecureStorage.aOptions.toMap();
    expect(options['sharedPreferencesName'], isEmpty);
    expect(options['preferencesKeyPrefix'], isEmpty);
  });
}
