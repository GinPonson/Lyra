import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_widgets.dart';
import 'login_failure_message.dart';

/// 展示已完成第三方授权的新用户注册步骤，并复用现有授权身份补全会话。
class InviteCodeScreen extends ConsumerStatefulWidget {
  const InviteCodeScreen({super.key, required this.registration});

  /// 当前等待邀请码的授权尝试；身份和服务端地址由会话控制器管理。
  final SessionRegistrationRequired registration;

  @override
  ConsumerState<InviteCodeScreen> createState() => _InviteCodeScreenState();
}

class _InviteCodeScreenState extends ConsumerState<InviteCodeScreen> {
  final _inviteCodeController = TextEditingController();
  bool _submitting = false;
  String? _message;

  @override
  void dispose() {
    _inviteCodeController.dispose();
    super.dispose();
  }

  /// 仅当页面所对应的授权尝试仍是当前待注册身份时返回 true。
  bool _hasRegistrationAttempt() {
    final session = ref.read(sessionProvider).value;
    return session is SessionRegistrationRequired &&
        session.provider == widget.registration.provider &&
        session.serverUrl == widget.registration.serverUrl &&
        session.expiresAt == widget.registration.expiresAt;
  }

  /// 使用已授权的待注册身份提交邀请码；失败时留在当前页以便修改后重试。
  Future<void> _completeRegistration() async {
    if (_submitting || !_hasRegistrationAttempt()) return;
    final inviteCode = _inviteCodeController.text.trim();
    if (inviteCode.isEmpty) {
      setState(() => _message = '请输入邀请码。');
      return;
    }

    setState(() {
      _submitting = true;
      _message = null;
    });
    try {
      final completed = await ref
          .read(sessionProvider.notifier)
          .completeRegistration(inviteCode: inviteCode);
      if (!mounted || completed) return;
      if (_hasRegistrationAttempt()) {
        setState(() => _message = '注册未完成，请检查邀请码后重试。');
      }
    } on LyraApiError catch (error) {
      if (!mounted || !_hasRegistrationAttempt()) return;
      setState(() => _message = loginFailureMessage(error));
    } on Object {
      if (!mounted || !_hasRegistrationAttempt()) return;
      setState(() => _message = '注册未完成，请检查网络后重试。');
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  /// 撤销仍在内存中的待注册尝试，并回到第三方登录入口。
  void _cancelRegistration() {
    if (!_hasRegistrationAttempt()) return;
    ref.read(sessionProvider.notifier).cancelRegistration();
  }

  @override
  Widget build(BuildContext context) => PopScope(
    // 注册表单位于根路由；系统返回键用于撤销pending，而不是让pending留在根页面后直接退出。
    canPop: false,
    onPopInvokedWithResult: (didPop, _) {
      if (!didPop) _cancelRegistration();
    },
    child: Scaffold(
      key: const ValueKey('invite-code-screen'),
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            return SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(24, 24, 24, 24),
              child: ConstrainedBox(
                constraints: BoxConstraints(
                  minHeight: (constraints.maxHeight - 48).clamp(
                    0.0,
                    double.infinity,
                  ),
                ),
                child: Align(
                  alignment: Alignment.center,
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 400),
                    child: LyraCard(
                      padding: const EdgeInsets.all(20),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Text(
                            '完成注册',
                            style: Theme.of(context).textTheme.headlineSmall,
                          ),
                          const SizedBox(height: 20),
                          TextField(
                            key: const ValueKey('registration-invite-code'),
                            controller: _inviteCodeController,
                            enabled: !_submitting,
                            keyboardType: TextInputType.visiblePassword,
                            textCapitalization: TextCapitalization.none,
                            autocorrect: false,
                            enableSuggestions: false,
                            textInputAction: TextInputAction.done,
                            onSubmitted: (_) => _completeRegistration(),
                            decoration: const InputDecoration(
                              labelText: '邀请码',
                              hintText: '输入邀请码',
                            ),
                          ),
                          if (_message != null) ...[
                            const SizedBox(height: 12),
                            Semantics(
                              liveRegion: true,
                              child: Text(
                                _message!,
                                style: const TextStyle(
                                  color: LyraColors.inkSecondary,
                                  height: 1.5,
                                ),
                              ),
                            ),
                          ],
                          const SizedBox(height: 16),
                          SizedBox(
                            height: 54,
                            child: FilledButton(
                              key: const ValueKey('complete-registration'),
                              onPressed: _submitting
                                  ? null
                                  : _completeRegistration,
                              child: _submitting
                                  ? const SizedBox.square(
                                      dimension: 18,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                      ),
                                    )
                                  : const Text('完成注册'),
                            ),
                          ),
                          TextButton(
                            key: const ValueKey('cancel-registration'),
                            onPressed: _cancelRegistration,
                            child: const Text('返回登录'),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            );
          },
        ),
      ),
    ),
  );
}
