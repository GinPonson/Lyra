import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../overview/login_panel.dart';

/// 独立登录入口。未登录时不展示财务导航；完成授权后由 App 根切换工作台。
class LoginScreen extends ConsumerWidget {
  const LoginScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    return Scaffold(
      key: const ValueKey('login-screen'),
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) => SingleChildScrollView(
            key: const PageStorageKey('login-scroll'),
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 40),
            child: ConstrainedBox(
              constraints: BoxConstraints(
                minHeight: constraints.maxHeight > 80
                    ? constraints.maxHeight - 80
                    : 0,
              ),
              child: Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 400),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
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
                        style: TextStyle(color: LyraColors.muted, fontSize: 14),
                      ),
                      const SizedBox(height: 44),
                      Text(
                        '登录 Lyra',
                        textAlign: TextAlign.center,
                        style: Theme.of(context).textTheme.headlineLarge
                            ?.copyWith(fontSize: 27, letterSpacing: -.5),
                      ),
                      const SizedBox(height: 10),
                      const Text(
                        '选择一个账号，继续你的投资工作台。',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: LyraColors.muted,
                          fontSize: 13,
                          height: 1.7,
                        ),
                      ),
                      const SizedBox(height: 28),
                      if (session.isLoading)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 40),
                          child: Text(
                            '正在恢复登录…',
                            textAlign: TextAlign.center,
                            style: TextStyle(color: LyraColors.muted),
                          ),
                        )
                      else
                        const LoginPanel(framed: false),
                      const SizedBox(height: 30),
                      const Text(
                        '在浏览器完成授权后，返回 Lyra 即可。',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: LyraColors.mutedSoft,
                          fontSize: 12,
                          height: 1.7,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
