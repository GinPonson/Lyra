import 'package:flutter/material.dart';

import '../core/design/lyra_colors.dart';
import '../core/design/lyra_icons.dart';
import '../features/agent/agent_screen.dart';
import '../features/overview/overview_screen.dart';
import '../features/section/section_screen.dart';

/// 手机使用底部导航；展开宽度使用侧边 NavigationRail。
const double _navigationRailBreakpoint = 720;

/// 五个稳定一级入口，与产品方案保持同序。
enum LyraDestination {
  overview('总览', LyraIcons.overview),
  holdings('持仓', LyraIcons.holdings),
  analytics('分析', LyraIcons.analytics),
  ledger('流水', LyraIcons.ledger),
  agent('Agent', LyraIcons.agent);

  const LyraDestination(this.label, this.icon);

  final String label;
  final IconData icon;
}

/// 一级导航外壳。
///
/// IndexedStack 保留各页面滚动位置；目前不引入路由生成器和状态管理框架，
/// 等账户中心与详情页出现真实导航需求时再接入。
class LyraShell extends StatefulWidget {
  const LyraShell({super.key});

  @override
  State<LyraShell> createState() => _LyraShellState();
}

class _LyraShellState extends State<LyraShell> {
  int _selectedIndex = 0;

  static const List<Widget> _screens = <Widget>[
    OverviewScreen(),
    SectionScreen(
      key: ValueKey<String>('holdings-screen'),
      eyebrow: 'POSITIONS / HISTORICAL COST',
      title: '持仓',
      description: '按账户与合约查看数量、历史成本和到期风险。没有行情源时，不展示伪市值或伪实时涨跌。',
      icon: LyraIcons.holdings,
      sections: <String>['股票与期权', '到期窗口', '账户分组'],
    ),
    SectionScreen(
      key: ValueKey<String>('analytics-screen'),
      eyebrow: 'ANALYTICS / EXPLAINABLE',
      title: '分析',
      description: '月度、归因、回撤和风险都绑定账户范围、账本版本与计算口径。',
      icon: LyraIcons.analytics,
      sections: <String>['月度绩效', '损益归因', '回撤与风险'],
    ),
    SectionScreen(
      key: ValueKey<String>('ledger-screen'),
      eyebrow: 'LEDGER / IMMUTABLE',
      title: '流水',
      description: '交易与现金流按日期形成可追溯时间线，并明确区分 CSV、Excel 与 Gmail 来源。',
      icon: LyraIcons.ledger,
      sections: <String>['全部交易', '现金流', '来源与审计'],
    ),
    AgentScreen(
      key: ValueKey<String>('agent-screen'),
    ),
  ];

  /// 切换一级入口并保留其他页面状态。
  void _selectDestination(int index) {
    if (index == _selectedIndex) {
      return;
    }
    setState(() => _selectedIndex = index);
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (BuildContext context, BoxConstraints constraints) {
        final bool useRail = constraints.maxWidth >= _navigationRailBreakpoint;
        final Widget content = IndexedStack(
          index: _selectedIndex,
          children: _screens,
        );

        if (useRail) {
          return Scaffold(
            body: Row(
              children: <Widget>[
                SafeArea(
                  right: false,
                  child: NavigationRail(
                    selectedIndex: _selectedIndex,
                    onDestinationSelected: _selectDestination,
                    labelType: NavigationRailLabelType.all,
                    groupAlignment: -0.72,
                    leading: const Padding(
                      padding: EdgeInsets.only(top: 16, bottom: 28),
                      child: _LyraMark(),
                    ),
                    destinations: LyraDestination.values
                        .map(
                          (LyraDestination destination) =>
                              NavigationRailDestination(
                                icon: Icon(destination.icon),
                                selectedIcon: Icon(destination.icon),
                                label: Text(destination.label),
                              ),
                        )
                        .toList(growable: false),
                  ),
                ),
                const VerticalDivider(width: 1, thickness: 1),
                Expanded(child: content),
              ],
            ),
          );
        }

        return Scaffold(
          body: content,
          bottomNavigationBar: NavigationBar(
            selectedIndex: _selectedIndex,
            onDestinationSelected: _selectDestination,
            labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
            destinations: LyraDestination.values
                .map(
                  (LyraDestination destination) => NavigationDestination(
                    icon: Icon(destination.icon),
                    selectedIcon: Icon(destination.icon),
                    label: destination.label,
                  ),
                )
                .toList(growable: false),
          ),
        );
      },
    );
  }
}

/// 展开布局侧栏使用的纯文字品牌标记。
class _LyraMark extends StatelessWidget {
  const _LyraMark();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 44,
      height: 44,
      alignment: Alignment.center,
      decoration: const BoxDecoration(
        color: LyraColors.accent,
        shape: BoxShape.circle,
      ),
      child: Text(
        'L',
        style: Theme.of(context).textTheme.titleLarge
            ?.copyWith(color: Colors.white, fontWeight: FontWeight.w800),
      ),
    );
  }
}
