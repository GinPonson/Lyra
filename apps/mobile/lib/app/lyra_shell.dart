import 'package:flutter/material.dart';

import '../core/design/lyra_colors.dart';
import '../core/design/lyra_icons.dart';
import '../core/design/lyra_primary_icon.dart';
import '../features/agent/agent_screen.dart';
import '../features/ledger/ledger_screen.dart';
import '../features/overview/overview_screen.dart';

/// 账号与股票为核心，交易提供记录追溯；AI 从页内进入独立二级页。
enum LyraDestination {
  accounts('账号', LyraPrimaryGlyph.accounts),
  stocks('股票', LyraPrimaryGlyph.stocks),
  ledger('交易', LyraPrimaryGlyph.ledger);

  const LyraDestination(this.label, this.icon);
  final String label;
  final LyraPrimaryGlyph icon;
}

/// 宽屏采用紧凑顶栏，手机采用带标签的底栏；保留各页滚动与筛选状态。
class LyraShell extends StatefulWidget {
  const LyraShell({super.key});
  @override
  State<LyraShell> createState() => _LyraShellState();
}

class _LyraShellState extends State<LyraShell> {
  int _selectedIndex = 0;
  final Set<int> _visited = {0};

  void _selectDestination(int index) {
    if (index == _selectedIndex) return;
    setState(() {
      _selectedIndex = index;
      _visited.add(index);
    });
  }

  void _openAgent() => Navigator.push(
    context,
    MaterialPageRoute<void>(
      builder: (_) => const AgentScreen(key: ValueKey<String>('agent-screen')),
    ),
  );

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final wide = constraints.maxWidth >= 900;
      final content = IndexedStack(
        index: _selectedIndex,
        children: [
          OverviewScreen(
            onOpenLedger: () => _selectDestination(2),
            onOpenStocks: () => _selectDestination(1),
            onOpenAgent: _openAgent,
          ),
          _visited.contains(1)
              ? LedgerScreen(
                  key: const ValueKey('stocks-screen'),
                  stocks: true,
                  onConnect: () => _selectDestination(0),
                )
              : const SizedBox.shrink(),
          _visited.contains(2)
              ? LedgerScreen(
                  key: const ValueKey('ledger-screen'),
                  stocks: false,
                  onConnect: () => _selectDestination(0),
                )
              : const SizedBox.shrink(),
        ],
      );
      return Scaffold(
        body: Column(
          children: [
            if (wide)
              SafeArea(
                bottom: false,
                child: Container(
                  height: 72,
                  padding: const EdgeInsets.symmetric(horizontal: 48),
                  decoration: const BoxDecoration(
                    border: Border(
                      bottom: BorderSide(color: LyraColors.hairlineStrong),
                    ),
                  ),
                  child: Row(
                    children: [
                      const _LyraWordmark(),
                      const Spacer(),
                      for (final (index, destination)
                          in LyraDestination.values.indexed)
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 12),
                          child: TextButton.icon(
                            onPressed: () => _selectDestination(index),
                            icon: LyraPrimaryIcon(
                              destination.icon,
                              size: 20,
                              color: index == _selectedIndex
                                  ? LyraColors.accent
                                  : LyraColors.muted,
                            ),
                            label: Text(destination.label),
                            style: TextButton.styleFrom(
                              foregroundColor: index == _selectedIndex
                                  ? LyraColors.ink
                                  : LyraColors.muted,
                              backgroundColor: index == _selectedIndex
                                  ? LyraColors.surfaceStrong
                                  : Colors.transparent,
                              padding: const EdgeInsets.symmetric(
                                horizontal: 22,
                                vertical: 12,
                              ),
                            ),
                          ),
                        ),
                      const Spacer(),
                      TextButton.icon(
                        onPressed: _openAgent,
                        icon: const Icon(LyraIcons.agent, size: 18),
                        label: const Text('AI 分析'),
                      ),
                    ],
                  ),
                ),
              ),
            Expanded(child: content),
          ],
        ),
        bottomNavigationBar: wide
            ? null
            : DecoratedBox(
                decoration: const BoxDecoration(
                  border: Border(
                    top: BorderSide(color: LyraColors.hairlineStrong),
                  ),
                ),
                child: NavigationBar(
                  // 选中底色由图形容器控制尺寸，导航按钮仍保留完整点击区域。
                  indicatorColor: Colors.transparent,
                  selectedIndex: _selectedIndex,
                  onDestinationSelected: _selectDestination,
                  labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
                  destinations: [
                    for (final destination in LyraDestination.values)
                      NavigationDestination(
                        icon: _NavigationGlyph(destination.icon),
                        selectedIcon: _NavigationGlyph(
                          destination.icon,
                          selected: true,
                        ),
                        label: destination.label,
                      ),
                  ],
                ),
              ),
      );
    },
  );
}

/// 选中背景从默认宽胶囊收为小圆角块，不影响导航的触控和语义区域。
class _NavigationGlyph extends StatelessWidget {
  const _NavigationGlyph(this.glyph, {this.selected = false});
  final LyraPrimaryGlyph glyph;
  final bool selected;

  @override
  Widget build(BuildContext context) => Container(
    width: 46,
    height: 34,
    alignment: Alignment.center,
    decoration: BoxDecoration(
      color: selected ? LyraColors.accentTint : Colors.transparent,
      borderRadius: BorderRadius.circular(11),
    ),
    child: LyraPrimaryIcon(
      glyph,
      color: selected ? LyraColors.accent : LyraColors.muted,
    ),
  );
}

/// 几何图标与品牌文字组合，不使用表情符号。
class _LyraWordmark extends StatelessWidget {
  const _LyraWordmark();
  @override
  Widget build(BuildContext context) => const Row(
    children: [
      Icon(LyraIcons.cost, size: 29, color: LyraColors.accent),
      SizedBox(width: 10),
      Text(
        'lyra',
        style: TextStyle(
          fontSize: 32,
          fontWeight: FontWeight.w600,
          letterSpacing: -2,
        ),
      ),
    ],
  );
}
