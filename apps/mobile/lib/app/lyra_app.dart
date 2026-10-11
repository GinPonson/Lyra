import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/design/lyra_theme.dart';
import '../features/auth/login_screen.dart';
import 'lyra_shell.dart';
import 'session.dart';

/// 应用根节点：全局主题、Riverpod 作用域与一级导航外壳。
class LyraApp extends ConsumerWidget {
  const LyraApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider).value;
    final identity = session is SessionAuthenticated ? session.snapshot : null;
    return MaterialApp(
      // 更换身份时连同二级页一起清理；普通令牌轮换沿用同一个导航栈。
      key: ValueKey(
        identity == null
            ? 'signed-out'
            : (identity.serverUrl, identity.deviceId),
      ),
      title: 'Lyra',
      debugShowCheckedModeBanner: false,
      theme: LyraTheme.dark,
      builder: (context, child) => AnnotatedRegion<SystemUiOverlayStyle>(
        value: LyraTheme.systemUiOverlayStyle,
        child: child ?? const SizedBox.shrink(),
      ),
      home: identity == null ? const LoginScreen() : const LyraShell(),
    );
  }
}
