import 'package:flutter/material.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';

/// 设置页以细线分组，行高由内容和文字大小决定。
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
    padding: const EdgeInsets.only(top: 28),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.only(bottom: 8),
          child: Text(
            title,
            style: Theme.of(context).textTheme.labelLarge
                ?.copyWith(color: LyraColors.muted),
          ),
        ),
        DecoratedBox(
          decoration: const BoxDecoration(
            border: Border(
              top: BorderSide(color: LyraColors.hairlineStrong),
              bottom: BorderSide(color: LyraColors.hairlineStrong),
            ),
          ),
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

class ProfileSettingRow extends StatelessWidget {
  const ProfileSettingRow({
    super.key,
    required this.title,
    required this.icon,
    this.subtitle,
    this.onTap,
    this.trailing,
    this.enabled = true,
  });

  final String title;
  final IconData icon;
  final String? subtitle;
  final VoidCallback? onTap;
  final Widget? trailing;
  final bool enabled;

  @override
  Widget build(BuildContext context) => Material(
    color: Colors.transparent,
    child: InkWell(
      onTap: enabled ? onTap : null,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 2),
        child: Row(
          children: [
            ExcludeSemantics(
              child: Icon(
                icon,
                size: 22,
                color: enabled ? LyraColors.inkSecondary : LyraColors.mutedSoft,
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: Theme.of(context).textTheme.titleSmall?.copyWith(
                      color: enabled ? LyraColors.ink : LyraColors.muted,
                    ),
                  ),
                  if (subtitle != null) ...[
                    const SizedBox(height: 5),
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
              const SizedBox(width: 12),
              trailing ??
                  const ExcludeSemantics(
                    child: Icon(
                      LyraIcons.disclosure,
                      size: 20,
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
        padding: const EdgeInsets.fromLTRB(24, 12, 24, 36),
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
