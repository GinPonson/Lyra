import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/preferences/app_preferences.dart';
import '../auth/auth_provider.dart';
import '../auth/auth_workspace.dart';
import '../system/system_repository.dart';
import '../system/system_workspace.dart';

/// 已登录时以凭据绑定的地址为准，退出后仍可使用本机保存的地址。
final currentServerUrlProvider = Provider<String?>((ref) {
  final session = ref.watch(sessionProvider).value;
  final preferences =
      ref.watch(appPreferencesProvider).value ??
      ref.read(appPreferencesProvider.notifier).lastSaved;
  final remembered = preferences?.rememberedServerUrl;
  final input = session is SessionAuthenticated
      ? session.snapshot.serverUrl
      : remembered ?? const String.fromEnvironment('LYRA_SERVER_URL');
  return input.isEmpty ? null : input;
});

String serverDisplayName(String serverUrl) {
  final uri = Uri.tryParse(serverUrl);
  return uri == null || uri.host.isEmpty ? serverUrl : uri.authority;
}

String connectionStatusLabel(ServerConnectionStatus status) => switch (status) {
  ServerConnectionStatus.healthy => '连接正常',
  ServerConnectionStatus.degraded => '部分服务暂不可用',
  ServerConnectionStatus.unavailable => '服务暂不可用',
  ServerConnectionStatus.incompatible => '需要更新版本',
};

Future<void> showConnectionManagementSheet(BuildContext context) =>
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      useSafeArea: true,
      builder: (context) => SingleChildScrollView(
        padding: EdgeInsets.fromLTRB(
          20,
          0,
          20,
          MediaQuery.viewInsetsOf(context).bottom + 24,
        ),
        child: const _ConnectionManagementPanel(),
      ),
    );

class _ConnectionManagementPanel extends ConsumerWidget {
  const _ConnectionManagementPanel();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final url = ref.watch(currentServerUrlProvider);
    final check = url == null ? null : ref.watch(systemInfoProvider(url));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                '服务端连接',
                style: Theme.of(context).textTheme.titleLarge,
              ),
            ),
            IconButton(
              tooltip: '关闭',
              onPressed: () => Navigator.pop(context),
              icon: const Icon(LyraIcons.dismiss),
            ),
          ],
        ),
        if (url != null) ...[
          const SizedBox(height: 8),
          SelectableText(serverDisplayName(url)),
          const SizedBox(height: 6),
          Text(
            check!.when(
              loading: () => '正在检测连接…',
              error: (error, _) => LyraApiError.from(error).message,
              data: (value) => value.isCompatible
                  ? connectionStatusLabel(value.status)
                  : '服务端与当前 App 版本不兼容，请更新后再试。',
            ),
            style: const TextStyle(color: LyraColors.muted, height: 1.6),
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: check.isLoading
                  ? null
                  : () => ref.invalidate(systemInfoProvider(url)),
              icon: const Icon(LyraIcons.sync, size: 18),
              label: const Text('重新检测连接'),
            ),
          ),
        ] else
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 12),
            child: Text('先保存你的 Lyra 服务端地址，再登录或检测连接。'),
          ),
        const SizedBox(height: 12),
        const LoginPanel(),
      ],
    );
  }
}

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

/// 服务端地址与系统浏览器登录；所有入口复用 SessionController。
class LoginPanel extends ConsumerStatefulWidget {
  const LoginPanel({super.key, this.framed = true});

  final bool framed;

  @override
  ConsumerState<LoginPanel> createState() => _LoginPanelState();
}

class _LoginPanelState extends ConsumerState<LoginPanel> {
  final _serverUrl = TextEditingController();
  Object? _currentOwner;
  late final _LoginPanelOwner _loginOwner;
  bool _submitting = false;
  bool _savingAddress = false;
  bool _clearingLogin = false;
  bool _editingServer = false;
  bool _serverEdited = false;
  int _attempt = 0;
  String? _message;
  String _progressLabel = '等待授权…';
  String? _catalogUrl;

  @override
  void initState() {
    super.initState();
    _loginOwner = ref.read(_loginPanelOwnerProvider);
    _serverUrl.text = ref.read(currentServerUrlProvider) ?? '';
    _catalogUrl = _serverUrl.text.isEmpty ? null : _serverUrl.text;
  }

  @override
  void dispose() {
    _attempt++;
    final owner = _currentOwner;
    if (_submitting && owner != null) _loginOwner.cancel(owner);
    _serverUrl.dispose();
    super.dispose();
  }

  String? _validatedUrl() {
    try {
      return normalizeLyraServerUrl(_serverUrl.text);
    } on LyraApiError catch (error) {
      setState(() => _message = error.message);
      return null;
    }
  }

  bool _active(int attempt, Object owner) =>
      mounted && attempt == _attempt && _loginOwner.owns(owner);

  Future<void> _saveAddress() async {
    if (_submitting || _savingAddress) return;
    final url = _validatedUrl();
    if (url == null) return;
    final attempt = ++_attempt;
    setState(() {
      _savingAddress = true;
      _message = null;
    });
    try {
      // 更换服务端时先检查新地址支持哪些登录方式；确认登录后才改本机记忆。
      if (ref.read(sessionProvider).value is! SessionAuthenticated) {
        await ref
            .read(appPreferencesProvider.notifier)
            .setRememberedServerUrl(url);
      }
      if (!mounted || attempt != _attempt) return;
      _serverUrl.text = url;
      setState(() {
        _catalogUrl = url;
        _serverEdited = false;
        _message = null;
      });
      ref.invalidate(authProvidersProvider(url));
    } on Object {
      if (!mounted || attempt != _attempt) return;
      setState(() => _message = '服务端地址未能保存，请重试。');
    } finally {
      if (mounted && attempt == _attempt) {
        setState(() => _savingAddress = false);
      }
    }
  }

  Future<void> _submit({
    required AuthProvider provider,
    SessionAuthenticated? replace,
  }) async {
    if (_submitting || _savingAddress) return;
    final url = _validatedUrl();
    if (url == null) return;
    if (replace == null) {
      final catalog = ref.read(authProvidersProvider(url)).asData?.value;
      if (_serverEdited ||
          catalog == null ||
          !catalog.any((item) => item.provider == provider && item.available)) {
        return;
      }
    }
    final controller = ref.read(sessionProvider.notifier);
    final originalClient = controller.client;
    if (replace != null) {
      if (url == normalizeLyraServerUrl(replace.snapshot.serverUrl)) {
        setState(() => _message = '与当前服务端相同，无需更换。');
        return;
      }
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          scrollable: true,
          title: const Text('更换服务端？'),
          content: Text(
            '将退出此设备在 ${serverDisplayName(replace.snapshot.serverUrl)} 的登录，'
            '然后前往 ${serverDisplayName(url)} 的登录页面。'
            '\n\n你的账本仍保存在原服务端。',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('更换并前往登录'),
            ),
          ],
        ),
      );
      if (!mounted || confirmed != true) return;
      if (!identical(controller.client, originalClient)) {
        _showIdentityChanged();
        return;
      }
    }
    final attempt = ++_attempt;
    final owner = _currentOwner = Object();
    _loginOwner.claim(owner, controller);
    setState(() {
      _submitting = true;
      _message = null;
      _progressLabel = replace == null
          ? '准备 ${provider.label} 登录…'
          : '正在更换服务端…';
    });
    try {
      // 地址保存成功后才开始更换；失败不会清除当前登录。
      await ref
          .read(appPreferencesProvider.notifier)
          .setRememberedServerUrl(url);
      if (!_active(attempt, owner)) return;
      if (!identical(controller.client, originalClient)) {
        _showIdentityChanged();
        return;
      }
      _serverUrl.text = url;
      if (replace != null) {
        try {
          await controller.signOut();
        } on Object catch (error) {
          if (!_active(attempt, owner)) return;
          if (error is LyraApiError && error.code == 'LOCAL_LOGOUT_FAILED') {
            setState(() => _message = '无法清除本机登录，请重试后再更换服务端。');
            return;
          }
          if (ref.read(sessionProvider).value is! SessionSignedOut) return;
          setState(() => _message = '已退出此设备登录。原服务端暂时无法撤销会话，稍后可检查原服务端。');
        }
        if (!_active(attempt, owner) ||
            ref.read(sessionProvider).value is! SessionSignedOut) {
          return;
        }
        setState(() {
          _catalogUrl = url;
          _serverEdited = false;
          _editingServer = false;
          _message = '已更换服务端，请选择登录方式。';
        });
        return;
      }
      if (!_active(attempt, owner)) return;
      setState(() {
        _editingServer = false;
        _progressLabel = '等待 ${provider.label} 授权…';
      });
      await controller.signIn(
        provider: provider,
        serverUrl: url,
        displayName: 'Lyra Mobile',
      );
    } on LyraApiError catch (error) {
      if (!_active(attempt, owner)) return;
      setState(() => _message = error.message);
    } on Object {
      if (!_active(attempt, owner)) return;
      setState(() => _message = '登录未完成，请检查网络及本机存储后重试。');
    } finally {
      if (mounted && attempt == _attempt) setState(() => _submitting = false);
      _loginOwner.release(owner);
    }
  }

  void _showIdentityChanged() {
    _serverUrl.text = ref.read(currentServerUrlProvider) ?? '';
    setState(() {
      _editingServer = false;
      _serverEdited = false;
      _catalogUrl = _serverUrl.text.isEmpty ? null : _serverUrl.text;
      _message = '登录状态已变化，已停止此次更换。';
    });
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
    ref.listen(currentServerUrlProvider, (_, next) {
      if (!_submitting && !_savingAddress && !_serverEdited && next != null) {
        setState(() {
          _serverUrl.text = next;
          _catalogUrl = next;
        });
      }
    });
    final busy =
        _submitting || _savingAddress || _clearingLogin || state.isLoading;
    final requiresLogoutRetry =
        session is SessionSignedOut && session.retrySignOut;
    final catalog = _catalogUrl == null || authenticated != null
        ? null
        : ref.watch(authProvidersProvider(_catalogUrl!));
    final entries = catalog?.asData?.value;
    final identityProvider = AuthProvider.fromId(
      authenticated?.snapshot.authMethod,
    );
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
        if (authenticated != null && !_editingServer && !_submitting) ...[
          Text(
            authenticated.snapshot.userEmail ??
                authenticated.snapshot.userDisplayName ??
                '${identityProvider?.label ?? '账号'}已登录',
          ),
          const SizedBox(height: 6),
          SelectableText(
            authenticated.snapshot.serverUrl,
            style: const TextStyle(color: LyraColors.muted),
          ),
          const SizedBox(height: 18),
          OutlinedButton.icon(
            onPressed: () {
              _serverUrl.text = authenticated.snapshot.serverUrl;
              setState(() {
                _editingServer = true;
                _serverEdited = true;
                _message = null;
              });
            },
            icon: const LyraPrimaryIcon(LyraPrimaryGlyph.server, size: 18),
            label: const Text('更换服务端'),
          ),
          TextButton(
            onPressed: () => showDeviceSignOutDialog(context),
            child: const Text('退出登录'),
          ),
        ] else ...[
          if (authenticated == null)
            for (final provider in AuthProvider.values) ...[
              _ProviderSignInButton(
                provider: provider,
                available:
                    !busy &&
                    !requiresLogoutRetry &&
                    !_serverEdited &&
                    (entries?.any(
                          (item) => item.provider == provider && item.available,
                        ) ??
                        false),
                status: _submitting
                    ? null
                    : _catalogUrl == null || _serverEdited
                    ? '待连接'
                    : catalog?.isLoading == true
                    ? '检测中'
                    : entries == null
                    ? '暂不可用'
                    : entries.any(
                        (item) => item.provider == provider && item.available,
                      )
                    ? null
                    : '未配置',
                onPressed: () =>
                    _submit(provider: provider, replace: authenticated),
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
          ] else if (catalog?.hasError == true && !_serverEdited) ...[
            Text(
              catalog!.error is LyraApiError &&
                      (catalog.error! as LyraApiError).statusCode == 404
                  ? '服务端尚不支持登录方式查询，请更新服务端。'
                  : '暂时无法读取登录方式，请检查服务端地址与连接。',
              style: const TextStyle(color: LyraColors.muted, height: 1.6),
            ),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                key: const ValueKey('login-reload-providers'),
                onPressed: busy
                    ? null
                    : () => ref.invalidate(authProvidersProvider(_catalogUrl!)),
                icon: const Icon(LyraIcons.sync, size: 17),
                label: const Text('重新检测'),
              ),
            ),
          ] else if (entries != null &&
              entries.every((item) => !item.available) &&
              !_serverEdited)
            const Text(
              '这个服务端尚未配置登录方式，请联系服务端管理员。',
              style: TextStyle(color: LyraColors.muted, height: 1.6),
            ),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: LyraColors.surfaceSubtle,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: LyraColors.hairline),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    const LyraPrimaryIcon(LyraPrimaryGlyph.server, size: 18),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        '服务端连接',
                        style: Theme.of(context).textTheme.titleSmall,
                      ),
                    ),
                    if (_catalogUrl != null &&
                        !_serverEdited &&
                        !_editingServer)
                      TextButton(
                        key: const ValueKey('login-edit-server'),
                        onPressed: busy
                            ? null
                            : () => setState(() => _serverEdited = true),
                        child: const Text('更换'),
                      ),
                  ],
                ),
                if (_catalogUrl == null || _serverEdited || _editingServer) ...[
                  const SizedBox(height: 12),
                  TextField(
                    key: const ValueKey('login-server-url'),
                    controller: _serverUrl,
                    enabled: !busy,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    textInputAction: TextInputAction.done,
                    onChanged: (_) => setState(() => _serverEdited = true),
                    onSubmitted: (_) => busy ? null : _saveAddress(),
                    decoration: const InputDecoration(
                      labelText: '服务端地址',
                      hintText: 'https://lyra.example.com',
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (authenticated == null)
                    TextButton.icon(
                      key: const ValueKey('login-save-server'),
                      onPressed: busy ? null : _saveAddress,
                      icon: const Icon(LyraIcons.connection, size: 17),
                      label: Text(_savingAddress ? '正在连接…' : '连接服务端'),
                    ),
                  if (_editingServer)
                    const Text(
                      '更换后会前往登录页面。原服务端的账本会保留。',
                      style: TextStyle(
                        color: LyraColors.muted,
                        fontSize: 13,
                        height: 1.6,
                      ),
                    ),
                ] else ...[
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          serverDisplayName(_catalogUrl!),
                          style: const TextStyle(
                            color: LyraColors.inkSecondary,
                            fontSize: 13,
                          ),
                        ),
                      ),
                      IconButton(
                        key: const ValueKey('login-refresh-providers'),
                        tooltip: '重新检测登录方式',
                        onPressed: busy || catalog?.isLoading == true
                            ? null
                            : () => ref.invalidate(
                                authProvidersProvider(_catalogUrl!),
                              ),
                        icon: const Icon(LyraIcons.sync, size: 18),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
          if (_editingServer && !_submitting) ...[
            const SizedBox(height: 14),
            FilledButton(
              key: const ValueKey('login-change-server'),
              onPressed: busy
                  ? null
                  : () => _submit(
                      provider: identityProvider ?? AuthProvider.google,
                      replace: authenticated,
                    ),
              child: const Text('更换并前往登录'),
            ),
          ],
          if (_editingServer && !_submitting)
            TextButton(
              onPressed: busy
                  ? null
                  : () => setState(() {
                      _editingServer = false;
                      _serverEdited = false;
                      _serverUrl.text = authenticated!.snapshot.serverUrl;
                      _catalogUrl = _serverUrl.text;
                      _message = null;
                    }),
              child: const Text('取消更换'),
            ),
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
        if (requiresLogoutRetry && !widget.framed)
          TextButton(
            key: const ValueKey('login-retry-sign-out'),
            onPressed: busy ? null : _retrySignOut,
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
          _message = '已退出此设备登录。服务端地址未能保存，下次可重新填写。';
        });
      }
    } on Object catch (error) {
      if (!mounted) return;
      final localFailure =
          error is LyraApiError && error.code == 'LOCAL_LOGOUT_FAILED';
      setState(() {
        _finished = !localFailure;
        _message = localFailure
            ? '无法清除本机登录，请重试。'
            : '已退出此设备登录。服务端暂时无法撤销会话，账本仍保留在服务端。';
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
          const Text('这会清除此设备的登录信息。你的账本仍保存在服务端，本机使用偏好会保留。'),
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
