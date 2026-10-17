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
          builder: (context, constraints) {
            // 按视口高度分配上下留白：字标落在中上区域，登录入口压向底部。
            // 窄屏或放大字体下内容仍可滚动，不会溢出。
            final available = constraints.maxHeight > 80
                ? constraints.maxHeight - 80
                : 0.0;
            return SingleChildScrollView(
              key: const PageStorageKey('login-scroll'),
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 40),
              child: ConstrainedBox(
                constraints: BoxConstraints(minHeight: available),
                child: Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 400),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        SizedBox(height: available * 0.22),
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
                        // 字标已是本屏标题，不重复标题与说明。三段留白共同决定纵向
                        // 配比：加大上下留白、压缩中间空隙，让字标与登录入口都向
                        // 屏幕中部靠拢；三段之和保持不变，整体不会上下漂移。
                        SizedBox(height: available * 0.28),
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
                        SizedBox(height: available * 0.18),
                      ],
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
