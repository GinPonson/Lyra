import 'package:flutter/material.dart';

import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';

/// 尚未接入数据源的一级页面骨架。
///
/// 骨架只展示页面职责与空状态，不伪造金融数字；真实用例接入时直接替换内容区。
class SectionScreen extends StatelessWidget {
  const SectionScreen({
    required this.eyebrow,
    required this.title,
    required this.description,
    required this.icon,
    required this.sections,
    this.accent = LyraColors.accent,
    super.key,
  });

  final String eyebrow;
  final String title;
  final String description;
  final IconData icon;
  final List<String> sections;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return ColoredBox(
      color: LyraColors.canvas,
      child: SafeArea(
        bottom: false,
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 920),
            child: CustomScrollView(
              key: PageStorageKey<String>('section-$title'),
              slivers: <Widget>[
                SliverPadding(
                  padding: const EdgeInsets.fromLTRB(20, 30, 20, 40),
                  sliver: SliverList.list(
                    children: <Widget>[
                      Text(
                        eyebrow,
                        style: Theme.of(context).textTheme.labelSmall
                            ?.copyWith(color: accent),
                      ),
                      const SizedBox(height: 14),
                      Text(
                        title,
                        key: ValueKey<String>('section-title-$title'),
                        style: Theme.of(context).textTheme.headlineLarge,
                      ),
                      const SizedBox(height: 14),
                      Text(
                        description,
                        style: Theme.of(context).textTheme.bodyLarge
                            ?.copyWith(color: LyraColors.inkSecondary, height: 1.55),
                      ),
                      const SizedBox(height: 34),
                      Container(
                        padding: const EdgeInsets.all(24),
                        decoration: BoxDecoration(
                          color: LyraColors.surface,
                          borderRadius: BorderRadius.circular(16),
                          border: Border.all(color: LyraColors.hairline),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: <Widget>[
                            Container(
                              width: 44,
                              height: 44,
                              alignment: Alignment.center,
                              decoration: BoxDecoration(
                                color: LyraColors.accentSoft,
                                shape: BoxShape.circle,
                              ),
                              child: Icon(icon, color: accent, size: 22),
                            ),
                            const SizedBox(height: 20),
                            Text(
                              '等待服务端数据',
                              style: Theme.of(context).textTheme.titleMedium,
                            ),
                            const SizedBox(height: 8),
                            Text(
                              '连接后将在此显示经过账户范围和账本版本校验的真实结果。',
                              style: Theme.of(context).textTheme.bodyMedium
                                  ?.copyWith(color: LyraColors.muted),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 16),
                      ...sections.map(
                        (String section) => Container(
                          margin: const EdgeInsets.only(bottom: 8),
                          padding: const EdgeInsets.symmetric(
                            horizontal: 18,
                            vertical: 16,
                          ),
                          decoration: BoxDecoration(
                            color: LyraColors.surface,
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(color: LyraColors.hairline),
                          ),
                          child: Row(
                            children: <Widget>[
                              Expanded(
                                child: Text(
                                  section,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w600,
                                    color: LyraColors.ink,
                                  ),
                                ),
                              ),
                              const Icon(
                                LyraIcons.disclosure,
                                color: LyraColors.muted,
                                size: 20,
                              ),
                            ],
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
