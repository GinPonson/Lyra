import 'package:flutter/material.dart';

import '../../core/design/lyra_colors.dart';

/// 一级页面的分组入口条目。
class SectionEntry {
  const SectionEntry({
    required this.icon,
    required this.title,
    required this.subtitle,
    this.status,
  });

  final IconData icon;
  final String title;
  final String subtitle;

  /// 入口当前状态(如"即将接入");为空表示可进入。
  final String? status;
}

/// 尚未接入数据源的一级页面占位。
///
/// 只保留紧凑标题与分组入口,不伪造金融数字;真实用例接入时直接替换内容区。
/// 入口行用状态标签如实标注"即将接入",不使用暗示可点的箭头。
class SectionScreen extends StatelessWidget {
  const SectionScreen({required this.title, required this.sections, super.key});

  final String title;
  final List<SectionEntry> sections;

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
                  padding: const EdgeInsets.fromLTRB(20, 12, 20, 40),
                  sliver: SliverList.list(
                    children: <Widget>[
                      // 与总览头部同一级标题尺度,不用页面级大标题。
                      Text(
                        title,
                        key: ValueKey<String>('section-title-$title'),
                        style: Theme.of(context).textTheme.headlineLarge
                            ?.copyWith(fontSize: 22, letterSpacing: -0.3),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        '账本数据接入后,这里提供可筛选的明细与分组视图。',
                        style: Theme.of(context).textTheme.bodyMedium
                            ?.copyWith(color: LyraColors.muted),
                      ),
                      const SizedBox(height: 20),
                      // 分组直接铺在画布上,行间发丝线,无外框。
                      Column(
                        children: <Widget>[
                          for (final (int index, SectionEntry entry)
                              in sections.indexed)
                            _EntryRow(
                              entry: entry,
                              isLast: index == sections.length - 1,
                            ),
                        ],
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

class _EntryRow extends StatelessWidget {
  const _EntryRow({required this.entry, required this.isLast});

  final SectionEntry entry;
  final bool isLast;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 14),
      decoration: BoxDecoration(
        border: isLast
            ? null
            : const Border(bottom: BorderSide(color: LyraColors.hairline)),
      ),
      child: Row(
        children: <Widget>[
          Container(
            width: 38,
            height: 38,
            alignment: Alignment.center,
            decoration: const BoxDecoration(
              color: LyraColors.accentTint,
              shape: BoxShape.circle,
            ),
            child: Icon(entry.icon, size: 19, color: LyraColors.accent),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text(
                  entry.title,
                  style: const TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w600,
                    color: LyraColors.ink,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  entry.subtitle,
                  style: const TextStyle(
                    fontSize: 12.5,
                    color: LyraColors.muted,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 12),
          if (entry.status != null)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: LyraColors.surfaceStrong,
                borderRadius: BorderRadius.circular(999),
              ),
              child: Text(
                entry.status!,
                style: const TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w500,
                  color: LyraColors.muted,
                ),
              ),
            ),
        ],
      ),
    );
  }
}
