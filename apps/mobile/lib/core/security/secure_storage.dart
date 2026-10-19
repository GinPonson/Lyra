import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// 全 App 共用的生产安全存储实例。
///
/// 登录凭据、本机偏好与 Agent 会话 ID 三处存储默认落在同一个
/// SharedPreferences 文件与键前缀上，参数必须完全一致：否则同一份数据会被
/// 按两种密文格式读写，互相读不出来。因此这里只声明一份参数。
///
/// 插件自带 `androidx.security:security-crypto`，应用 minSdk 24（≥ 23），
/// 所以 EncryptedSharedPreferences 在所有支持的设备上都可用。
const FlutterSecureStorage kLyraSecureStorage = FlutterSecureStorage(
  aOptions: AndroidOptions(
    // 交给 Jetpack Security 的 EncryptedSharedPreferences（Keystore 主密钥 +
    // AES-256-GCM），取代 flutter_secure_storage 9.x 的历史默认
    // （自实现 RSA-ECB 包裹密钥 + AES-CBC 加密值）。
    encryptedSharedPreferences: true,
    // 主密钥失效或密文损坏时不自动清空：静默清空会把用户直接变成“未登录”，
    // 抹掉现场且无从提示。改为由会话控制器降级为可见的未登录状态并提供
    // 重新登录入口。
    resetOnError: false,
  ),
);
