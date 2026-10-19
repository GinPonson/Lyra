import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/design/lyra_widgets.dart';
import '../system/system_workspace.dart';
import 'profile_widgets.dart';

const lyraProjectUrl = 'https://github.com/GinPonson/Lyra';

/// 显示真实 App 版本与项目入口，不请求服务端诊断或连接状态。
class AboutScreen extends ConsumerWidget {
  const AboutScreen({super.key});

  /// 使用外部浏览器打开项目；[context] 用于明确反馈浏览器启动失败。
  Future<void> _openProject(BuildContext context) async {
    var opened = false;
    try {
      opened = await launchUrl(
        Uri.parse(lyraProjectUrl),
        mode: LaunchMode.externalApplication,
      );
    } on Object {
      opened = false;
    }
    if (!opened && context.mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('无法打开浏览器，可以长按下方地址复制。')));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final appVersion = ref.watch(appVersionProvider);
    return ProfileSubpage(
      title: '关于 Lyra',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const LyraCard(
            padding: EdgeInsets.all(24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'lyra',
                  style: TextStyle(
                    fontSize: 40,
                    fontWeight: FontWeight.w600,
                    letterSpacing: -1.6,
                    color: LyraColors.ink,
                  ),
                ),
                SizedBox(height: 8),
                Text(
                  '整理账户与交易，看清你的投资。',
                  style: TextStyle(
                    color: LyraColors.inkSecondary,
                    fontSize: 14,
                    height: 1.6,
                  ),
                ),
              ],
            ),
          ),
          ProfileSection(
            children: [
              ProfileSettingRow(
                title: 'App 版本',
                glyph: LyraPrimaryGlyph.about,
                value: appVersion.when(
                  data: (value) => value,
                  loading: () => '正在读取…',
                  error: (_, _) => '暂时无法读取版本',
                ),
                trailing: appVersion.hasError
                    ? IconButton(
                        tooltip: '重新读取 App 版本',
                        onPressed: () => ref.invalidate(appVersionProvider),
                        icon: const Icon(LyraIcons.sync),
                      )
                    : null,
              ),
              ProfileSettingRow(
                title: 'GitHub 项目',
                icon: LyraIcons.openLink,
                subtitle: 'GinPonson / Lyra',
                onTap: () => _openProject(context),
              ),
            ],
          ),
          const SizedBox(height: 16),
          const Padding(
            padding: EdgeInsets.symmetric(horizontal: 8),
            child: SelectableText(
              lyraProjectUrl,
              style: TextStyle(
                color: LyraColors.muted,
                fontSize: 12,
                height: 1.6,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
