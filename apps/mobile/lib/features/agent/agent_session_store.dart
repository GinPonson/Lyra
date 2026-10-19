import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../../core/security/secure_storage.dart';

/// Agent 活跃会话 ID 的本地持久化抽象。
abstract class AgentSessionStore {
  Future<String?> read();
  Future<void> write(String sessionId);
  Future<void> clear();
}

/// 系统安全存储实现。
///
/// 只持久化会话 ID：会话内容永远以服务端权威快照为准，本地不缓存任何
/// 金融数据；恢复时经 `GET /v1/agent/sessions/:id` 重新校验。
class SecureAgentSessionStore implements AgentSessionStore {
  const SecureAgentSessionStore({FlutterSecureStorage? storage})
    : _storage = storage ?? kLyraSecureStorage;

  final FlutterSecureStorage _storage;

  static const String _key = 'lyra.agent.sessionId';

  @override
  Future<String?> read() => _storage.read(key: _key);

  @override
  Future<void> write(String sessionId) =>
      _storage.write(key: _key, value: sessionId);

  @override
  Future<void> clear() => _storage.delete(key: _key);
}

/// 测试与离线场景使用的内存实现；不落盘。
class MemoryAgentSessionStore implements AgentSessionStore {
  String? _sessionId;

  @override
  Future<String?> read() async => _sessionId;

  @override
  Future<void> write(String sessionId) async {
    _sessionId = sessionId;
  }

  @override
  Future<void> clear() async {
    _sessionId = null;
  }
}
