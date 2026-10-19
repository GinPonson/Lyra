import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import 'invite_code_screen.dart';
import '../overview/login_panel.dart';

/// 独立登录入口。未登录时不展示财务导航；完成授权后由 App 根切换工作台。
class LoginScreen extends ConsumerWidget {
  const LoginScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    final current = session.value;
    if (current is SessionRegistrationRequired) {
      return InviteCodeScreen(registration: current);
    }
    return Scaffold(
      key: const ValueKey('login-screen'),
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            // 留出顶部品牌空间与底部 24px 操作间距；键盘或大字挤压时由滚动承接。
            final available = (constraints.maxHeight - 56).clamp(
              0.0,
              double.infinity,
            );
            return SingleChildScrollView(
              key: const PageStorageKey('login-scroll'),
              padding: const EdgeInsets.fromLTRB(24, 32, 24, 24),
              child: ConstrainedBox(
                constraints: BoxConstraints(minHeight: available),
                child: IntrinsicHeight(
                  child: Align(
                    alignment: Alignment.topCenter,
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 400),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          SizedBox(height: available * 0.19),
                          const Row(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              Icon(
                                LyraIcons.cost,
                                size: 36,
                                color: LyraColors.accent,
                              ),
                              SizedBox(width: 10),
                              Text(
                                'lyra',
                                textScaler: TextScaler.noScaling,
                                style: TextStyle(
                                  fontSize: 43,
                                  fontWeight: FontWeight.w600,
                                  letterSpacing: -2.6,
                                  height: 1.2,
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 12),
                          const Text(
                            '你的投资，清晰可见。',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: LyraColors.muted,
                              fontSize: 14,
                            ),
                          ),
                          const Spacer(),
                          if (session.isLoading)
                            const Padding(
                              padding: EdgeInsets.symmetric(vertical: 24),
                              child: Center(
                                child: SizedBox(
                                  width: 20,
                                  height: 20,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                ),
                              ),
                            )
                          else
                            const LoginPanel(framed: false),
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
    );
  }
}
