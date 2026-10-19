import 'package:flutter/material.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/design/lyra_widgets.dart';

/// 用统一卡片承载连续的设置行。
///
/// [title] 是可空的 `String`，可省略以适配紧凑总览页，二级页面仍可显示分组
/// 标题；[children] 是 `List<Widget>`，按传入顺序显示。
class ProfileSection extends StatelessWidget {
  const ProfileSection({super.key, this.title, required this.children});

  final String? title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 16),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (title != null) ...[
          Padding(
            padding: const EdgeInsets.only(left: 2, bottom: 8),
            child: Text(
              title!,
              style: Theme.of(context).textTheme.labelMedium?.copyWith(
                color: LyraColors.muted,
                fontWeight: FontWeight.w500,
                letterSpacing: 0,
              ),
            ),
          ),
        ],
        LyraCard(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final (index, child) in children.indexed) ...[
                if (index > 0) const Divider(height: 1),
                child,
              ],
            ],
          ),
        ),
      ],
    ),
  );
}

/// 显示设置名称、当前值和可选的跳转操作。
///
/// [title] 是必填 `String` 设置名称；[glyph] 是统一 Hugeicons 名称，[icon] 是
/// 现有二级页 `IconData`，二者至少提供一个；[subtitle] 与 [value] 是可空
/// `String`，分别提供补充说明和当前状态；[onTap] 是可空 `VoidCallback`；
/// [trailing] 是可空 `Widget`，可替换默认箭头；[enabled] 是布尔值，控制交互和文字颜色。
class ProfileSettingRow extends StatelessWidget {
  const ProfileSettingRow({
    super.key,
    required this.title,
    this.icon,
    this.glyph,
    this.subtitle,
    this.value,
    this.onTap,
    this.trailing,
    this.enabled = true,
  }) : assert(icon != null || glyph != null);

  final String title;
  final IconData? icon;
  final LyraPrimaryGlyph? glyph;
  final String? subtitle;
  final String? value;
  final VoidCallback? onTap;
  final Widget? trailing;
  final bool enabled;

  @override
  Widget build(BuildContext context) => Material(
    color: Colors.transparent,
    child: InkWell(
      onTap: enabled ? onTap : null,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 18),
        child: Row(
          children: [
            ExcludeSemantics(
              child: glyph == null
                  ? Icon(
                      icon,
                      size: 21,
                      color: enabled
                          ? LyraColors.inkSecondary
                          : LyraColors.mutedSoft,
                    )
                  : LyraPrimaryIcon(
                      glyph!,
                      size: 21,
                      color: enabled
                          ? LyraColors.inkSecondary
                          : LyraColors.mutedSoft,
                    ),
            ),
            const SizedBox(width: 20),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  LayoutBuilder(
                    builder: (context, constraints) {
                      final name = Text(
                        title,
                        style: Theme.of(context).textTheme.titleSmall?.copyWith(
                          color: enabled ? LyraColors.ink : LyraColors.muted,
                          fontWeight: FontWeight.w500,
                        ),
                      );
                      final stacked =
                          constraints.maxWidth < 240 &&
                          MediaQuery.textScalerOf(context).scale(14) > 18;
                      final currentValue = value == null
                          ? null
                          : Text(
                              value!,
                              textAlign: stacked
                                  ? TextAlign.left
                                  : TextAlign.right,
                              style: Theme.of(context).textTheme.bodySmall
                                  ?.copyWith(
                                    color: LyraColors.inkSecondary,
                                    height: 1.5,
                                  ),
                            );
                      // 大字时让设置名称完整占行，避免“行情与估值”出现单字孤行。
                      if (stacked && currentValue != null) {
                        return Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            name,
                            const SizedBox(height: 4),
                            currentValue,
                          ],
                        );
                      }
                      return Row(
                        children: [
                          Expanded(child: name),
                          if (currentValue != null) ...[
                            const SizedBox(width: 8),
                            Expanded(child: currentValue),
                          ],
                        ],
                      );
                    },
                  ),
                  if (subtitle != null) ...[
                    const SizedBox(height: 4),
                    Text(
                      subtitle!,
                      style: Theme.of(context).textTheme.bodySmall
                          ?.copyWith(height: 1.5, color: LyraColors.muted),
                    ),
                  ],
                ],
              ),
            ),
            if (trailing != null || onTap != null) ...[
              const SizedBox(width: 8),
              trailing ??
                  const ExcludeSemantics(
                    child: Icon(
                      LyraIcons.disclosure,
                      size: 18,
                      color: LyraColors.mutedSoft,
                    ),
                  ),
            ],
          ],
        ),
      ),
    ),
  );
}

class ProfileNotice extends StatelessWidget {
  const ProfileNotice({
    super.key,
    required this.message,
    this.onRetry,
    this.retryLabel = '重试',
  });

  final String message;
  final VoidCallback? onRetry;
  final String retryLabel;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 16),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          message,
          style: Theme.of(context).textTheme.bodyMedium?.copyWith(height: 1.6),
        ),
        if (onRetry != null) ...[
          const SizedBox(height: 6),
          TextButton.icon(
            onPressed: onRetry,
            icon: const Icon(LyraIcons.sync, size: 18),
            label: Text(retryLabel),
          ),
        ],
      ],
    ),
  );
}

/// 二级设置页延续主页面的标题、卡片留白，并保留系统返回路径。
///
/// [title] 是非空页面名称；[child] 是可纵向滚动的内容。内容宽度限制为 720px，
/// 窄屏保留 14px 外边距，大字标题可在工具栏内换行。
class ProfileSubpage extends StatelessWidget {
  const ProfileSubpage({super.key, required this.title, required this.child});

  final String title;
  final Widget child;

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      toolbarHeight: MediaQuery.textScalerOf(context).scale(24) > 32 ? 88 : 68,
      titleSpacing: 0,
      leadingWidth: 58,
      title: Text(
        title,
        style: Theme.of(context).textTheme.headlineMedium?.copyWith(
          fontSize: 24,
          fontWeight: FontWeight.w600,
          height: 1.25,
          letterSpacing: -.4,
        ),
      ),
    ),
    body: SafeArea(
      top: false,
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 32),
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: child,
          ),
        ),
      ),
    ),
  );
}
