import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/preferences/app_preferences.dart';
import '../accounts/account_repository.dart';
import '../accounts/account_workspace.dart';
import '../overview/login_panel.dart';
import '../market/market_widgets.dart';
import '../system/system_workspace.dart';
import 'about_screen.dart';
import 'brokerage_accounts_screen.dart';
import 'profile_preferences.dart';
import 'profile_widgets.dart';

class ProfileScreen extends ConsumerStatefulWidget {
  const ProfileScreen({super.key});

  @override
  ConsumerState<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends ConsumerState<ProfileScreen> {
  bool _saving = false;
  String? _saveError;

  Future<void> _saveHidden(bool hidden) async {
    if (_saving) return;
    setState(() {
      _saving = true;
      _saveError = null;
    });
    try {
      await ref
          .read(appPreferencesProvider.notifier)
          .setHideAmountsOnStartup(hidden);
    } on Object {
      if (mounted) setState(() => _saveError = '隐藏金额设置未能保存，请重试。');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _reloadPreferences() async {
    if (_saving) return;
    setState(() {
      _saving = true;
      _saveError = null;
    });
    try {
      await ref.read(appPreferencesProvider.notifier).reload();
    } on Object {
      if (mounted) setState(() => _saveError = '暂时无法读取本机设置，请重试。');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  void _openPage(Widget page) =>
      Navigator.push<void>(context, MaterialPageRoute(builder: (_) => page));

  String _startupLabel(
    AppPreferences preferences,
    List<BrokerageAccount>? accounts,
  ) {
    switch (preferences.startupAccountMode) {
      case StartupAccountMode.rememberLast:
        return '记住上次查看';
      case StartupAccountMode.all:
        return '全部证券账户';
      case StartupAccountMode.specific:
        final id = ref
            .watch(currentAccountPreferencesProvider)
            .specificAccountId;
        final matching = accounts?.where((account) => account.id == id);
        if (matching != null && matching.isNotEmpty) {
          return matching.first.displayName;
        }
        if (id == null) {
          return ref.watch(preferencesIdentityProvider) == null
              ? '指定账户 · 登录后选择'
              : '指定账户 · 请选择证券账户';
        }
        if (accounts == null) return '指定账户 · 等待读取';
        return '原指定账户已不可用，将显示全部账户';
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(sessionProvider);
    final identity = session.value is SessionAuthenticated
        ? session.value! as SessionAuthenticated
        : null;
    final preferences = ref.watch(appPreferencesProvider);
    final saved =
        preferences.value ??
        ref.read(appPreferencesProvider.notifier).lastSaved;
    final accounts = identity == null
        ? null
        : ref.watch(brokerageAccountsProvider);
    final url = ref.watch(currentServerUrlProvider);
    final connection = url == null ? null : ref.watch(systemInfoProvider(url));
    final connectionLabel = connection?.when(
      data: (value) => connectionStatusLabel(value.status),
      loading: () => '正在检测…',
      error: (_, _) => '暂时无法连接，可重新检测',
    );
    return SafeArea(
      bottom: false,
      child: SingleChildScrollView(
        key: const PageStorageKey('profile-scroll'),
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  '我的',
                  style: Theme.of(context).textTheme.headlineLarge?.copyWith(
                    fontSize: 22,
                    fontWeight: FontWeight.w500,
                    letterSpacing: 0,
                  ),
                ),
                const SizedBox(height: 24),
                if (session.isLoading)
                  const LinearProgressIndicator(minHeight: 2)
                else if (identity != null)
                  _ProfileIdentity(email: identity.snapshot.userEmail)
                else
                  const LoginPanel(),
                ProfileSection(
                  title: '账户与数据',
                  children: [
                    ProfileSettingRow(
                      key: const ValueKey('profile-accounts'),
                      title: '证券账户',
                      glyph: LyraPrimaryGlyph.brokerage,
                      subtitle: identity == null
                          ? '登录后管理证券账户'
                          : accounts!.when(
                              data: (rows) => rows.isEmpty
                                  ? '创建你的第一个证券账户'
                                  : '${rows.length} 个账户 · 查看或新增',
                              loading: () => '正在读取账户…',
                              error: (_, _) => '暂时无法读取账户',
                            ),
                      onTap: () => _openPage(const BrokerageAccountsScreen()),
                    ),
                  ],
                ),
                ProfileSection(
                  title: '使用偏好',
                  children: [
                    if (saved == null && preferences.isLoading)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 24),
                        child: LinearProgressIndicator(minHeight: 2),
                      ),
                    if (saved != null) ...[
                      ProfileSettingRow(
                        title: '启动时隐藏金额',
                        glyph: LyraPrimaryGlyph.hideAmounts,
                        subtitle: '下次打开 Lyra 生效。隐藏财务页面金额，AI 对话文字除外。',
                        enabled: !_saving,
                        trailing: Semantics(
                          label: '启动时隐藏财务页面金额',
                          child: Switch.adaptive(
                            key: const ValueKey('startup-hide-amounts'),
                            value: saved.hideAmountsOnStartup,
                            onChanged: _saving ? null : _saveHidden,
                          ),
                        ),
                      ),
                      ProfileSettingRow(
                        key: const ValueKey('profile-startup-account'),
                        title: '启动账户',
                        glyph: LyraPrimaryGlyph.accountScope,
                        value: _startupLabel(saved, accounts?.asData?.value),
                        onTap: () => showStartupAccountSheet(context),
                      ),
                      ProfileSettingRow(
                        key: const ValueKey('profile-profit-colors'),
                        title: '涨跌配色',
                        glyph: LyraPrimaryGlyph.profitColors,
                        value:
                            saved.profitColorScheme == ProfitColorScheme.greenUp
                            ? '绿涨红跌'
                            : '红涨绿跌',
                        onTap: () => showProfitColorSheet(context),
                      ),
                    ],
                    if (preferences.hasError || _saveError != null)
                      ProfileNotice(
                        message: _saveError ?? '本机设置暂时无法读取或保存，已保存的偏好会保留。',
                        onRetry: _saving ? null : _reloadPreferences,
                        retryLabel: '重新读取设置',
                      ),
                  ],
                ),
                ProfileSection(
                  title: '服务与应用',
                  children: [
                    ProfileSettingRow(
                      key: const ValueKey('profile-connection'),
                      title: '服务端连接',
                      glyph: LyraPrimaryGlyph.server,
                      subtitle: url == null
                          ? '设置服务端地址'
                          : '${serverDisplayName(url)} · $connectionLabel',
                      onTap: () => showConnectionManagementSheet(context),
                    ),
                    ProfileSettingRow(
                      key: const ValueKey('profile-market-data'),
                      title: '行情与估值',
                      icon: Icons.query_stats_outlined,
                      subtitle: '参考价格、报价时点与数据状态',
                      onTap: () => showMarketDataSheet(context),
                    ),
                    ProfileSettingRow(
                      key: const ValueKey('profile-about'),
                      title: '关于 Lyra',
                      glyph: LyraPrimaryGlyph.about,
                      subtitle: '版本信息与项目地址',
                      onTap: () => _openPage(const AboutScreen()),
                    ),
                  ],
                ),
                if (identity != null) ...[
                  const SizedBox(height: 24),
                  OutlinedButton.icon(
                    key: const ValueKey('profile-sign-out'),
                    onPressed: () => showDeviceSignOutDialog(context),
                    icon: const LyraPrimaryIcon(
                      LyraPrimaryGlyph.logout,
                      size: 18,
                    ),
                    label: const Text('退出登录'),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _ProfileIdentity extends StatelessWidget {
  const _ProfileIdentity({this.email});
  final String? email;

  @override
  Widget build(BuildContext context) {
    final initial = email == null || email!.isEmpty
        ? 'L'
        : email!.characters.first.toUpperCase();
    return Row(
      children: [
        ExcludeSemantics(
          child: CircleAvatar(
            radius: 24,
            backgroundColor: LyraColors.accentTint,
            foregroundColor: LyraColors.accent,
            child: Text(
              initial,
              textScaler: TextScaler.noScaling,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w500),
            ),
          ),
        ),
        const SizedBox(width: 14),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                email ?? 'Google 账号',
                style: Theme.of(context).textTheme.titleMedium
                    ?.copyWith(fontSize: 15, fontWeight: FontWeight.w500),
              ),
              const SizedBox(height: 4),
              const Text(
                'Google 已登录',
                style: TextStyle(
                  color: LyraColors.muted,
                  fontSize: 13,
                  height: 1.5,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}
