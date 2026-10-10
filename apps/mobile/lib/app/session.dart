import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:cryptography/cryptography.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/api/lyra_api_client.dart';
import '../core/api/lyra_api_error.dart';
import '../core/security/credential_store.dart';
import '../features/agent/agent_session_store.dart';
import '../features/auth/auth_repository.dart';
import '../features/ledger/ledger_models.dart';
import '../features/ledger/ledger_repository.dart';

class SessionDependencies {
  const SessionDependencies({
    required this.credentialStore,
    this.clientFactory,
    this.agentSessionStore,
    this.openAuthorizationUrl,
  });

  final CredentialStore credentialStore;
  final AgentSessionStore? agentSessionStore;
  final DioFactory? clientFactory;

  /// 系统浏览器入口；测试替换后不启动外部应用。
  final Future<bool> Function(Uri url)? openAuthorizationUrl;
}

typedef DioFactory = Dio Function(String serverUrl);

sealed class SessionState {
  const SessionState();
}

class SessionSignedOut extends SessionState {
  const SessionSignedOut({this.message});
  final String? message;
}

class SessionAuthenticated extends SessionState {
  const SessionAuthenticated({required this.snapshot, required this.summary});
  final CredentialSnapshot snapshot;
  final LedgerSummaryRead? summary;
}

class _PendingTokenRefresh {
  const _PendingTokenRefresh({required this.identity, required this.tokens});

  final CredentialSnapshot identity;
  final Future<RefreshedDeviceTokens> tokens;
}

/// OAuth 浏览器登录、设备凭据恢复、刷新轮换与退出。
class SessionController extends AsyncNotifier<SessionState> {
  LyraApiClient? _client;
  _PendingTokenRefresh? _pendingRefresh;
  int _loginGeneration = 0;
  int _authGeneration = 0;
  SessionState _publishedState = const SessionSignedOut();
  CancelToken? _loginCancel;
  Timer? _pollTimer;
  Completer<void>? _pollWake;

  @override
  Future<SessionState> build() async {
    final dependencies = ref.watch(sessionDependenciesProvider);
    final generation = ++_authGeneration;
    _client?.invalidate();
    _client = null;
    _publishedState = const SessionSignedOut();
    ref.onDispose(() {
      ++_authGeneration;
      cancelSignIn();
      _client?.invalidate();
      _client = null;
    });
    final snapshot = await withCredentialMutation(
      dependencies.credentialStore.read,
    );
    if (!_authActive(generation)) return _publishedState;
    if (snapshot == null)
      return _restored(generation, const SessionSignedOut());
    if (snapshot.authMethod != 'google') {
      await _expireIdentity(
        dependencies,
        snapshot,
        generation,
        '登录方式已更新，请使用 Google 重新登录。',
      );
      return _publishedState;
    }
    final client = _client = _buildClient(dependencies, snapshot, generation);
    try {
      final summary = await LedgerRepository(client).summary(scope: 'all');
      final latest = await withCredentialMutation(
        dependencies.credentialStore.read,
      );
      if (!_authActive(generation)) return _publishedState;
      if (!_sameIdentity(latest, snapshot))
        return _restored(generation, const SessionSignedOut());
      return _restored(
        generation,
        SessionAuthenticated(snapshot: latest!, summary: summary),
      );
    } on LyraApiError catch (error) {
      if (!_authActive(generation)) return _publishedState;
      if (error.statusCode == 401) {
        await _expireIdentity(
          dependencies,
          snapshot,
          generation,
          '登录已过期，请重新登录。',
        );
        return _publishedState;
      }
      return _restored(
        generation,
        SessionAuthenticated(snapshot: snapshot, summary: null),
      );
    } on Object {
      return _restored(
        generation,
        SessionAuthenticated(snapshot: snapshot, summary: null),
      );
    }
  }

  bool _authActive(int generation) =>
      ref.mounted && generation == _authGeneration;

  SessionState _restored(int generation, SessionState value) {
    if (_authActive(generation)) _publishedState = value;
    return _publishedState;
  }

  void _publish(SessionState value) {
    _publishedState = value;
    if (ref.mounted) state = AsyncData(value);
  }

  bool _sameIdentity(CredentialSnapshot? left, CredentialSnapshot right) =>
      left != null &&
      left.deviceId == right.deviceId &&
      left.serverUrl == right.serverUrl;

  /// 凭据只从 verifier 绑定的交换接口返回，浏览器不接收 Lyra 令牌。
  Future<bool> signInWithGoogle({
    required String serverUrl,
    required String displayName,
  }) async {
    cancelSignIn();
    final generation = _loginGeneration;
    final authGeneration = ++_authGeneration;
    _client?.invalidate();
    _client = null;
    _publish(const SessionSignedOut());
    final cancel = _loginCancel = CancelToken();
    final dependencies = ref.read(sessionDependenciesProvider);
    bool active() =>
        _authActive(authGeneration) && generation == _loginGeneration;

    try {
      final random = math.Random.secure();
      final verifier = base64UrlEncode(
        List<int>.generate(32, (_) => random.nextInt(256)),
      ).replaceAll('=', '');
      final challenge = base64UrlEncode(
        (await Sha256().hash(utf8.encode(verifier))).bytes,
      ).replaceAll('=', '');
      final keyPair = await Ed25519().newKeyPair();
      final publicKey = await keyPair.extractPublicKey();
      final privateKey = await keyPair.extractPrivateKeyBytes();
      if (!active()) return false;

      final repository = AuthRepository(
        dio: _bareClient(dependencies, serverUrl),
      );
      final login = await repository.startGoogleLogin(
        displayName: displayName,
        publicKeyPem: _spkiPem(publicKey.bytes),
        codeChallenge: challenge,
        cancelToken: cancel,
      );
      if (!active()) return false;
      final launch =
          dependencies.openAuthorizationUrl ??
          (Uri url) => launchUrl(url, mode: LaunchMode.externalApplication);
      if (!await launch(login.authorizationUrl)) {
        throw const LyraApiError(
          code: 'LOGIN_BROWSER_UNAVAILABLE',
          message: '无法打开浏览器，请重试。',
          statusCode: 0,
          retryable: true,
        );
      }
      final maximumDeadline = DateTime.now().add(const Duration(minutes: 10));
      final deadline = login.expiresAt.isBefore(maximumDeadline)
          ? login.expiresAt
          : maximumDeadline;
      while (active()) {
        if (!DateTime.now().isBefore(deadline)) {
          throw const LyraApiError(
            code: 'LOGIN_EXPIRED',
            message: '登录已超时，请重新开始。',
            statusCode: 410,
            retryable: true,
          );
        }
        final credentials = await repository.exchangeGoogleLogin(
          loginId: login.loginId,
          codeVerifier: verifier,
          cancelToken: cancel,
        );
        if (!active()) return false;
        if (credentials != null) {
          final snapshot = CredentialSnapshot(
            serverUrl: serverUrl,
            deviceId: credentials.deviceId,
            accessToken: credentials.accessToken,
            refreshToken: credentials.refreshToken,
            devicePrivateKey: base64Encode(privateKey),
            authMethod: 'google',
            userEmail: credentials.userEmail,
          );
          return withCredentialMutation(() async {
            if (!active()) return false;
            await dependencies.credentialStore.write(snapshot);
            // 取消/新登录可能发生在安全存储 await 期间；回滚仍在同一队列内，
            // 所以下一次登录只能在旧写入与回滚全部完成后提交。
            if (!active()) {
              await dependencies.credentialStore.clear();
              return false;
            }
            await _sessionStore(dependencies).clear();
            if (!active()) {
              await dependencies.credentialStore.clear();
              return false;
            }
            _client = _buildClient(dependencies, snapshot, authGeneration);
            _publish(SessionAuthenticated(snapshot: snapshot, summary: null));
            return true;
          });
        }
        await _waitForPoll(login.pollInterval);
      }
      return false;
    } on DioException catch (error) {
      if (CancelToken.isCancel(error)) return false;
      rethrow;
    } finally {
      if (generation == _loginGeneration) {
        _loginCancel = null;
        _cancelPoll();
      }
    }
  }

  /// 用户取消或界面销毁即停止轮询，过期事务由服务端清理。
  void cancelSignIn() {
    _loginGeneration++;
    _loginCancel?.cancel();
    _loginCancel = null;
    _cancelPoll();
  }

  Future<void> _waitForPoll(Duration interval) {
    final wake = _pollWake = Completer<void>();
    _pollTimer = Timer(interval, () {
      if (!wake.isCompleted) wake.complete();
    });
    return wake.future;
  }

  void _cancelPoll() {
    _pollTimer?.cancel();
    _pollTimer = null;
    final wake = _pollWake;
    _pollWake = null;
    if (wake != null && !wake.isCompleted) wake.complete();
  }

  /// 本机先退出；随后只用捕获的旧设备凭据撤销服务端会话。
  Future<void> signOut() async {
    final pendingRefresh = _pendingRefresh;
    cancelSignIn();
    ++_authGeneration;
    final dependencies = ref.read(sessionDependenciesProvider);
    _client?.invalidate();
    _client = null;
    _publish(const SessionSignedOut());
    final CredentialSnapshot? snapshot;
    try {
      snapshot = await withCredentialMutation(() async {
        final previous = await dependencies.credentialStore.read();
        await dependencies.credentialStore.clear();
        await _sessionStore(dependencies).clear();
        return previous;
      });
    } on Object {
      throw const LyraApiError(
        code: 'LOCAL_LOGOUT_FAILED',
        message: '无法清除本机登录，请重试。',
        statusCode: 0,
        retryable: true,
      );
    }
    if (snapshot == null) return;
    final repository = AuthRepository(
      dio: _bareClient(dependencies, snapshot.serverUrl),
    );
    var accessToken = snapshot.accessToken;
    var refreshToken = snapshot.refreshToken;
    if (pendingRefresh != null &&
        _sameIdentity(pendingRefresh.identity, snapshot)) {
      try {
        // 刷新可能已在服务端消耗旧令牌；等待同一设备的结果仅用于撤销，
        // 不把迟到的令牌写回本机，也不影响另一轮登录。
        final tokens = await pendingRefresh.tokens.timeout(
          const Duration(seconds: 45),
        );
        accessToken = tokens.accessToken;
        refreshToken = tokens.refreshToken;
      } on Object {
        // 刷新失败或等待超时仍尝试撤销已捕获的凭据。
      }
    }
    try {
      await repository.logout(accessToken: accessToken);
    } on LyraApiError catch (error) {
      if (error.statusCode != 401) rethrow;
      // 仅为旧设备撤销刷新；绝不写回已退出或另一次登录的凭据。
      final tokens = await repository.refresh(refreshToken: refreshToken);
      await repository.logout(accessToken: tokens.accessToken);
    }
  }

  LyraApiClient? get client => _client;

  Future<void> _expireIdentity(
    SessionDependencies dependencies,
    CredentialSnapshot identity,
    int generation,
    String message,
  ) {
    return withCredentialMutation(() async {
      if (!_authActive(generation)) return;
      final latest = await dependencies.credentialStore.read();
      if (!_authActive(generation) || !_sameIdentity(latest, identity)) return;
      ++_authGeneration;
      _client?.invalidate();
      _client = null;
      _publish(SessionSignedOut(message: message));
      await dependencies.credentialStore.clear();
      await _sessionStore(dependencies).clear();
    });
  }

  AgentSessionStore _sessionStore(SessionDependencies dependencies) =>
      dependencies.agentSessionStore ?? const SecureAgentSessionStore();

  Dio _bareClient(SessionDependencies dependencies, String serverUrl) =>
      dependencies.clientFactory?.call(serverUrl) ??
      Dio(
        BaseOptions(
          baseUrl: serverUrl,
          connectTimeout: const Duration(seconds: 10),
          receiveTimeout: const Duration(seconds: 30),
        ),
      );

  LyraApiClient _buildClient(
    SessionDependencies dependencies,
    CredentialSnapshot identity,
    int generation,
  ) {
    return LyraApiClient(
      serverUrl: identity.serverUrl,
      deviceId: identity.deviceId,
      credentialStore: dependencies.credentialStore,
      dio: dependencies.clientFactory?.call(identity.serverUrl),
      tokenRefresher: () async {
        final current = await withCredentialMutation(() async {
          if (!_authActive(generation)) return null;
          final value = await dependencies.credentialStore.read();
          return _authActive(generation) && _sameIdentity(value, identity)
              ? value
              : null;
        });
        if (current == null || !_authActive(generation)) return null;
        final pending = _PendingTokenRefresh(
          identity: identity,
          tokens: AuthRepository(
            dio: _bareClient(dependencies, identity.serverUrl),
          ).refresh(refreshToken: current.refreshToken),
        );
        _pendingRefresh = pending;
        try {
          final tokens = await pending.tokens;
          return await withCredentialMutation(() async {
            if (!_authActive(generation)) return null;
            final latest = await dependencies.credentialStore.read();
            if (!_authActive(generation) || !_sameIdentity(latest, identity))
              return null;
            if (latest!.refreshToken != current.refreshToken)
              return latest.accessToken;
            final updated = CredentialSnapshot(
              serverUrl: current.serverUrl,
              deviceId: current.deviceId,
              accessToken: tokens.accessToken,
              refreshToken: tokens.refreshToken,
              devicePrivateKey: current.devicePrivateKey,
              authMethod: current.authMethod,
              userEmail: current.userEmail,
            );
            await dependencies.credentialStore.write(updated);
            if (!_authActive(generation)) return null;
            final value = _publishedState;
            if (value is SessionAuthenticated &&
                _sameIdentity(value.snapshot, identity)) {
              _publish(
                SessionAuthenticated(snapshot: updated, summary: value.summary),
              );
            }
            return tokens.accessToken;
          });
        } finally {
          if (identical(_pendingRefresh, pending)) _pendingRefresh = null;
        }
      },
      onUnauthorized: () =>
          _expireIdentity(dependencies, identity, generation, '登录已过期，请重新登录。'),
    );
  }
}

String _spkiPem(List<int> publicKeyBytes) {
  const prefix = <int>[
    0x30,
    0x2a,
    0x30,
    0x05,
    0x06,
    0x03,
    0x2b,
    0x65,
    0x70,
    0x03,
    0x21,
    0x00,
  ];
  final body = base64Encode(<int>[...prefix, ...publicKeyBytes]);
  final lines = <String>[
    for (int offset = 0; offset < body.length; offset += 64)
      body.substring(offset, math.min(offset + 64, body.length)),
  ];
  return '-----BEGIN PUBLIC KEY-----\n${lines.join('\n')}\n-----END PUBLIC KEY-----';
}

final sessionDependenciesProvider = Provider<SessionDependencies>(
  (ref) => SessionDependencies(
    credentialStore: SecureCredentialStore(),
    agentSessionStore: const SecureAgentSessionStore(),
  ),
);

final sessionProvider = AsyncNotifierProvider<SessionController, SessionState>(
  SessionController.new,
);
