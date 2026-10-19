import 'package:flutter/material.dart';

import '../core/design/lyra_colors.dart';
import '../core/design/lyra_icons.dart';
import '../core/design/lyra_primary_icon.dart';
import '../features/agent/agent_screen.dart';
import '../features/ledger/ledger_screen.dart';
import '../features/overview/overview_screen.dart';
import '../features/profile/profile_screen.dart';

/// 金融工作台与个人设置分开；AI 从页内进入独立二级页。
enum LyraDestination {
  overview('总览', LyraPrimaryGlyph.overview),
  stocks('股票', LyraPrimaryGlyph.stocks),
  ledger('交易', LyraPrimaryGlyph.ledger),
  profile('我的', LyraPrimaryGlyph.profile);

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
          _visited.contains(3)
              ? const ProfileScreen()
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
                  constraints: const BoxConstraints(minHeight: 72),
                  padding: EdgeInsets.symmetric(
                    horizontal: constraints.maxWidth >= 1200 ? 48 : 24,
                    vertical: 8,
                  ),
                  decoration: const BoxDecoration(
                    border: Border(
                      bottom: BorderSide(color: LyraColors.hairlineStrong),
                    ),
                  ),
                  child: Row(
                    children: [
                      const _LyraWordmark(),
                      const SizedBox(width: 24),
                      Expanded(
                        child: Wrap(
                          alignment: WrapAlignment.center,
                          spacing: 8,
                          runSpacing: 4,
                          children: [
                            for (final (index, destination)
                                in LyraDestination.values.indexed)
                              TextButton.icon(
                                onPressed: () => _selectDestination(index),
                                icon: LyraPrimaryIcon(
                                  destination.icon,
                                  size: 20,
                                  color: index == _selectedIndex
                                      ? LyraColors.accent
                                      : LyraColors.inkSecondary,
                                ),
                                label: Text(destination.label),
                                style: TextButton.styleFrom(
                                  foregroundColor: index == _selectedIndex
                                      ? LyraColors.accent
                                      : LyraColors.inkSecondary,
                                  backgroundColor: index == _selectedIndex
                                      ? LyraColors.accentTint
                                      : Colors.transparent,
                                  padding: const EdgeInsets.symmetric(
                                    horizontal: 18,
                                    vertical: 12,
                                  ),
                                  shape: RoundedRectangleBorder(
                                    borderRadius: BorderRadius.circular(10),
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
                      const SizedBox(width: 24),
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
                // 边线绘制在导航背景之上，避免被子组件的不透明表面覆盖。
                position: DecorationPosition.foreground,
                decoration: const BoxDecoration(
                  border: Border(
                    top: BorderSide(
                      color: LyraColors.hairlineStrong,
                      width: 0.75,
                    ),
                  ),
                ),
                child: NavigationBar(
                  selectedIndex: _selectedIndex,
                  onDestinationSelected: _selectDestination,
                  animationDuration: const Duration(milliseconds: 200),
                  labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
                  destinations: [
                    for (final destination in LyraDestination.values)
                      NavigationDestination(
                        icon: LyraPrimaryIcon(destination.icon),
                        label: destination.label,
                      ),
                  ],
                ),
              ),
      );
    },
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
