import 'package:flutter/material.dart';

import '../core/design/lyra_theme.dart';
import 'lyra_shell.dart';

/// 应用根节点：全局主题、Riverpod 作用域与一级导航外壳。
class LyraApp extends StatelessWidget {
  const LyraApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Lyra',
      debugShowCheckedModeBanner: false,
      theme: LyraTheme.light,
      home: const LyraShell(),
    );
  }
}
