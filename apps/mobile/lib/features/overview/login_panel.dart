import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';

/// 服务端地址与系统浏览器登录；OAuth client secret 只保存在服务端。
class LoginPanel extends ConsumerStatefulWidget {
  const LoginPanel({super.key});

  @override
  ConsumerState<LoginPanel> createState() => _LoginPanelState();
}

class _LoginPanelState extends ConsumerState<LoginPanel> {
  final _serverUrl = TextEditingController(
    text: const String.fromEnvironment('LYRA_SERVER_URL'),
  );
  SessionController? _activeController;
  bool _submitting = false;
  bool _signingOut = false;
  int _attempt = 0;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    final session = ref.read(sessionProvider).value;
    if (session is SessionAuthenticated) {
      _serverUrl.text = session.snapshot.serverUrl;
    }
  }

  @override
  void dispose() {
    _serverUrl.dispose();
    if (_submitting) _activeController?.cancelSignIn();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_submitting) return;
    final server = Uri.tryParse(_serverUrl.text.trim());
    final loopback =
        server != null &&
        ['localhost', '127.0.0.1', '::1'].contains(server.host);
    if (server == null ||
        server.host.isEmpty ||
        server.userInfo.isNotEmpty ||
        server.hasQuery ||
        server.hasFragment ||
        !(server.scheme == 'https' || (server.scheme == 'http' && loopback))) {
      setState(() => _errorMessage = '请输入 HTTPS 服务端地址（本机开发可使用 HTTP）。');
      return;
    }
    final serverUrl = server.toString().replaceFirst(RegExp(r'/+$'), '');
    final attempt = ++_attempt;
    setState(() {
      _submitting = true;
      _errorMessage = null;
    });
    final controller = _activeController = ref.read(sessionProvider.notifier);
    try {
      await controller.signInWithGoogle(
        serverUrl: serverUrl,
        displayName: 'Lyra Mobile',
      );
    } on LyraApiError catch (error) {
      if (!mounted || attempt != _attempt) return;
      setState(() => _errorMessage = '${error.message}（${error.code}）');
    } on Object {
      if (!mounted || attempt != _attempt) return;
      setState(() => _errorMessage = '登录未完成，请检查网络后重试。');
    } finally {
      if (mounted && attempt == _attempt) setState(() => _submitting = false);
    }
  }

  void _cancel() {
    _attempt++;
    _activeController?.cancelSignIn();
    setState(() {
      _submitting = false;
      _errorMessage = '已取消登录。';
    });
  }

  Future<void> _signOut() async {
    if (_signingOut) return;
    final attempt = ++_attempt;
    setState(() {
      _signingOut = true;
      _errorMessage = null;
    });
    try {
      await ref.read(sessionProvider.notifier).signOut();
    } on Object catch (error) {
      if (!mounted || attempt != _attempt) return;
      final localFailure =
          error is LyraApiError && error.code == 'LOCAL_LOGOUT_FAILED';
      final cleared =
          !localFailure && ref.read(sessionProvider).value is SessionSignedOut;
      setState(
        () => _errorMessage = cleared
            ? '已清除本机登录。未能连接服务端撤销会话，请在服务端检查设备。'
            : '无法清除本机登录，请重试。',
      );
    } finally {
      if (mounted) setState(() => _signingOut = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(sessionProvider).value;
    final authenticated = session is SessionAuthenticated ? session : null;
    final sessionMessage = session is SessionSignedOut ? session.message : null;
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        color: LyraColors.surfaceSubtle,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              const Icon(
                LyraIcons.security,
                size: 20,
                color: LyraColors.accent,
              ),
              const SizedBox(width: 10),
              Text(
                authenticated == null ? '登录 Lyra' : '已登录 Lyra',
                style: Theme.of(context).textTheme.titleLarge,
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (authenticated != null) ...<Widget>[
            Text(authenticated.snapshot.userEmail ?? 'Google 账号已登录'),
            const SizedBox(height: 6),
            Text(
              authenticated.snapshot.serverUrl,
              style: Theme.of(context).textTheme.bodySmall
                  ?.copyWith(color: LyraColors.muted),
            ),
            const SizedBox(height: 18),
            OutlinedButton(
              onPressed: _signingOut ? null : _signOut,
              child: Text(_signingOut ? '正在退出…' : '退出登录'),
            ),
          ] else ...<Widget>[
            Text(
              '使用 Google 账号登录你的服务端。授权完成后返回 Lyra。',
              style: Theme.of(context).textTheme.bodyMedium
                  ?.copyWith(color: LyraColors.muted),
            ),
            const SizedBox(height: 18),
            TextField(
              controller: _serverUrl,
              enabled: !_submitting,
              keyboardType: TextInputType.url,
              autocorrect: false,
              decoration: const InputDecoration(
                labelText: '服务端地址',
                hintText: 'https://lyra.example.com',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 18),
            FilledButton.icon(
              onPressed: _submitting ? null : _submit,
              icon: _submitting
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(LyraIcons.security),
              label: Text(_submitting ? '等待 Google 授权…' : '使用 Google 登录'),
            ),
            if (_submitting)
              TextButton(onPressed: _cancel, child: const Text('取消登录')),
          ],
          if (_errorMessage != null || sessionMessage != null) ...<Widget>[
            const SizedBox(height: 12),
            Text(
              _errorMessage ?? sessionMessage!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
        ],
      ),
    );
  }
}
