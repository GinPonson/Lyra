import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../features/accounts/account_sheet.dart';
import '../../features/accounts/account_workspace.dart';
import '../../features/ledger/ledger_workspace.dart';
import '../api/lyra_api_error.dart';
import 'lyra_colors.dart';
import 'lyra_icons.dart';
import 'lyra_number.dart';
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
                  padding: EdgeInsets.fromLTRB(gutter, 16, gutter, 32),
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
    child: Row(
      children: [
        Expanded(
          child: Text(
            title,
            style: const TextStyle(
              fontSize: 25,
              height: 1.2,
              fontWeight: FontWeight.w600,
              letterSpacing: -.6,
              color: LyraColors.ink,
            ),
          ),
        ),
        ?trailing,
      ],
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

/// 错误不是空数据：保留错误代码与可见重试动作。
class LyraLoadError extends StatelessWidget {
  const LyraLoadError({super.key, required this.error, required this.onRetry});
  final Object error;
  final VoidCallback onRetry;
  @override
  Widget build(BuildContext context) => LyraEmpty(
    icon: LyraIcons.error,
    title: '数据未能加载',
    message: error is LyraApiError
        ? '${(error as LyraApiError).message} · ${(error as LyraApiError).code}'
        : '响应处理失败：$error',
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

/// 金额按展示精度排版；长按或悬停查看原始值，辅助阅读同样保留精度。
class LyraAmount extends StatelessWidget {
  const LyraAmount(
    this.value, {
    super.key,
    this.size = 24,
    this.color,
    this.currency,
    this.decimals = 2,
  });
  final String value;
  final double size;
  final Color? color;
  final String? currency;
  final int decimals;
  @override
  Widget build(BuildContext context) => Tooltip(
    message: '原始值：$value${currency == null ? '' : ' $currency'}',
    child: Text(
      '${LyraNumber.amount(value, decimals: decimals)}${currency == null ? '' : ' $currency'}',
      style: LyraTheme.numeric.copyWith(
        fontSize: size,
        color: color ?? LyraColors.ink,
        letterSpacing: -size * .025,
        height: 1.2,
        fontWeight: FontWeight.w500,
      ),
    ),
  );
}

/// 范围选择器按需读取获授权账户，所有页面共用同一个选择状态。
class AccountScopeButton extends ConsumerWidget {
  const AccountScopeButton({super.key, this.prominent = false});

  /// 账号首页可直接使用范围选择器作为页头，其余页面保持紧凑按钮。
  final bool prominent;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(accountScopeProvider);
    final paired = ref.watch(sessionProvider).value is SessionAuthenticated;
    return TextButton.icon(
      onPressed: paired
          ? () => showModalBottomSheet<void>(
              context: context,
              isScrollControlled: true,
              showDragHandle: true,
              builder: (context) => const _AccountPicker(),
            )
          : null,
      icon: prominent ? null : const Icon(LyraIcons.accountScope, size: 17),
      label: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: prominent ? 230 : 150),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: Text(
                scope.label,
                style: prominent
                    ? const TextStyle(fontSize: 19, fontWeight: FontWeight.w600)
                    : null,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            const SizedBox(width: 6),
            const Icon(LyraIcons.expand, size: 16),
          ],
        ),
      ),
      style: TextButton.styleFrom(
        foregroundColor: LyraColors.ink,
        padding: EdgeInsets.symmetric(horizontal: prominent ? 0 : 12),
      ),
    );
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
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 32),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                '选择证券账户',
                style: TextStyle(fontSize: 24, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 16),
              ListTile(
                title: const Text('全部账号'),
                subtitle: const Text('不同币种分别展示'),
                leading: const Icon(LyraIcons.accountScope),
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
                          leading: const Icon(LyraIcons.cash),
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
