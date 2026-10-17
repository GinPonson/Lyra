import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../features/accounts/account_sheet.dart';
import '../../features/accounts/account_workspace.dart';
import '../../features/ledger/ledger_workspace.dart';
import '../preferences/app_preferences.dart';
import 'lyra_colors.dart';
import 'lyra_error_message.dart';
import 'lyra_icons.dart';
import 'lyra_number.dart';
import 'lyra_primary_icon.dart';
import 'lyra_theme.dart';

/// 自适应金融页面：宽屏有边界，窄屏保留阅读留白与可滚动内容。
class LyraPage extends StatelessWidget {
  const LyraPage({super.key, required this.storageKey, required this.children});
  final String storageKey;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => SafeArea(
    bottom: false,
    child: LayoutBuilder(
      builder: (context, constraints) {
        final gutter = constraints.maxWidth >= 800 ? 40.0 : 20.0;
        return Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1240),
            child: CustomScrollView(
              key: PageStorageKey<String>(storageKey),
              slivers: [
                SliverPadding(
                  padding: EdgeInsets.fromLTRB(gutter, 24, gutter, 32),
                  sliver: SliverToBoxAdapter(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: children,
                    ),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    ),
  );
}

/// 一次性的错层进入动效；尊重系统减少动画设置，滚动不重复触发。
class LyraReveal extends StatelessWidget {
  const LyraReveal({super.key, required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) => TweenAnimationBuilder<double>(
    tween: Tween(begin: 0, end: 1),
    duration: MediaQuery.disableAnimationsOf(context)
        ? Duration.zero
        : const Duration(milliseconds: 280),
    curve: Curves.easeOutCubic,
    builder: (context, value, child) => Opacity(
      opacity: value,
      child: Transform.translate(
        offset: Offset(0, 8 * (1 - value)),
        child: child,
      ),
    ),
    child: child,
  );
}

/// 紧凑页标题与右侧操作共享一行，让首屏空间优先留给金融数据。
class LyraPageTitle extends StatelessWidget {
  const LyraPageTitle({super.key, required this.title, this.trailing});
  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 16),
    child: LayoutBuilder(
      builder: (context, constraints) {
        final heading = Text(
          title,
          style: const TextStyle(
            fontSize: 22,
            height: 1.2,
            fontWeight: FontWeight.w500,
            letterSpacing: -.2,
            color: LyraColors.ink,
          ),
        );
        if (trailing != null &&
            constraints.maxWidth < 360 &&
            MediaQuery.textScalerOf(context).scale(22) > 26) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [heading, const SizedBox(height: 8), trailing!],
          );
        }
        return Row(
          children: [
            Expanded(child: heading),
            ?trailing,
          ],
        );
      },
    ),
  );
}

/// 有边界的图标按钮，统一可点击区域和无障碍标签。
class LyraIconButton extends StatelessWidget {
  const LyraIconButton({
    super.key,
    required this.icon,
    required this.label,
    required this.onPressed,
  });
  final IconData icon;
  final String label;
  final VoidCallback? onPressed;
  @override
  Widget build(BuildContext context) => IconButton.outlined(
    tooltip: label,
    onPressed: onPressed,
    icon: Icon(icon, size: 21),
    style: IconButton.styleFrom(
      minimumSize: const Size(46, 46),
      side: const BorderSide(color: LyraColors.hairlineStrong),
    ),
  );
}

/// 空数据、断连和筛选零结果共用布局，调用方必须明确说明原因。
class LyraEmpty extends StatelessWidget {
  const LyraEmpty({
    super.key,
    required this.title,
    required this.message,
    this.icon = LyraIcons.portfolio,
    this.action,
  });
  final String title;
  final String message;
  final IconData icon;
  final Widget? action;
  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 40),
    decoration: BoxDecoration(
      color: LyraColors.surfaceSubtle,
      borderRadius: BorderRadius.circular(20),
    ),
    child: Column(
      children: [
        Container(
          padding: const EdgeInsets.all(18),
          decoration: const BoxDecoration(
            shape: BoxShape.circle,
            color: LyraColors.surface,
          ),
          child: Icon(icon, size: 30, color: LyraColors.accent),
        ),
        const SizedBox(height: 20),
        Text(
          title,
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 10),
        ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 380),
          child: Text(
            message,
            textAlign: TextAlign.center,
            style: const TextStyle(
              height: 1.7,
              color: LyraColors.inkSecondary,
              fontSize: 13,
            ),
          ),
        ),
        if (action != null) ...[const SizedBox(height: 24), action!],
      ],
    ),
  );
}

/// 数据读取失败时说明下一步操作，诊断信息留在错误对象中。
class LyraLoadError extends StatelessWidget {
  const LyraLoadError({super.key, required this.error, required this.onRetry});
  final Object error;
  final VoidCallback onRetry;
  @override
  Widget build(BuildContext context) => LyraEmpty(
    icon: LyraIcons.error,
    title: '数据未能加载',
    message: lyraErrorMessage(error, fallback: '暂时无法读取数据，请重新加载。'),
    action: OutlinedButton.icon(
      onPressed: onRetry,
      icon: const Icon(LyraIcons.sync, size: 18),
      label: const Text('重新加载'),
    ),
  );
}

/// 骨架屏与数据区占位相同；无永久动画，便于辅助技术和截图测试。
class LyraSkeleton extends StatelessWidget {
  const LyraSkeleton({super.key});
  @override
  Widget build(BuildContext context) => Semantics(
    label: '正在加载账本',
    child: Column(
      children: [
        for (int i = 0; i < 3; i++)
          Container(
            height: 78,
            margin: const EdgeInsets.only(bottom: 12),
            decoration: BoxDecoration(
              color: LyraColors.surfaceSubtle,
              borderRadius: BorderRadius.circular(14),
            ),
          ),
      ],
    ),
  );
}

/// 只为盈亏等带方向的金融数值选色；品牌和成功状态不读取此规则。
Color lyraProfitColor(String value, ProfitColorScheme scheme) {
  if (!RegExp(r'^-?\d+(?:\.\d+)?$').hasMatch(value) ||
      !RegExp(r'[1-9]').hasMatch(value)) {
    return LyraColors.ink;
  }
  final positive = !value.startsWith('-');
  final green = positive == (scheme == ProfitColorScheme.greenUp);
  return green ? LyraColors.positive : LyraColors.negative;
}

/// 金额按展示精度排版；隐藏时同时移除原始值提示与辅助阅读内容。
/// 股数、笔数等复用数字排版时须显式设置 [isMonetary] 为 false。
class LyraAmount extends ConsumerWidget {
  const LyraAmount(
    this.value, {
    super.key,
    this.size = 24,
    this.color,
    this.currency,
    this.decimals = 2,
    this.isMonetary = true,
    this.colorBySign = false,
    this.showPositiveSign = false,
    this.exact = false,
    this.textAlign,
  });
  final String value;
  final double size;
  final Color? color;
  final String? currency;
  final int decimals;
  final bool isMonetary;
  final bool colorBySign;

  /// 只在方向本身有意义的汇总盈亏上显示正号；不改变服务端数值。
  final bool showPositiveSign;

  /// 明细沿用服务端原始精度，列表仍使用 [decimals] 进行展示舍入。
  final bool exact;
  final TextAlign? textAlign;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden =
        isMonetary && value != '—' && ref.watch(amountsHiddenProvider);
    final suffix = currency == null ? '' : ' $currency';
    final amountColor = hidden
        ? LyraColors.ink
        : color ??
              (colorBySign
                  ? lyraProfitColor(value, ref.watch(profitColorSchemeProvider))
                  : LyraColors.ink);
    var display = hidden
        ? '••••'
        : (exact ? value : LyraNumber.amount(value, decimals: decimals));
    if (!hidden &&
        showPositiveSign &&
        !value.startsWith('-') &&
        RegExp(r'[1-9]').hasMatch(value) &&
        RegExp(r'^\d').hasMatch(display)) {
      display = '+$display';
    }
    final text = Text(
      '$display$suffix',
      textAlign: textAlign,
      style: LyraTheme.numeric.copyWith(
        fontSize: size,
        color: amountColor,
        letterSpacing: -size * .015,
        height: 1.2,
        fontWeight: FontWeight.w500,
      ),
    );
    if (hidden) {
      return Semantics(
        label: '金额已隐藏${currency == null ? '' : '，$currency'}',
        excludeSemantics: true,
        child: text,
      );
    }
    return Tooltip(message: '原始值：$value$suffix', child: text);
  }
}

/// 范围选择器按需读取获授权账户，所有页面共用同一个选择状态。
class AccountScopeButton extends ConsumerWidget {
  const AccountScopeButton({
    super.key,
    this.prominent = false,
    this.compact = false,
  });

  /// 账号首页可直接使用范围选择器作为页头，其余页面保持紧凑按钮。
  final bool prominent;

  /// 总览单行页头使用短胶囊；长账户名省略但仍可通过提示完整读取。
  final bool compact;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(accountScopeProvider);
    final paired = ref.watch(sessionProvider).value is SessionAuthenticated;
    final button = TextButton.icon(
      onPressed: paired
          ? () => showModalBottomSheet<void>(
              context: context,
              isScrollControlled: true,
              showDragHandle: true,
              builder: (context) => const _AccountPicker(),
            )
          : null,
      icon: prominent || compact
          ? null
          : const LyraPrimaryIcon(LyraPrimaryGlyph.accountScope, size: 17),
      label: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: prominent ? 230 : 150),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: Text(
                scope.label,
                style: TextStyle(
                  fontSize: compact ? 13 : (prominent ? 18 : 14),
                  fontWeight: FontWeight.w500,
                ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            SizedBox(width: compact ? 3 : 6),
            Icon(LyraIcons.expand, size: compact ? 14 : 16),
          ],
        ),
      ),
      style: TextButton.styleFrom(
        foregroundColor: LyraColors.ink,
        backgroundColor: compact ? LyraColors.surface : null,
        padding: EdgeInsets.symmetric(
          horizontal: compact ? 10 : (prominent ? 0 : 12),
        ),
        minimumSize: compact ? const Size(0, 34) : null,
        shape: compact
            ? RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
                side: const BorderSide(color: LyraColors.hairline),
              )
            : null,
      ),
    );
    return compact ? Tooltip(message: scope.label, child: button) : button;
  }
}

class _AccountPicker extends ConsumerWidget {
  const _AccountPicker();
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final accounts = ref.watch(brokerageAccountsProvider);
    final scope = ref.watch(accountScopeProvider);
    void select(String id, String label) {
      ref.read(accountScopeProvider.notifier).select(id, label);
      Navigator.pop(context);
    }

    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * .75,
        ),
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 32),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                '选择证券账户',
                style: TextStyle(fontSize: 22, fontWeight: FontWeight.w500),
              ),
              const SizedBox(height: 16),
              ListTile(
                title: const Text('全部账号'),
                subtitle: const Text('不同币种分别展示'),
                leading: const LyraPrimaryIcon(LyraPrimaryGlyph.accountScope),
                trailing: scope.id == 'all'
                    ? const Icon(LyraIcons.confirm)
                    : null,
                onTap: () => select('all', '全部账号'),
              ),
              accounts.when(
                loading: () => const LyraSkeleton(),
                error: (e, _) => LyraLoadError(
                  error: e,
                  onRetry: () => ref.invalidate(brokerageAccountsProvider),
                ),
                data: (rows) {
                  return Column(
                    children: [
                      if (rows.isEmpty)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 20),
                          child: Text('尚无证券账户，添加后即可导入交易。'),
                        ),
                      for (final account in rows)
                        ListTile(
                          title: Text(account.displayName),
                          subtitle: Text(
                            '${account.maskedSuffix} · ${account.baseCurrency}',
                          ),
                          leading: const LyraPrimaryIcon(
                            LyraPrimaryGlyph.brokerage,
                          ),
                          trailing: scope.id == account.id
                              ? const Icon(LyraIcons.confirm)
                              : null,
                          onTap: () => select(account.id, account.displayName),
                        ),
                    ],
                  );
                },
              ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: () async {
                  final client = ref.read(sessionProvider.notifier).client;
                  final account = await showCreateAccountSheet(context);
                  if (account != null &&
                      context.mounted &&
                      identical(
                        client,
                        ref.read(sessionProvider.notifier).client,
                      )) {
                    Navigator.pop(context);
                  }
                },
                icon: const Icon(LyraIcons.addAccount, size: 18),
                label: const Text('新增证券账户'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
