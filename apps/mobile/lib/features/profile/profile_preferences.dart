import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_widgets.dart';
import '../../core/preferences/app_preferences.dart';
import '../accounts/account_workspace.dart';
import 'profile_widgets.dart';

/// 打开启动范围面板；[context] 提供导航，只影响下次启动的账户选择。
Future<void> showStartupAccountSheet(BuildContext context) =>
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      backgroundColor: LyraColors.canvas,
      builder: (_) => const _StartupAccountSheet(),
    );

class _StartupAccountSheet extends ConsumerStatefulWidget {
  const _StartupAccountSheet();

  @override
  ConsumerState<_StartupAccountSheet> createState() =>
      _StartupAccountSheetState();
}

class _StartupAccountSheetState extends ConsumerState<_StartupAccountSheet> {
  late final String? _identity;
  bool _busy = false;
  bool _identityChanged = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _identity = ref.read(preferencesIdentityProvider);
  }

  Future<void> _save(StartupAccountMode mode, [String? accountId]) async {
    if (_busy || _identityChanged) return;
    if (_identity != ref.read(preferencesIdentityProvider)) return;
    final controller = ref.read(appPreferencesProvider.notifier);
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      if (accountId == null) {
        await controller.setStartupAccountMode(mode);
      } else {
        await controller.setSpecificStartupAccount(accountId);
      }
      if (!mounted || _identity != ref.read(preferencesIdentityProvider)) {
        return;
      }
      Navigator.pop(context);
    } on Object {
      if (!mounted || _identity != ref.read(preferencesIdentityProvider)) {
        return;
      }
      setState(() => _error = '启动账户未能保存，请重试。');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(preferencesIdentityProvider, (_, next) {
      if (next != _identity) {
        setState(() {
          _identityChanged = true;
          _busy = false;
          _error = null;
        });
      }
    });
    final preferences =
        ref.watch(appPreferencesProvider).value ??
        ref.read(appPreferencesProvider.notifier).lastSaved;
    final accountPreferences = ref.watch(currentAccountPreferencesProvider);
    final signedIn = ref.watch(sessionProvider).value is SessionAuthenticated;
    final accounts = signedIn ? ref.watch(brokerageAccountsProvider) : null;
    final enabled = !_busy && !_identityChanged && preferences != null;
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(14, 0, 14, 32),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const LyraSheetHeader(title: '启动账户'),
          const Padding(
            padding: EdgeInsets.fromLTRB(8, 0, 8, 16),
            child: Text(
              '下次启动时生效，当前查看范围保持不变。',
              style: TextStyle(color: LyraColors.inkSecondary, height: 1.5),
            ),
          ),
          LyraCard(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
            child: Column(
              children: [
                _ChoiceRow(
                  title: '记住上次查看',
                  subtitle: '回到上次查看的账户范围',
                  selected:
                      preferences?.startupAccountMode ==
                      StartupAccountMode.rememberLast,
                  onTap: enabled
                      ? () => _save(StartupAccountMode.rememberLast)
                      : null,
                ),
                const Divider(height: 1, indent: 14, endIndent: 14),
                _ChoiceRow(
                  title: '全部证券账户',
                  subtitle: '不同币种分别展示',
                  selected:
                      preferences?.startupAccountMode == StartupAccountMode.all,
                  onTap: enabled ? () => _save(StartupAccountMode.all) : null,
                ),
              ],
            ),
          ),
          ProfileSection(
            title: '指定证券账户',
            children: [
              if (_identityChanged)
                const ProfileNotice(message: '登录账号已变化，请关闭后重新打开此设置。')
              else if (!signedIn)
                const ProfileNotice(message: '登录后可以从你的证券账户中选择。')
              else
                accounts!.when(
                  loading: () => const Padding(
                    padding: EdgeInsets.symmetric(vertical: 24),
                    child: LinearProgressIndicator(minHeight: 2),
                  ),
                  error: (_, _) => ProfileNotice(
                    message: '暂时无法读取账户，请稍后重试。',
                    onRetry: () => ref.invalidate(brokerageAccountsProvider),
                    retryLabel: '重新加载账户',
                  ),
                  data: (rows) => rows.isEmpty
                      ? const ProfileNotice(message: '还没有可选择的证券账户，可在“证券账户”中创建。')
                      : Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            for (final account in rows)
                              _ChoiceRow(
                                title: account.displayName,
                                subtitle:
                                    '尾号 ${account.maskedSuffix} · ${account.baseCurrency}',
                                selected:
                                    preferences?.startupAccountMode ==
                                        StartupAccountMode.specific &&
                                    accountPreferences.specificAccountId ==
                                        account.id,
                                onTap: enabled
                                    ? () => _save(
                                        StartupAccountMode.specific,
                                        account.id,
                                      )
                                    : null,
                              ),
                          ],
                        ),
                ),
            ],
          ),
          if (_busy) const LinearProgressIndicator(minHeight: 2),
          if (_error != null) ProfileNotice(message: _error!),
        ],
      ),
    );
  }
}

/// 打开涨跌配色面板；[context] 提供导航，点选后立即保存并关闭。
Future<void> showProfitColorSheet(BuildContext context) =>
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      backgroundColor: LyraColors.canvas,
      builder: (_) => const _ProfitColorSheet(),
    );

class _ProfitColorSheet extends ConsumerStatefulWidget {
  const _ProfitColorSheet();

  @override
  ConsumerState<_ProfitColorSheet> createState() => _ProfitColorSheetState();
}

class _ProfitColorSheetState extends ConsumerState<_ProfitColorSheet> {
  bool _busy = false;
  String? _error;

  Future<void> _save(ProfitColorScheme scheme) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref
          .read(appPreferencesProvider.notifier)
          .setProfitColorScheme(scheme);
      if (mounted) Navigator.pop(context);
    } on Object {
      if (mounted) setState(() => _error = '涨跌配色未能保存，请重试。');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final preferences =
        ref.watch(appPreferencesProvider).value ??
        ref.read(appPreferencesProvider.notifier).lastSaved;
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(14, 0, 14, 32),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const LyraSheetHeader(title: '涨跌配色'),
          const Padding(
            padding: EdgeInsets.fromLTRB(8, 0, 8, 16),
            child: Text(
              '按你的习惯显示，金额正负号保持不变。',
              style: TextStyle(color: LyraColors.inkSecondary, height: 1.5),
            ),
          ),
          LyraCard(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
            child: Column(
              children: [
                for (final scheme in ProfitColorScheme.values)
                  _ChoiceRow(
                    title: scheme == ProfitColorScheme.greenUp
                        ? '绿涨红跌'
                        : '红涨绿跌',
                    selected: preferences?.profitColorScheme == scheme,
                    onTap: _busy || preferences == null
                        ? null
                        : () => _save(scheme),
                    detail: Padding(
                      padding: const EdgeInsets.only(top: 6),
                      child: Wrap(
                        spacing: 18,
                        runSpacing: 4,
                        children: [
                          _ColorSample(
                            label: '上涨',
                            rising: true,
                            color: scheme == ProfitColorScheme.greenUp
                                ? LyraColors.positive
                                : LyraColors.negative,
                          ),
                          _ColorSample(
                            label: '下跌',
                            rising: false,
                            color: scheme == ProfitColorScheme.greenUp
                                ? LyraColors.negative
                                : LyraColors.positive,
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ),
          if (_busy) const LinearProgressIndicator(minHeight: 2),
          if (_error != null) ProfileNotice(message: _error!),
        ],
      ),
    );
  }
}

/// 同一组内的可选行；[selected] 显示真实当前选择，[onTap] 为空时不可修改。
/// [title] 为名称，[subtitle] 和 [detail] 分别提供简短说明和可选预览内容。
class _ChoiceRow extends StatelessWidget {
  const _ChoiceRow({
    required this.title,
    required this.selected,
    this.subtitle,
    this.detail,
    this.onTap,
  });

  final String title;
  final bool selected;
  final String? subtitle;
  final Widget? detail;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Semantics(
    selected: selected,
    child: Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Material(
        color: selected ? LyraColors.accentTint : Colors.transparent,
        borderRadius: BorderRadius.circular(16),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 14),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: Theme.of(context).textTheme.titleSmall
                            ?.copyWith(fontWeight: FontWeight.w500),
                      ),
                      if (subtitle != null) ...[
                        const SizedBox(height: 5),
                        Text(
                          subtitle!,
                          style: const TextStyle(
                            color: LyraColors.muted,
                            height: 1.6,
                          ),
                        ),
                      ],
                      ?detail,
                    ],
                  ),
                ),
                const SizedBox(width: 16),
                ExcludeSemantics(
                  child: Icon(
                    selected ? LyraIcons.confirm : LyraIcons.unselectedChoice,
                    color: selected ? LyraColors.accent : LyraColors.mutedSoft,
                    size: 22,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    ),
  );
}

/// 用图标和文字共同展示 [color] 的市场含义，不依赖颜色辨别涨跌。
/// [label] 为可读方向，[rising] 决定趋势图标的方向。
class _ColorSample extends StatelessWidget {
  const _ColorSample({
    required this.label,
    required this.rising,
    required this.color,
  });

  final String label;
  final bool rising;
  final Color color;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      ExcludeSemantics(
        child: Icon(
          rising ? LyraIcons.arrowUpRight : LyraIcons.arrowDownRight,
          color: color,
          size: 17,
        ),
      ),
      const SizedBox(width: 5),
      Text(label, style: TextStyle(color: color, fontSize: 13)),
    ],
  );
}
