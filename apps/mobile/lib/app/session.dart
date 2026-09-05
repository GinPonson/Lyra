import 'dart:convert';
import 'dart:math' as math;

import 'package:cryptography/cryptography.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/api/lyra_api_client.dart';
import '../core/security/credential_store.dart';
import '../features/ledger/ledger_models.dart';
import '../features/ledger/ledger_repository.dart';
import '../features/pairing/pairing_repository.dart';

/// 会话控制器的持久化与网络依赖；测试可整体替换。
class SessionDependencies {
  const SessionDependencies({
    required this.credentialStore,
    this.clientFactory,
  });

  final CredentialStore credentialStore;

  /// 由 [serverUrl] 构建裸 Dio；生产用 Dio()，测试注入假适配器。
  final DioFactory? clientFactory;
}

typedef DioFactory = Dio Function(String serverUrl);

/// 会话状态：未配对或已配对（持有服务端身份、令牌与账本汇总）。
sealed class SessionState {
  const SessionState();
}

class SessionLoading extends SessionState {
  const SessionLoading();
}

class SessionUnpaired extends SessionState {
  const SessionUnpaired();
}

class SessionPaired extends SessionState {
  const SessionPaired({required this.snapshot, required this.summary});

  final CredentialSnapshot snapshot;
  final LedgerSummaryRead? summary;
}

/// 驱动 App 的会话生命周期：恢复已存凭据、配对、加载账本汇总。
class SessionController extends AsyncNotifier<SessionState> {
  LyraApiClient? _client;

  @override
  Future<SessionState> build() async {
    final dependencies = ref.watch(sessionDependenciesProvider);
    final snapshot = await dependencies.credentialStore.read();
    if (snapshot == null) {
      return const SessionUnpaired();
    }
    _client = _buildClient(dependencies, snapshot);
    try {
      final summary = await _loadSummary();
      return SessionPaired(snapshot: snapshot, summary: summary);
    } on Object {
      // 汇总加载失败不把会话打回未配对；已配对设备仍可重试或配对其他服务端。
      return SessionPaired(snapshot: snapshot, summary: null);
    }
  }

  /// 用服务端配对码兑换设备凭据：生成设备密钥对、配对并建立会话。
  Future<void> pair({
    required String serverUrl,
    required String code,
    required String displayName,
  }) async {
    final dependencies = ref.read(sessionDependenciesProvider);
    final keyPair = await Ed25519().newKeyPair();
    final publicKey = await keyPair.extractPublicKey();
    final publicKeyBytes = publicKey.bytes;
    final privateKeyBytes = await keyPair.extractPrivateKeyBytes();
    final publicKeyPem = _spkiPem(publicKeyBytes);

    final dio = dependencies.clientFactory?.call(serverUrl) ??
        Dio(BaseOptions(baseUrl: serverUrl));
    final credentials = await PairingRepository(dio: dio).pair(
      code: code,
      displayName: displayName,
      publicKeyPem: publicKeyPem,
    );
    final snapshot = CredentialSnapshot(
      serverUrl: serverUrl,
      deviceId: credentials.deviceId,
      accessToken: credentials.accessToken,
      refreshToken: credentials.refreshToken,
      devicePrivateKey: base64Encode(privateKeyBytes),
    );
    await dependencies.credentialStore.write(snapshot);
    _client = _buildClient(dependencies, snapshot);
    final summary = await _loadSummary();
    state = AsyncData(SessionPaired(snapshot: snapshot, summary: summary));
  }

  /// 退出配对：清除安全存储中的凭据并回到未配对状态。
  Future<void> unpair() async {
    final dependencies = ref.read(sessionDependenciesProvider);
    await dependencies.credentialStore.clear();
    _client = null;
    state = const AsyncData(SessionUnpaired());
  }

  LyraApiClient? get client => _client;

  LyraApiClient _buildClient(
    SessionDependencies dependencies,
    CredentialSnapshot snapshot,
  ) {
    return LyraApiClient(
      serverUrl: snapshot.serverUrl,
      credentialStore: dependencies.credentialStore,
      dio: dependencies.clientFactory?.call(snapshot.serverUrl),
      tokenRefresher: () async {
        final dio = dependencies.clientFactory?.call(snapshot.serverUrl) ??
            Dio(BaseOptions(baseUrl: snapshot.serverUrl));
        final credentials = await PairingRepository(
          dio: dio,
        ).refresh(refreshToken: snapshot.refreshToken);
        final updated = CredentialSnapshot(
          serverUrl: snapshot.serverUrl,
          deviceId: credentials.deviceId,
          accessToken: credentials.accessToken,
          refreshToken: credentials.refreshToken,
          devicePrivateKey: snapshot.devicePrivateKey,
        );
        await dependencies.credentialStore.write(updated);
        return credentials.accessToken;
      },
    );
  }

  Future<LedgerSummaryRead?> _loadSummary() async {
    final client = _client;
    if (client == null) {
      return null;
    }
    return LedgerRepository(client).summary(scope: 'all');
  }
}

/// Ed25519 公钥的 44 字节 SubjectPublicKeyInfo DER 包装为 PEM。
///
/// Ed25519 SPKI 结构是固定的：算法标识 + 32 字节公钥，无需完整 DER 库。
String _spkiPem(List<int> publicKeyBytes) {
  const ed25519SpkiPrefix = <int>[
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00,
  ];
  final der = <int>[...ed25519SpkiPrefix, ...publicKeyBytes];
  final base64Body = base64Encode(der);
  final lines = <String>[
    for (int offset = 0; offset < base64Body.length; offset += 64)
      base64Body.substring(
        offset,
        math.min(offset + 64, base64Body.length),
      ),
  ];
  return '-----BEGIN PUBLIC KEY-----\n${lines.join('\n')}\n-----END PUBLIC KEY-----';
}

/// 会话依赖的注入点；默认使用系统安全存储，测试用内存实现覆盖。
final Provider<SessionDependencies> sessionDependenciesProvider =
    Provider<SessionDependencies>(
  (ref) => SessionDependencies(credentialStore: SecureCredentialStore()),
);

final AsyncNotifierProvider<SessionController, SessionState> sessionProvider =
    AsyncNotifierProvider<SessionController, SessionState>(
  SessionController.new,
);
