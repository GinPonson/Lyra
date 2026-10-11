import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/preferences/app_preferences.dart';
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
  const LoginPanel({super.key});

  @override
  ConsumerState<LoginPanel> createState() => _LoginPanelState();
}

class _LoginPanelState extends ConsumerState<LoginPanel> {
  final _serverUrl = TextEditingController();
  Object? _currentOwner;
  late final _LoginPanelOwner _loginOwner;
  bool _submitting = false;
  bool _savingAddress = false;
  bool _editingServer = false;
  bool _serverEdited = false;
  int _attempt = 0;
  String? _message;
  String _progressLabel = '等待 Google 授权…';

  @override
  void initState() {
    super.initState();
    _loginOwner = ref.read(_loginPanelOwnerProvider);
    _serverUrl.text = ref.read(currentServerUrlProvider) ?? '';
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
      await ref
          .read(appPreferencesProvider.notifier)
          .setRememberedServerUrl(url);
      if (!mounted || attempt != _attempt) return;
      _serverUrl.text = url;
      setState(() => _message = '服务端地址已保存。');
    } on Object {
      if (!mounted || attempt != _attempt) return;
      setState(() => _message = '服务端地址未能保存，请重试。');
    } finally {
      if (mounted && attempt == _attempt) {
        setState(() => _savingAddress = false);
      }
    }
  }

  Future<void> _submit({SessionAuthenticated? replace}) async {
    if (_submitting || _savingAddress) return;
    final url = _validatedUrl();
    if (url == null) return;
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
            '然后在 ${serverDisplayName(url)} 重新使用 Google 登录。'
            '\n\n你的账本仍保存在原服务端。',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('更换并登录'),
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
      _progressLabel = replace == null ? '准备 Google 登录…' : '正在更换服务端…';
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
      }
      if (!_active(attempt, owner)) return;
      setState(() {
        _editingServer = false;
        _progressLabel = '等待 Google 授权…';
      });
      await controller.signInWithGoogle(
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

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(sessionProvider);
    final session = state.value;
    final authenticated = session is SessionAuthenticated ? session : null;
    final sessionMessage = session is SessionSignedOut ? session.message : null;
    ref.listen(currentServerUrlProvider, (_, next) {
      if (!_submitting && !_savingAddress && !_serverEdited && next != null) {
        _serverUrl.text = next;
      }
    });
    final busy = _submitting || _savingAddress || state.isLoading;
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: LyraColors.surfaceSubtle,
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Icon(
                LyraIcons.security,
                size: 20,
                color: LyraColors.accent,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  authenticated == null ? '登录 Lyra' : '已登录 Lyra',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (authenticated != null) ...[
            Text(authenticated.snapshot.userEmail ?? 'Google 账号已登录'),
            const SizedBox(height: 6),
            SelectableText(
              authenticated.snapshot.serverUrl,
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: LyraColors.muted),
            ),
            if (!_editingServer && !_submitting) ...[
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: () {
                  _serverUrl.text = authenticated.snapshot.serverUrl;
                  setState(() {
                    _editingServer = true;
                    _serverEdited = true;
                    _message = null;
                  });
                },
                icon: const Icon(LyraIcons.connection, size: 18),
                label: const Text('更换服务端'),
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () => showDeviceSignOutDialog(context),
                child: const Text('退出登录'),
              ),
            ],
          ] else
            const Text(
              '使用 Google 账号登录你的服务端。授权完成后返回 Lyra。',
              style: TextStyle(color: LyraColors.muted, height: 1.6),
            ),
          if (authenticated == null || _editingServer || _submitting) ...[
            const SizedBox(height: 18),
            TextField(
              key: const ValueKey('login-server-url'),
              controller: _serverUrl,
              enabled: !busy,
              keyboardType: TextInputType.url,
              autocorrect: false,
              onChanged: (_) => _serverEdited = true,
              decoration: const InputDecoration(
                labelText: '服务端地址',
                hintText: 'https://lyra.example.com',
              ),
            ),
            if (_editingServer) ...[
              const SizedBox(height: 12),
              const Text(
                '更换后需要重新登录，原服务端的账本会保留。',
                style: TextStyle(height: 1.6),
              ),
            ],
            const SizedBox(height: 16),
            FilledButton.icon(
              onPressed: busy ? null : () => _submit(replace: authenticated),
              icon: _submitting
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(LyraIcons.security, size: 20),
              label: Text(
                _submitting
                    ? _progressLabel
                    : authenticated != null
                    ? '更换并重新登录'
                    : '使用 Google 登录',
              ),
            ),
            if (_submitting)
              TextButton(onPressed: _cancel, child: const Text('取消登录'))
            else if (_editingServer)
              TextButton(
                onPressed: busy
                    ? null
                    : () => setState(() {
                        _editingServer = false;
                        _message = null;
                      }),
                child: const Text('取消更换'),
              )
            else
              TextButton(
                onPressed: busy ? null : _saveAddress,
                child: Text(_savingAddress ? '正在保存…' : '保存地址'),
              ),
          ],
          if (_message != null || sessionMessage != null) ...[
            const SizedBox(height: 12),
            Text(
              _message ?? sessionMessage!,
              style: const TextStyle(
                color: LyraColors.inkSecondary,
                height: 1.6,
              ),
            ),
          ],
        ],
      ),
    );
  }
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
