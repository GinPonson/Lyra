import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/preferences/app_preferences.dart';
import '../auth/auth_provider.dart';
import '../auth/auth_workspace.dart';
import '../system/system_repository.dart';

/// 保留已有凭据与历史地址；新安装使用发布时配置的地址。
/// 先完成偏好恢复，避免启动时短暂请求另一个后台。
final currentServerUrlProvider = Provider<String?>((ref) {
  final session = ref.watch(sessionProvider).value;
  final String input;
  if (session is SessionAuthenticated) {
    input = session.snapshot.serverUrl;
  } else if (session is SessionSignedOut && session.serverUrl != null) {
    input = session.serverUrl!;
  } else {
    final preferences =
        ref.watch(appPreferencesProvider).value ??
        ref.read(appPreferencesProvider.notifier).lastSaved;
    if (preferences == null) return null;
    input =
        preferences.rememberedServerUrl ??
        ref.watch(configuredServerUrlProvider);
  }
  if (input.trim().isEmpty) return null;
  try {
    return normalizeLyraServerUrl(input);
  } on LyraApiError {
    return null;
  }
});

/// 登录页只展示可采取行动的提示，不直接透传后台错误或配置细节。
String _loginFailureMessage(LyraApiError error) => switch (error.code) {
  'LOGIN_BROWSER_UNAVAILABLE' => '无法打开浏览器，请重试。',
  'LOGIN_EXPIRED' => '登录已超时，请重新开始。',
  'LOGIN_STATE_INVALID' || 'LOGIN_ALREADY_REDEEMED' => '此次登录已失效，请重新开始。',
  'AUTH_FORBIDDEN' => '此账号暂时无法登录 Lyra，请换一个账号。',
  'LOGIN_DENIED' => '未能完成授权，请重试或换一个账号。',
  'AUTH_NOT_CONFIGURED' => '此登录方式暂不可用，请选择其他方式。',
  'LOGIN_RATE_LIMITED' => '尝试次数较多，请稍后重试。',
  'CLIENT_VERSION_INCOMPATIBLE' => '请更新 Lyra 后重试。',
  _ when error.statusCode == 429 => '尝试次数较多，请稍后重试。',
  _ when error.statusCode == 0 => '暂时无法登录，请检查网络后重试。',
  _ => '登录未完成，请重试。',
};

/// 多个快捷入口共用登录操作归属，旧表单销毁不会取消新表单发起的登录。
class _LoginPanelOwner {
  Object? owner;
  SessionController? controller;

  void claim(Object nextOwner, SessionController nextController) {
    if (owner != null && !identical(owner, nextOwner)) {
      controller?.cancelSignIn();
    }
    owner = nextOwner;
    controller = nextController;
  }

  bool owns(Object candidate) => identical(owner, candidate);

  void cancel(Object candidate) {
    if (!owns(candidate)) return;
    controller?.cancelSignIn();
    owner = null;
    controller = null;
  }

  void release(Object candidate) {
    if (!owns(candidate)) return;
    owner = null;
    controller = null;
  }
}

final _loginPanelOwnerProvider = Provider((ref) => _LoginPanelOwner());

/// 三种系统浏览器登录方式；连接配置由 App 自动处理。
class LoginPanel extends ConsumerStatefulWidget {
  const LoginPanel({super.key, this.framed = true});

  final bool framed;

  @override
  ConsumerState<LoginPanel> createState() => _LoginPanelState();
}

class _LoginPanelState extends ConsumerState<LoginPanel> {
  Object? _currentOwner;
  late final _LoginPanelOwner _loginOwner;
  bool _submitting = false;
  bool _clearingLogin = false;
  int _attempt = 0;
  String? _message;
  String _progressLabel = '等待授权…';

  @override
  void initState() {
    super.initState();
    _loginOwner = ref.read(_loginPanelOwnerProvider);
  }

  @override
  void dispose() {
    _attempt++;
    final owner = _currentOwner;
    if (_submitting && owner != null) _loginOwner.cancel(owner);
    super.dispose();
  }

  bool _active(int attempt, Object owner) =>
      mounted && attempt == _attempt && _loginOwner.owns(owner);

  Future<void> _submit(AuthProvider provider) async {
    final session = ref.read(sessionProvider);
    if (_submitting ||
        _clearingLogin ||
        session.isLoading ||
        session.value is SessionAuthenticated ||
        (session.value is SessionSignedOut &&
            (session.value! as SessionSignedOut).retrySignOut)) {
      return;
    }
    final url = ref.read(currentServerUrlProvider);
    if (url == null) return;
    final catalog = ref.read(authProvidersProvider(url)).asData?.value;
    if (catalog == null ||
        !catalog.any((item) => item.provider == provider && item.available)) {
      return;
    }
    final controller = ref.read(sessionProvider.notifier);
    final originalClient = controller.client;
    final attempt = ++_attempt;
    final owner = _currentOwner = Object();
    _loginOwner.claim(owner, controller);
    setState(() {
      _submitting = true;
      _message = null;
      _progressLabel = '准备 ${provider.label} 登录…';
    });
    try {
      await ref
          .read(appPreferencesProvider.notifier)
          .setRememberedServerUrl(url);
      if (!_active(attempt, owner)) return;
      if (!identical(controller.client, originalClient) ||
          ref.read(currentServerUrlProvider) != url) {
        setState(() => _message = '登录状态已变化，请重试。');
        return;
      }
      setState(() => _progressLabel = '等待 ${provider.label} 授权…');
      await controller.signIn(
        provider: provider,
        serverUrl: url,
        displayName: 'Lyra Mobile',
      );
    } on LyraApiError catch (error) {
      if (!_active(attempt, owner)) return;
      setState(() => _message = _loginFailureMessage(error));
    } on Object {
      if (!_active(attempt, owner)) return;
      setState(() => _message = '登录未完成，请重试。');
    } finally {
      if (mounted && attempt == _attempt) setState(() => _submitting = false);
      _loginOwner.release(owner);
    }
  }

  void _reloadLoginOptions() {
    setState(() => _message = null);
    ref.invalidate(appPreferencesProvider);
    final url = ref.read(currentServerUrlProvider);
    if (url != null) ref.invalidate(authProvidersProvider(url));
  }

  void _cancel() {
    _attempt++;
    final owner = _currentOwner;
    if (owner != null) _loginOwner.cancel(owner);
    setState(() {
      _submitting = false;
      _message = '已取消登录。';
    });
  }

  Future<void> _retrySignOut() async {
    if (_clearingLogin) return;
    final session = ref.read(sessionProvider).value;
    if (session is! SessionSignedOut || !session.retrySignOut) return;
    setState(() {
      _clearingLogin = true;
      _message = null;
    });
    try {
      await ref.read(sessionProvider.notifier).signOut();
    } on Object {
      // 控制器把当前身份对应的安全提示发布给登录页。
    } finally {
      if (mounted) setState(() => _clearingLogin = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(sessionProvider);
    final session = state.value;
    final authenticated = session is SessionAuthenticated ? session : null;
    final sessionMessage = session is SessionSignedOut ? session.message : null;
    final preferences = ref.watch(appPreferencesProvider);
    final url = ref.watch(currentServerUrlProvider);
    final catalog = url == null || authenticated != null
        ? null
        : ref.watch(authProvidersProvider(url));
    final entries = catalog?.asData?.value;
    final preparing =
        state.isLoading ||
        (url == null && preferences.isLoading) ||
        catalog?.isLoading == true;
    final busy = _submitting || _clearingLogin || preparing;
    final requiresLogoutRetry =
        session is SessionSignedOut && session.retrySignOut;
    final identityProvider = AuthProvider.fromId(
      authenticated?.snapshot.authMethod,
    );
    final allUnavailable =
        entries == null || entries.every((item) => !item.available);
    final needsRetry =
        !preparing &&
        (allUnavailable || entries.any((item) => !item.available));
    final content = Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (widget.framed) ...[
          Text(
            authenticated == null ? '登录 Lyra' : '已登录 Lyra',
            style: Theme.of(context).textTheme.titleLarge
                ?.copyWith(fontWeight: FontWeight.w500),
          ),
          const SizedBox(height: 12),
        ],
        if (authenticated != null) ...[
          Text(
            authenticated.snapshot.userEmail ??
                authenticated.snapshot.userDisplayName ??
                '${identityProvider?.label ?? '账号'}已登录',
          ),
          TextButton(
            onPressed: () => showDeviceSignOutDialog(context),
            child: const Text('退出登录'),
          ),
        ] else ...[
          for (final provider in AuthProvider.values) ...[
            _ProviderSignInButton(
              provider: provider,
              available:
                  !busy &&
                  !requiresLogoutRetry &&
                  (entries?.any(
                        (item) => item.provider == provider && item.available,
                      ) ??
                      false),
              status: _submitting || preparing
                  ? null
                  : entries?.any(
                          (item) => item.provider == provider && item.available,
                        ) ==
                        true
                  ? null
                  : '暂不可用',
              onPressed: () => _submit(provider),
            ),
            const SizedBox(height: 12),
          ],
          if (_submitting) ...[
            Semantics(
              liveRegion: true,
              child: Text(
                _progressLabel,
                textAlign: TextAlign.center,
                style: const TextStyle(color: LyraColors.accent, height: 1.6),
              ),
            ),
            const SizedBox(height: 6),
            const Text(
              '请在浏览器完成授权，然后返回 Lyra。',
              textAlign: TextAlign.center,
              style: TextStyle(color: LyraColors.muted, fontSize: 13),
            ),
            TextButton(onPressed: _cancel, child: const Text('取消登录')),
          ] else if (preparing)
            const Text(
              '正在准备登录…',
              textAlign: TextAlign.center,
              style: TextStyle(color: LyraColors.muted, height: 1.6),
            )
          else if (needsRetry) ...[
            if (allUnavailable)
              const Text(
                '暂时无法登录，请稍后重试。',
                textAlign: TextAlign.center,
                style: TextStyle(color: LyraColors.muted, height: 1.6),
              ),
            TextButton.icon(
              key: const ValueKey('login-reload-providers'),
              onPressed: busy ? null : _reloadLoginOptions,
              icon: const Icon(LyraIcons.sync, size: 17),
              label: const Text('重试'),
            ),
          ],
        ],
        if (_message != null || sessionMessage != null) ...[
          const SizedBox(height: 16),
          Semantics(
            liveRegion: true,
            child: Text(
              _message ?? sessionMessage!,
              style: const TextStyle(
                color: LyraColors.inkSecondary,
                height: 1.6,
              ),
            ),
          ),
        ],
        if (requiresLogoutRetry)
          TextButton(
            key: const ValueKey('login-retry-sign-out'),
            onPressed: _clearingLogin || _submitting || state.isLoading
                ? null
                : _retrySignOut,
            child: Text(_clearingLogin ? '正在清理…' : '重试清除登录'),
          ),
      ],
    );
    if (!widget.framed) return content;
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: LyraColors.surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: LyraColors.hairline),
      ),
      child: content,
    );
  }
}

class _ProviderSignInButton extends StatelessWidget {
  const _ProviderSignInButton({
    required this.provider,
    required this.available,
    required this.status,
    required this.onPressed,
  });
  final AuthProvider provider;
  final bool available;
  final String? status;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => OutlinedButton(
    key: ValueKey('login-${provider.id}'),
    onPressed: available ? onPressed : null,
    style: OutlinedButton.styleFrom(
      minimumSize: const Size.fromHeight(54),
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 15),
      backgroundColor: available
          ? LyraColors.surfaceStrong
          : LyraColors.surface,
      foregroundColor: LyraColors.ink,
      side: BorderSide(
        color: available ? LyraColors.hairlineStrong : LyraColors.hairline,
      ),
    ),
    child: Row(
      children: [
        ExcludeSemantics(
          child: Image.asset(
            switch (provider) {
              AuthProvider.google => 'assets/auth/google-g.png',
              AuthProvider.linuxdo => 'assets/auth/linuxdo.png',
              AuthProvider.github => 'assets/auth/github-white.png',
            },
            width: 20,
            height: 20,
            fit: BoxFit.contain,
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            '使用 ${provider.label} 登录',
            textAlign: status == null ? TextAlign.center : TextAlign.left,
          ),
        ),
        if (status != null) ...[
          const SizedBox(width: 10),
          Text(
            status!,
            style: const TextStyle(
              fontSize: 11,
              fontWeight: FontWeight.w400,
              color: LyraColors.mutedSoft,
            ),
          ),
        ],
      ],
    ),
  );
}

Future<void> showDeviceSignOutDialog(BuildContext context) {
  final container = ProviderScope.containerOf(context, listen: false);
  final session = container.read(sessionProvider).value;
  final client = container.read(sessionProvider.notifier).client;
  if (session is! SessionAuthenticated || client == null) return Future.value();
  return showDialog<void>(
    context: context,
    barrierDismissible: false,
    builder: (_) => _DeviceSignOutDialog(
      client: client,
      serverUrl: session.snapshot.serverUrl,
    ),
  );
}

class _DeviceSignOutDialog extends ConsumerStatefulWidget {
  const _DeviceSignOutDialog({required this.client, required this.serverUrl});

  final LyraApiClient client;
  final String serverUrl;

  @override
  ConsumerState<_DeviceSignOutDialog> createState() =>
      _DeviceSignOutDialogState();
}

class _DeviceSignOutDialogState extends ConsumerState<_DeviceSignOutDialog> {
  bool _busy = false;
  bool _started = false;
  bool _finished = false;
  String? _message;

  Future<void> _signOut() async {
    if (_busy) return;
    final controller = ref.read(sessionProvider.notifier);
    if ((!_started && !identical(controller.client, widget.client)) ||
        (_started && controller.client != null)) {
      setState(() {
        _finished = true;
        _message = '登录状态已变化，请关闭后重试。';
      });
      return;
    }
    setState(() {
      _busy = true;
      _message = null;
    });
    var addressSaved = true;
    try {
      try {
        await ref
            .read(appPreferencesProvider.notifier)
            .setRememberedServerUrl(widget.serverUrl);
      } on Object {
        addressSaved = false;
      }
      if (!mounted) return;
      if ((!_started && !identical(controller.client, widget.client)) ||
          (_started && controller.client != null)) {
        setState(() {
          _finished = true;
          _message = '登录状态已变化，请关闭后重试。';
        });
        return;
      }
      _started = true;
      await controller.signOut();
      if (!mounted) return;
      if (ref.read(sessionProvider).value is! SessionSignedOut) {
        setState(() {
          _finished = true;
          _message = '原设备登录已退出。';
        });
      } else if (addressSaved) {
        Navigator.pop(context);
      } else {
        setState(() {
          _finished = true;
          _message = '已退出此设备登录。部分本机设置未能保存。';
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      final localFailure =
          error is LyraApiError && error.code == 'LOCAL_LOGOUT_FAILED';
      setState(() {
        _finished = !localFailure;
        _message = localFailure ? '无法清除本机登录，请重试。' : '已退出此设备登录，但暂时无法完成全部退出操作。';
      });
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: !_busy,
    child: AlertDialog(
      scrollable: true,
      title: Text(_finished ? '退出登录' : '退出此设备登录？'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('退出后需要重新登录。你的账本和使用偏好会保留。'),
          if (_message != null) ...[
            const SizedBox(height: 16),
            Text(_message!, style: const TextStyle(height: 1.6)),
          ],
          if (_busy) ...[
            const SizedBox(height: 20),
            const LinearProgressIndicator(minHeight: 2),
          ],
        ],
      ),
      actions: _finished
          ? [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('完成'),
              ),
            ]
          : [
              TextButton(
                onPressed: _busy ? null : () => Navigator.pop(context),
                child: const Text('取消'),
              ),
              FilledButton(
                onPressed: _busy ? null : _signOut,
                child: Text(_busy ? '正在退出…' : '退出登录'),
              ),
            ],
    ),
  );
}
