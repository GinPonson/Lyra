import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:cryptography/cryptography.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/api/lyra_api_client.dart';
import '../core/api/lyra_api_error.dart';
import '../core/preferences/app_preferences.dart';
import '../core/security/credential_store.dart';
import '../features/agent/agent_session_store.dart';
import '../features/auth/auth_repository.dart';
import '../features/auth/auth_provider.dart';
import '../features/auth/authorization_browser.dart';
import '../features/auth/user_profile.dart';
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

  /// 系统授权窗口入口；Future 在授权窗口关闭后完成，测试可替换它避免打开浏览器。
  final Future<bool> Function(Uri url)? openAuthorizationUrl;
}

typedef DioFactory = Dio Function(String serverUrl);

sealed class SessionState {
  const SessionState();
}

class SessionSignedOut extends SessionState {
  const SessionSignedOut({
    this.message,
    this.retrySignOut = false,
    this.serverUrl,
  });
  final String? message;
  final bool retrySignOut;

  /// 只保留原账本的路由，不保留身份或令牌；偏好写失败时仍可原地重新登录。
  final String? serverUrl;
}

class SessionAuthenticated extends SessionState {
  const SessionAuthenticated({required this.snapshot, required this.summary});
  final CredentialSnapshot snapshot;
  final LedgerSummaryRead? summary;
}

/// 第三方身份已通过验证，但新用户尚未通过邀请码完成 Lyra 注册。
///
/// 这里只发布界面所需的来源、地址与截止时间；PKCE、设备私钥和登录事务留在内存中。
class SessionRegistrationRequired extends SessionState {
  const SessionRegistrationRequired({
    required this.provider,
    required this.serverUrl,
    required this.expiresAt,
  });

  final AuthProvider provider;
  final String serverUrl;
  final DateTime expiresAt;
}

/// 保留一次已授权尝试的证明，补邀请码时继续兑换同一事务，绝不重新打开浏览器。
class _PendingRegistration {
  const _PendingRegistration({
    required this.provider,
    required this.serverUrl,
    required this.login,
    required this.codeVerifier,
    required this.devicePrivateKey,
    required this.deadline,
    required this.repository,
    required this.cancel,
    required this.loginGeneration,
    required this.authGeneration,
  });

  final AuthProvider provider;
  final String serverUrl;
  final OAuthLoginStart login;
  final String codeVerifier;
  final String devicePrivateKey;
  final DateTime deadline;
  final AuthRepository repository;
  final CancelToken cancel;
  final int loginGeneration;
  final int authGeneration;
}

class _PendingTokenRefresh {
  const _PendingTokenRefresh({required this.identity, required this.tokens});

  final CredentialSnapshot identity;
  final Future<RefreshedDeviceTokens> tokens;
}

/// 账号停用后的统一提示；停用与令牌过期的处置相同（结束会话），原因不同。
const String _userDisabledMessage = '账号已停用，请联系管理员。';

/// OAuth 浏览器登录、设备凭据恢复、刷新轮换与退出。
class SessionController extends AsyncNotifier<SessionState> {
  LyraApiClient? _client;
  _PendingTokenRefresh? _pendingRefresh;
  _PendingRegistration? _pendingRegistration;
  bool _registrationSubmitting = false;
  int _loginGeneration = 0;
  int _authGeneration = 0;
  int _ledgerRefreshGeneration = 0;
  SessionState _publishedState = const SessionSignedOut();
  CancelToken? _loginCancel;
  Timer? _pollTimer;
  Completer<void>? _pollWake;

  @override
  Future<SessionState> build() async {
    final dependencies = ref.watch(sessionDependenciesProvider);
    final generation = ++_authGeneration;
    cancelSignIn();
    _client?.invalidate();
    _client = null;
    _publishedState = const SessionSignedOut();
    ref.onDispose(() {
      ++_authGeneration;
      cancelSignIn();
      _client?.invalidate();
      _client = null;
    });
    // 安全存储可能不可读（Keystore 主密钥失效、密文损坏）。若让异常留在
    // build 的 Future 里，总览会停在永远重试的加载错误上，用户没有任何自助
    // 入口；这里降级为可见的未登录状态，并给出可执行的下一步。
    final CredentialSnapshot? snapshot;
    try {
      snapshot = await withCredentialMutation(
        dependencies.credentialStore,
        dependencies.credentialStore.read,
      );
    } on Object {
      return _restored(
        generation,
        const SessionSignedOut(message: '暂时无法恢复登录，请重新登录。'),
      );
    }
    if (!_authActive(generation)) return _publishedState;
    if (snapshot == null) {
      return _restored(generation, const SessionSignedOut());
    }
    if (AuthProvider.fromId(snapshot.authMethod) == null) {
      await _expireIdentity(
        dependencies,
        snapshot,
        generation,
        '登录方式已更新，请重新登录。',
      );
      return _publishedState;
    }
    // 稳定 userId 是唯一身份锚点。旧凭据缺失它时无法证明归属，必须清理会话
    // 并要求重新登录，不能按邮箱或第三方 subject 猜测属于哪个用户。
    if (snapshot.userId == null || snapshot.userId!.isEmpty) {
      await _expireIdentity(
        dependencies,
        snapshot,
        generation,
        '登录凭据已更新，请重新登录。',
      );
      return _publishedState;
    }
    final client = _client = _buildClient(dependencies, snapshot, generation);
    try {
      final summary = await LedgerRepository(client).summary(scope: 'all');
      final latest = await withCredentialMutation(
        dependencies.credentialStore,
        dependencies.credentialStore.read,
      );
      if (!_authActive(generation)) return _publishedState;
      if (!_sameIdentity(latest, snapshot)) {
        return _restored(generation, const SessionSignedOut());
      }
      // 账本读取成功只说明令牌有效；再用 /me 核对服务端解析出的身份与本机
      // 凭据一致，避免把上一个身份的账本交给下一个登录。
      await _verifyRestoredIdentity(dependencies, client, latest!, generation);
      if (!_authActive(generation)) return _publishedState;
      return _restored(
        generation,
        SessionAuthenticated(snapshot: latest, summary: summary),
      );
    } on LyraApiError catch (error) {
      if (!_authActive(generation)) return _publishedState;
      if (error.statusCode == 401 || error.code == kUserDisabledCode) {
        await _expireIdentity(
          dependencies,
          snapshot,
          generation,
          error.code == kUserDisabledCode
              ? _userDisabledMessage
              : '登录已过期，请重新登录。',
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

  /// 用 `GET /v1/me` 核对服务端当前身份与本机凭据的稳定 userId 是否一致。
  ///
  /// 只有明确的身份不一致才结束会话。401 与停用已由客户端回调结束会话；网络、
  /// 服务故障或响应形态不符只是本次核对未完成，保留已恢复的会话（账本读取会
  /// 单独给出可重试错误），不把连接失败伪装成身份不符或账号停用。
  Future<void> _verifyRestoredIdentity(
    SessionDependencies dependencies,
    LyraApiClient client,
    CredentialSnapshot identity,
    int generation,
  ) async {
    final LyraUserProfile profile;
    try {
      profile = await UserProfileRepository(client).me();
    } on LyraApiError {
      return;
    } on FormatException {
      return;
    }
    if (!_authActive(generation) || profile.userId == identity.userId) return;
    await _expireIdentity(dependencies, identity, generation, '登录身份已变化，请重新登录。');
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

  /// 身份同一性判定包含稳定 userId：同一设备换用户后，旧请求的结果不得写回。
  bool _sameIdentity(CredentialSnapshot? left, CredentialSnapshot right) =>
      left != null &&
      left.deviceId == right.deviceId &&
      left.serverUrl == right.serverUrl &&
      left.userId == right.userId;

  /// 凭据只从 verifier 绑定的交换接口返回，浏览器不接收 Lyra 令牌。
  Future<bool> signInWithGoogle({
    required String serverUrl,
    required String displayName,
  }) => signIn(
    provider: AuthProvider.google,
    serverUrl: serverUrl,
    displayName: displayName,
  );

  /// 验证指定来源的身份并建立会话；新用户需要邀请码时发布注册状态并返回 false。
  ///
  /// [serverUrl] 是已规范化的后台地址，[displayName] 是当前设备名称。
  /// 只有兑换凭据并安全保存后才返回 true；授权取消返回 false，明确失败向上抛出。
  Future<bool> signIn({
    required AuthProvider provider,
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
      final login = await repository.startLogin(
        provider: provider,
        displayName: displayName,
        publicKeyPem: _spkiPem(publicKey.bytes),
        codeChallenge: challenge,
        cancelToken: cancel,
      );
      if (!active()) return false;
      final launch =
          dependencies.openAuthorizationUrl ??
          (Uri url) => authorizeInBrowser(url, cancel);
      // 授权窗口由系统在固定回跳时关闭；回到 App 后才开始兑换，避免后台冻结
      // 导致网络栈与轮询失步。回跳本身不表示登录成功，仍须核对服务端的用户对象。
      if (!await launch(login.authorizationUrl)) {
        throw const LyraApiError(
          code: 'LOGIN_BROWSER_UNAVAILABLE',
          message: '无法打开浏览器，请重试。',
          statusCode: 0,
          retryable: true,
        );
      }
      if (!active()) return false;
      final maximumDeadline = DateTime.now().add(const Duration(minutes: 10));
      final deadline = login.expiresAt.isBefore(maximumDeadline)
          ? login.expiresAt
          : maximumDeadline;
      // 轮询期间的**瞬时失败不判定登录失败**。
      //
      // 浏览器授权可能已经成功、服务端事务正在等待兑换（10 分钟 TTL），此时一次
      // 网络抖动就放弃会让用户白跑一趟授权、还得多试一次。这里只容忍"再试可能
      // 成功"的错误（仅网络层失败），连续失败超过预算仍然如实失败——
      // 不能把真实故障伪装成"还在等待"。
      // 冻结恢复后网络栈需要时间重新就绪，几次快速失败不代表授权失败；
      // 预算给到约 1 分钟，仍由 10 分钟的登录截止时间兜底。
      const transientBudget = 30;
      var transientFailures = 0;
      while (active()) {
        if (!DateTime.now().isBefore(deadline)) {
          throw const LyraApiError(
            code: 'LOGIN_EXPIRED',
            message: '登录已超时，请重新开始。',
            statusCode: 410,
            retryable: true,
          );
        }
        LoginCredentials? credentials;
        try {
          credentials = await repository.exchangeLogin(
            provider: provider,
            loginId: login.loginId,
            codeVerifier: verifier,
            cancelToken: cancel,
          );
          transientFailures = 0;
        } on LyraApiError catch (error) {
          if (error.code == 'INVITE_CODE_REQUIRED') {
            if (!active()) return false;
            // 缺邀请码是已经验证身份后的注册步骤，不是失败或已登录状态。
            // 不把未完成注册的私钥与证明写进安全存储，重启后必须重新验证身份。
            _pendingRegistration = _PendingRegistration(
              provider: provider,
              serverUrl: serverUrl,
              login: login,
              codeVerifier: verifier,
              devicePrivateKey: base64Encode(privateKey),
              deadline: deadline,
              repository: repository,
              cancel: cancel,
              loginGeneration: generation,
              authGeneration: authGeneration,
            );
            _publish(
              SessionRegistrationRequired(
                provider: provider,
                serverUrl: serverUrl,
                expiresAt: deadline,
              ),
            );
            return false;
          }
          if (!_isTransientPollFailure(error)) rethrow;
          transientFailures++;
          if (transientFailures >= transientBudget) rethrow;
          await _waitForPoll(login.pollInterval);
          continue;
        }
        if (!active()) return false;
        if (credentials != null) {
          return await _persistLogin(
            dependencies: dependencies,
            credentials: credentials,
            serverUrl: serverUrl,
            devicePrivateKey: base64Encode(privateKey),
            provider: provider,
            authGeneration: authGeneration,
            active: active,
          );
        }
        await _waitForPoll(login.pollInterval);
      }
      return false;
    } on LyraApiError catch (error) {
      if (error.code == 'LOGIN_CANCELLED') return false;
      rethrow;
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

  /// 对当前已授权的新用户补交邀请码，成功建立会话；取消或被替换时返回 false。
  ///
  /// [inviteCode] 为大小写敏感的邀请码，去掉首尾空白后发送。错码及网络错误保留
  /// 当前注册步骤并抛给表单；过期、重放或账号停用会结束尝试并回到可重新登录状态。
  Future<bool> completeRegistration({required String inviteCode}) async {
    final pending = _pendingRegistration;
    if (pending == null || !_registrationActive(pending)) {
      throw const LyraApiError(
        code: 'LOGIN_STATE_INVALID',
        message: '此次登录已失效，请重新开始。',
        statusCode: 409,
        retryable: false,
      );
    }
    if (_registrationSubmitting) {
      throw const LyraApiError(
        code: 'LOGIN_IN_PROGRESS',
        message: '正在完成注册，请稍候。',
        statusCode: 409,
        retryable: true,
      );
    }
    if (!DateTime.now().isBefore(pending.deadline)) {
      cancelSignIn();
      _publish(
        SessionSignedOut(serverUrl: pending.serverUrl, message: '登录已超时，请重新开始。'),
      );
      throw const LyraApiError(
        code: 'LOGIN_EXPIRED',
        message: '登录已超时，请重新开始。',
        statusCode: 410,
        retryable: false,
      );
    }
    final code = inviteCode.trim();
    if (code.isEmpty) {
      throw const LyraApiError(
        code: 'INVITE_CODE_REQUIRED',
        message: '请填写邀请码。',
        statusCode: 403,
        retryable: true,
      );
    }
    _registrationSubmitting = true;
    try {
      final credentials = await pending.repository.exchangeLogin(
        provider: pending.provider,
        loginId: pending.login.loginId,
        codeVerifier: pending.codeVerifier,
        inviteCode: code,
        cancelToken: pending.cancel,
      );
      if (!_registrationActive(pending)) return false;
      if (credentials == null) {
        throw const FormatException('已授权的注册事务返回了未授权状态');
      }
      final signedIn = await _persistLogin(
        dependencies: ref.read(sessionDependenciesProvider),
        credentials: credentials,
        serverUrl: pending.serverUrl,
        devicePrivateKey: pending.devicePrivateKey,
        provider: pending.provider,
        authGeneration: pending.authGeneration,
        active: () => _registrationActive(pending),
      );
      if (identical(_pendingRegistration, pending)) {
        _pendingRegistration = null;
        _registrationSubmitting = false;
      }
      return signedIn;
    } on LyraApiError catch (error) {
      if (!_registrationActive(pending)) return false;
      final terminalMessage = switch (error.code) {
        'LOGIN_EXPIRED' => '登录已超时，请重新开始。',
        'LOGIN_STATE_INVALID' || 'LOGIN_ALREADY_REDEEMED' => '此次登录已失效，请重新开始。',
        'LOGIN_FAILED' || 'LOGIN_DENIED' => '登录未能完成，请重新登录。',
        kUserDisabledCode => _userDisabledMessage,
        _ => null,
      };
      if (terminalMessage != null) {
        cancelSignIn();
        _publish(
          SessionSignedOut(
            serverUrl: pending.serverUrl,
            message: terminalMessage,
          ),
        );
      }
      rethrow;
    } on DioException catch (error) {
      if (CancelToken.isCancel(error)) return false;
      rethrow;
    } finally {
      if (identical(_pendingRegistration, pending)) {
        _registrationSubmitting = false;
      }
    }
  }

  /// 返回登录首页并撤销尚未完成的注册；旧表单不能影响已建立或已替换的新会话。
  void cancelRegistration() {
    if (_pendingRegistration == null ||
        _publishedState is! SessionRegistrationRequired) {
      return;
    }
    cancelSignIn();
  }

  /// 核对注册仍属于当前登录代次，防止取消、换身份或销毁后的响应写回。
  bool _registrationActive(_PendingRegistration pending) =>
      identical(_pendingRegistration, pending) &&
      _authActive(pending.authGeneration) &&
      pending.loginGeneration == _loginGeneration;

  /// 将普通登录或邀请注册的凭据通过同一串行边界安全提交，返回是否建立了会话。
  ///
  /// [active] 在每个存储等待点核对尝试归属；被取消的写入在同一队列内回滚。
  /// [devicePrivateKey] 是本次设备生成的 base64 私钥，不能取自之前身份的凭据。
  Future<bool> _persistLogin({
    required SessionDependencies dependencies,
    required LoginCredentials credentials,
    required String serverUrl,
    required String devicePrivateKey,
    required AuthProvider provider,
    required int authGeneration,
    required bool Function() active,
  }) async {
    if (!active()) return false;
    // 停用账号只能得到明确的不可重试错误；即便服务端返回 200 也不建立会话。
    if (credentials.user.status == LyraUserStatus.disabled) {
      throw const LyraApiError(
        code: kUserDisabledCode,
        message: _userDisabledMessage,
        statusCode: 403,
        retryable: false,
      );
    }
    final snapshot = CredentialSnapshot(
      serverUrl: serverUrl,
      deviceId: credentials.deviceId,
      accessToken: credentials.accessToken,
      refreshToken: credentials.refreshToken,
      userId: credentials.user.userId,
      devicePrivateKey: devicePrivateKey,
      authMethod: provider.id,
      userEmail: credentials.userEmail,
      userDisplayName: credentials.userDisplayName,
      providerSubject: credentials.subject,
      googleSubject: credentials.googleSubject,
    );
    return withCredentialMutation(dependencies.credentialStore, () async {
      if (!active()) return false;
      await dependencies.credentialStore.write(snapshot);
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

  /// 用户取消或界面销毁即停止轮询，过期事务由服务端清理。
  void cancelSignIn() {
    _loginGeneration++;
    _loginCancel?.cancel();
    _loginCancel = null;
    final pending = _pendingRegistration;
    _pendingRegistration = null;
    _registrationSubmitting = false;
    pending?.cancel.cancel();
    if (pending != null && _publishedState is SessionRegistrationRequired) {
      _publish(SessionSignedOut(serverUrl: pending.serverUrl));
    }
    _cancelPoll();
  }

  /// 轮询期间值得再试的错误：**仅限网络层失败**（没有拿到任何响应）。
  ///
  /// 服务端给出的 4xx/5xx 都是明确结论，重试只会让用户多等：403 是停用或邀请码，
  /// 409 是已兑换，410 是已过期，5xx 是服务端自己坏了——这些立刻如实报错。
  bool _isTransientPollFailure(LyraApiError error) => error.statusCode == 0;

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
    final serverUrl = switch (_publishedState) {
      SessionAuthenticated(:final snapshot) => snapshot.serverUrl,
      SessionSignedOut(:final serverUrl) => serverUrl,
      SessionRegistrationRequired(:final serverUrl) => serverUrl,
    };
    final pendingRefresh = _pendingRefresh;
    cancelSignIn();
    final generation = ++_authGeneration;
    final dependencies = ref.read(sessionDependenciesProvider);
    _client?.invalidate();
    _client = null;
    _publish(SessionSignedOut(serverUrl: serverUrl));
    final CredentialSnapshot? snapshot;
    try {
      snapshot = await withCredentialMutation(
        dependencies.credentialStore,
        () async {
          final previous = await dependencies.credentialStore.read();
          await dependencies.credentialStore.clear();
          await _sessionStore(dependencies).clear();
          return previous;
        },
      );
    } on Object {
      if (_authActive(generation)) {
        _publish(
          SessionSignedOut(
            message: '无法清除本机登录，请重试。',
            retrySignOut: true,
            serverUrl: serverUrl,
          ),
        );
      }
      throw const LyraApiError(
        code: 'LOCAL_LOGOUT_FAILED',
        message: '无法清除本机登录，请重试。',
        statusCode: 0,
        retryable: true,
      );
    }
    await _rememberLoginServer(snapshot?.serverUrl ?? serverUrl, generation);
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
      try {
        await repository.logout(accessToken: accessToken);
      } on LyraApiError catch (error) {
        if (error.statusCode != 401) rethrow;
        // 仅为旧设备撤销刷新；绝不写回已退出或另一次登录的凭据。
        final tokens = await repository.refresh(refreshToken: refreshToken);
        await repository.logout(accessToken: tokens.accessToken);
      }
    } on Object {
      if (_authActive(generation)) {
        _publish(
          SessionSignedOut(
            message: '已退出此设备登录，但暂时无法完成全部退出操作。',
            serverUrl: serverUrl,
          ),
        );
      }
      rethrow;
    }
  }

  LyraApiClient? get client => _client;

  /// 刷新账本汇总不重建登录会话，保留新建/选择的证券账户范围。
  /// 写入已成功但读请求失败时清除旧汇总，让页面进入可重试读取状态。
  Future<bool> refreshLedger() async {
    final generation = _authGeneration;
    final refreshGeneration = ++_ledgerRefreshGeneration;
    final client = _client;
    final before = _publishedState;
    if (client == null || before is! SessionAuthenticated) return false;
    bool active() =>
        _authActive(generation) &&
        refreshGeneration == _ledgerRefreshGeneration &&
        identical(_client, client);
    try {
      final summary = await LedgerRepository(client).summary(scope: 'all');
      final current = _publishedState;
      if (!active() || current is! SessionAuthenticated) return false;
      _publish(
        SessionAuthenticated(snapshot: current.snapshot, summary: summary),
      );
      return true;
    } on Object {
      final current = _publishedState;
      if (active() && current is SessionAuthenticated) {
        _publish(
          SessionAuthenticated(snapshot: current.snapshot, summary: null),
        );
      }
      return false;
    }
  }

  Future<void> _expireIdentity(
    SessionDependencies dependencies,
    CredentialSnapshot identity,
    int generation,
    String message,
  ) async {
    final expiredGeneration = await withCredentialMutation<int?>(
      dependencies.credentialStore,
      () async {
        if (!_authActive(generation)) return null;
        final latest = await dependencies.credentialStore.read();
        if (!_authActive(generation) || !_sameIdentity(latest, identity)) {
          return null;
        }
        final expiredGeneration = ++_authGeneration;
        _client?.invalidate();
        _client = null;
        _publish(
          SessionSignedOut(message: message, serverUrl: identity.serverUrl),
        );
        try {
          await dependencies.credentialStore.clear();
          await _sessionStore(dependencies).clear();
        } on Object {
          if (_authActive(expiredGeneration)) {
            _publish(
              SessionSignedOut(
                message: '无法清除本机登录，请重试。',
                retrySignOut: true,
                serverUrl: identity.serverUrl,
              ),
            );
          }
          rethrow;
        }
        return expiredGeneration;
      },
    );
    if (expiredGeneration != null) {
      await _rememberLoginServer(identity.serverUrl, expiredGeneration);
    }
  }

  /// 凭据清理完成后再保存地址，偏好存储故障不能延长失效身份的生命周期。
  Future<void> _rememberLoginServer(String? serverUrl, int generation) async {
    if (serverUrl == null || !_authActive(generation)) return;
    try {
      await ref
          .read(appPreferencesProvider.notifier)
          .setRememberedServerUrl(serverUrl);
    } on Object {
      // 当前未登录状态仍携带原地址；后续登录保存成功时会补齐持久化。
    }
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
        final current = await withCredentialMutation(
          dependencies.credentialStore,
          () async {
            if (!_authActive(generation)) return null;
            final value = await dependencies.credentialStore.read();
            return _authActive(generation) && _sameIdentity(value, identity)
                ? value
                : null;
          },
        );
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
          return await withCredentialMutation(
            dependencies.credentialStore,
            () async {
              if (!_authActive(generation)) return null;
              final latest = await dependencies.credentialStore.read();
              if (!_authActive(generation) ||
                  !_sameIdentity(latest, identity)) {
                return null;
              }
              if (latest!.refreshToken != current.refreshToken) {
                return latest.accessToken;
              }
              final updated = CredentialSnapshot(
                serverUrl: current.serverUrl,
                deviceId: current.deviceId,
                accessToken: tokens.accessToken,
                refreshToken: tokens.refreshToken,
                userId: current.userId,
                devicePrivateKey: current.devicePrivateKey,
                authMethod: current.authMethod,
                userEmail: current.userEmail,
                userDisplayName: current.userDisplayName,
                providerSubject: current.providerSubject,
                googleSubject: current.googleSubject,
              );
              await dependencies.credentialStore.write(updated);
              if (!_authActive(generation)) return null;
              final value = _publishedState;
              if (value is SessionAuthenticated &&
                  _sameIdentity(value.snapshot, identity)) {
                _publish(
                  SessionAuthenticated(
                    snapshot: updated,
                    summary: value.summary,
                  ),
                );
              }
              return tokens.accessToken;
            },
          );
        } finally {
          if (identical(_pendingRefresh, pending)) _pendingRefresh = null;
        }
      },
      onUnauthorized: () =>
          _expireIdentity(dependencies, identity, generation, '登录已过期，请重新登录。'),
      onUserDisabled: () => _expireIdentity(
        dependencies,
        identity,
        generation,
        _userDisabledMessage,
      ),
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
