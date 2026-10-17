import 'package:flutter/material.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';

/// 设置以安静的表面分组，行高由内容和文字大小决定。
class ProfileSection extends StatelessWidget {
  const ProfileSection({
    super.key,
    required this.title,
    required this.children,
  });

  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 24),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.only(left: 2, bottom: 8),
          child: Text(
            title,
            style: Theme.of(context).textTheme.labelMedium?.copyWith(
              color: LyraColors.muted,
              fontWeight: FontWeight.w500,
              letterSpacing: 0,
            ),
          ),
        ),
        Material(
          color: LyraColors.surface,
          borderRadius: BorderRadius.circular(16),
          clipBehavior: Clip.antiAlias,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final (index, child) in children.indexed) ...[
                if (index > 0)
                  const Divider(height: 1, indent: 48, endIndent: 14),
                child,
              ],
            ],
          ),
        ),
      ],
    ),
  );
}

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
        padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 14),
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
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Wrap(
                    alignment: WrapAlignment.spaceBetween,
                    spacing: 12,
                    runSpacing: 4,
                    children: [
                      Text(
                        title,
                        style: Theme.of(context).textTheme.titleSmall?.copyWith(
                          color: enabled ? LyraColors.ink : LyraColors.muted,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                      if (value != null)
                        Text(
                          value!,
                          style: Theme.of(context).textTheme.bodySmall
                              ?.copyWith(
                                color: LyraColors.inkSecondary,
                                height: 1.5,
                              ),
                        ),
                    ],
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

/// 二级设置页保留返回路径，并限制宽屏文字行长。
class ProfileSubpage extends StatelessWidget {
  const ProfileSubpage({super.key, required this.title, required this.child});

  final String title;
  final Widget child;

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text(title)),
    body: SafeArea(
      top: false,
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 12, 20, 32),
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

class ProfileSheetHeader extends StatelessWidget {
  const ProfileSheetHeader({super.key, required this.title});

  final String title;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Expanded(
        child: Text(title, style: Theme.of(context).textTheme.titleLarge),
      ),
      IconButton(
        tooltip: '关闭',
        onPressed: () => Navigator.pop(context),
        icon: const Icon(LyraIcons.dismiss),
      ),
    ],
  );
}
