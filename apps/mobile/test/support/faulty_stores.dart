import 'package:flutter/services.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';

/// 安全存储故障替身：模拟 Keystore 主密钥失效（读取失败）或写入失败。
///
/// 真实实现抛出的就是平台异常；这里用 [PlatformException] 保持同一失败类别，
/// 避免测试把“存储不可用”误建模成协议错误。
class FaultyCredentialStore implements CredentialStore {
  FaultyCredentialStore({this.readFails = false, this.writeFails = false});

  /// 读取是否失败；测试可在流程中途改变以验证重试路径。
  bool readFails;

  /// 写入是否失败；同上。
  bool writeFails;

  CredentialSnapshot? snapshot;

  @override
  Future<CredentialSnapshot?> read() async {
    if (readFails) {
      throw PlatformException(code: 'keystore_unavailable', message: '无法解密本机凭据');
    }
    return snapshot;
  }

  @override
  Future<void> write(CredentialSnapshot value) async {
    if (writeFails) {
      throw PlatformException(code: 'keystore_write_failed', message: '无法写入本机凭据');
    }
    snapshot = value;
  }

  @override
  Future<void> clear() async {
    snapshot = null;
  }
}

/// Agent 会话 ID 存储故障替身；用于验证本机持久化失败不会锁死创建流程。
class FaultyAgentSessionStore implements AgentSessionStore {
  FaultyAgentSessionStore({this.writeFails = false});

  /// 写入是否失败；测试可在流程中途改变以验证重试路径。
  bool writeFails;

  String? sessionId;

  @override
  Future<String?> read() async => sessionId;

  @override
  Future<void> write(String value) async {
    if (writeFails) {
      throw PlatformException(code: 'keystore_write_failed', message: '无法写入会话 ID');
    }
    sessionId = value;
  }

  @override
  Future<void> clear() async {
    sessionId = null;
  }
}
