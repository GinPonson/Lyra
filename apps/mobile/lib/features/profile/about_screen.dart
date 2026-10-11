import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../overview/login_panel.dart';
import '../system/system_workspace.dart';
import 'profile_widgets.dart';

const lyraProjectUrl = 'https://github.com/GinPonson/Lyra';

class AboutScreen extends ConsumerWidget {
  const AboutScreen({super.key});

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
    final url = ref.watch(currentServerUrlProvider);
    final server = url == null ? null : ref.watch(systemInfoProvider(url));
    return ProfileSubpage(
      title: '关于 Lyra',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const SizedBox(height: 12),
          const Text(
            'lyra',
            style: TextStyle(
              fontSize: 34,
              fontWeight: FontWeight.w500,
              letterSpacing: -1.4,
              color: LyraColors.ink,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            '整理账户与交易，看清你的投资。',
            style: TextStyle(color: LyraColors.muted, height: 1.7),
          ),
          ProfileSection(
            title: '版本信息',
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
                title: '服务端版本',
                glyph: LyraPrimaryGlyph.server,
                value:
                    server?.when(
                      data: (value) => value.serverVersion,
                      loading: () => '正在读取…',
                      error: (_, _) => '暂时无法读取版本',
                    ) ??
                    '尚未设置服务端',
                trailing: server?.hasError == true
                    ? IconButton(
                        tooltip: '重新读取服务端版本',
                        onPressed: () =>
                            ref.invalidate(systemInfoProvider(url!)),
                        icon: const Icon(LyraIcons.sync),
                      )
                    : null,
              ),
            ],
          ),
          ProfileSection(
            title: '项目',
            children: [
              ProfileSettingRow(
                title: 'GitHub 项目',
                icon: LyraIcons.openLink,
                subtitle: 'GinPonson / Lyra',
                onTap: () => _openProject(context),
              ),
            ],
          ),
          const SizedBox(height: 16),
          const SelectableText(
            lyraProjectUrl,
            style: TextStyle(
              color: LyraColors.muted,
              fontSize: 12,
              height: 1.6,
            ),
          ),
        ],
      ),
    );
  }
}
