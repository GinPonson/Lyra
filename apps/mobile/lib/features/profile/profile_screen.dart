import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/preferences/app_preferences.dart';
import '../accounts/account_repository.dart';
import '../accounts/account_workspace.dart';
import '../auth/auth_provider.dart';
import '../overview/login_panel.dart';
import '../market/market_settings.dart';
import '../market/market_widgets.dart';
import '../market/market_workspace.dart';
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
  bool _loadingPreferences = false;
  String? _preferenceReadError;

  /// 重新读取本机设置，并在存储失败时显示错误和重试操作。
  ///
  /// 加载标记会阻止本次读取完成前重复发起请求。
  Future<void> _reloadPreferences() async {
    if (_loadingPreferences) return;
    setState(() {
      _loadingPreferences = true;
      _preferenceReadError = null;
    });
    try {
      await ref.read(appPreferencesProvider.notifier).reload();
    } on Object {
      if (mounted) {
        setState(() => _preferenceReadError = '暂时无法读取本机设置，请重试。');
      }
    } finally {
      if (mounted) setState(() => _loadingPreferences = false);
    }
  }

  /// 将 [page] 推入当前导航栈并返回其结果 Future。
  void _openPage(Widget page) =>
      Navigator.push<void>(context, MaterialPageRoute(builder: (_) => page));

  /// 汇总已登录用户的账户数量，或显示账户列表当前读取状态。
  ///
  /// [accounts] 类型为 `AsyncValue<List<BrokerageAccount>>?`，没有已认证账户
  /// 请求时为 null；[signedIn] 和 [sessionLoading] 是登录及会话加载状态。
  /// 返回值为区分加载、不可用、空列表和已读取状态的字符串。
  String _accountLabel(
    AsyncValue<List<BrokerageAccount>>? accounts, {
    required bool signedIn,
    required bool sessionLoading,
  }) {
    if (sessionLoading) return '正在读取…';
    if (!signedIn) return '登录后管理';
    if (accounts == null) return '正在读取账户…';
    return accounts.when(
      data: (rows) => rows.isEmpty ? '尚无账户' : '${rows.length} 个',
      loading: () => '正在读取账户…',
      error: (_, _) => '暂时无法读取账户',
    );
  }

  /// 返回当前启动账户偏好的显示值。
  ///
  /// [preferences] 为 `AppPreferences` 已保存设置，[accounts] 类型为
  /// `AsyncValue<List<BrokerageAccount>>?`，代表账户列表实时请求状态；已不可用的
  /// 指定账户会明确显示，不会回落为正常状态。返回值为紧凑状态字符串。
  String _startupLabel(
    AppPreferences preferences,
    AsyncValue<List<BrokerageAccount>>? accounts,
  ) {
    switch (preferences.startupAccountMode) {
      case StartupAccountMode.rememberLast:
        return '上次查看';
      case StartupAccountMode.all:
        return '全部账户';
      case StartupAccountMode.specific:
        final id = ref
            .watch(currentAccountPreferencesProvider)
            .specificAccountId;
        if (id == null) {
          return ref.watch(preferencesIdentityProvider) == null
              ? '登录后选择'
              : '请选择账户';
        }
        if (accounts == null) return '登录后选择';
        return accounts.when(
          data: (rows) {
            for (final account in rows) {
              if (account.id == id) return account.displayName;
            }
            return '原账户不可用';
          },
          loading: () => '正在读取…',
          error: (_, _) => '账户读取失败',
        );
    }
  }

  /// 汇总当前登录会话中服务端生效的行情来源。
  ///
  /// [settings] 类型为 `AsyncValue<MarketSettings>?`，代表行情设置实时请求；
  /// [signedIn] 与 [sessionLoading] 是布尔状态，用于区分未登录、会话加载和行情
  /// 来源状态。返回值为实际有效来源数量或读取状态的字符串。
  String _marketLabel(
    AsyncValue<MarketSettings>? settings, {
    required bool signedIn,
    required bool sessionLoading,
  }) {
    if (sessionLoading) return '正在读取…';
    if (!signedIn) return '登录后设置';
    if (settings == null) return '正在读取…';
    return settings.when(
      data: (value) {
        final activeSourceIds = {
          ...value.effectiveRoutes.stock,
          ...value.effectiveRoutes.option,
        };
        return activeSourceIds.isEmpty
            ? '暂无可用来源'
            : '${activeSourceIds.length} 个来源';
      },
      loading: () => '正在读取…',
      error: (_, _) => '行情来源读取失败',
    );
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
    final marketSettings = identity == null
        ? null
        : ref.watch(marketSettingsProvider);
    return SafeArea(
      bottom: false,
      child: SingleChildScrollView(
        key: const PageStorageKey('profile-scroll'),
        padding: const EdgeInsets.fromLTRB(14, 36, 14, 28),
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                  child: Text(
                    '我的',
                    style: Theme.of(context).textTheme.headlineLarge?.copyWith(
                      fontSize: 28,
                      fontWeight: FontWeight.w600,
                      letterSpacing: 0,
                    ),
                  ),
                ),
                const SizedBox(height: 18),
                if (session.isLoading)
                  const LinearProgressIndicator(minHeight: 2)
                else if (identity != null)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    child: _ProfileIdentity(
                      name: identity.snapshot.userEmail?.isNotEmpty == true
                          ? identity.snapshot.userEmail
                          : identity.snapshot.userDisplayName,
                      provider:
                          AuthProvider.fromId(identity.snapshot.authMethod)
                              ?.label ??
                          '账号',
                    ),
                  )
                else
                  const LoginPanel(),
                ProfileSection(
                  children: [
                    ProfileSettingRow(
                      key: const ValueKey('profile-accounts'),
                      title: '证券账户',
                      glyph: LyraPrimaryGlyph.brokerage,
                      value: _accountLabel(
                        accounts,
                        signedIn: identity != null,
                        sessionLoading: session.isLoading,
                      ),
                      onTap: () => _openPage(const BrokerageAccountsScreen()),
                    ),
                    if ((saved == null && preferences.isLoading) ||
                        _loadingPreferences)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 24),
                        child: LinearProgressIndicator(minHeight: 2),
                      ),
                    if (saved != null) ...[
                      ProfileSettingRow(
                        key: const ValueKey('profile-startup-account'),
                        title: '启动账户',
                        glyph: LyraPrimaryGlyph.accountScope,
                        value: _startupLabel(saved, accounts),
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
                    if (preferences.hasError || _preferenceReadError != null)
                      ProfileNotice(
                        message:
                            _preferenceReadError ?? '本机设置暂时无法读取或保存，已保存的偏好会保留。',
                        onRetry: _loadingPreferences
                            ? null
                            : _reloadPreferences,
                        retryLabel: '重新读取设置',
                      ),
                  ],
                ),
                ProfileSection(
                  children: [
                    ProfileSettingRow(
                      key: const ValueKey('profile-market-data'),
                      title: '行情与估值',
                      glyph: LyraPrimaryGlyph.stocks,
                      value: _marketLabel(
                        marketSettings,
                        signedIn: identity != null,
                        sessionLoading: session.isLoading,
                      ),
                      onTap: () => showMarketDataSheet(context),
                    ),
                    ProfileSettingRow(
                      key: const ValueKey('profile-about'),
                      title: '关于 Lyra',
                      glyph: LyraPrimaryGlyph.about,
                      onTap: () => _openPage(const AboutScreen()),
                    ),
                  ],
                ),
                if (identity != null) ...[
                  const SizedBox(height: 24),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton.icon(
                      key: const ValueKey('profile-sign-out'),
                      onPressed: () => showDeviceSignOutDialog(context),
                      icon: const LyraPrimaryIcon(
                        LyraPrimaryGlyph.logout,
                        size: 20,
                        color: LyraColors.inkSecondary,
                      ),
                      label: const Text('退出登录'),
                      style: TextButton.styleFrom(
                        foregroundColor: LyraColors.inkSecondary,
                        padding: const EdgeInsets.symmetric(
                          horizontal: 8,
                          vertical: 12,
                        ),
                      ),
                    ),
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

/// 在紧凑单行区域显示登录身份与其认证服务商。
class _ProfileIdentity extends StatelessWidget {
  /// 使用可空字符串 [name] 显示账号标识，使用字符串 [provider] 显示认证服务商。
  /// 身份缺失时使用 provider 名称生成简短账号标签。
  const _ProfileIdentity({this.name, required this.provider});
  final String? name;
  final String provider;

  @override
  Widget build(BuildContext context) {
    final identityName = name == null || name!.isEmpty ? '$provider 账号' : name!;
    final initial = identityName.characters.first.toUpperCase();
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
                _wrappableIdentity(identityName),
                key: const ValueKey('profile-identity-name'),
                semanticsLabel: identityName,
                style: Theme.of(context).textTheme.titleMedium
                    ?.copyWith(fontSize: 16, fontWeight: FontWeight.w600),
              ),
              Text(
                provider,
                style: const TextStyle(
                  color: LyraColors.muted,
                  fontSize: 14,
                  height: 1.4,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }

  /// 为长身份插入不可见断行点，同时保留原始无障碍标签。
  ///
  /// [value] 是真实身份文本 `String`；返回可断行的显示文本，短身份保持原样，
  /// 以便在窄屏大字号下换行而不丢失可读内容。
  String _wrappableIdentity(String value) {
    final characters = value.characters.toList(growable: false);
    final containsCjk = RegExp(r'[\u3400-\u9fff]').hasMatch(value);
    if (characters.length <= (containsCjk ? 8 : 24)) return value;

    final chunkSize = containsCjk ? 3 : 6;
    final result = StringBuffer();
    var chunkLength = 0;
    for (var index = 0; index < characters.length; index++) {
      result.write(characters[index]);
      chunkLength++;
      final charactersAfter = characters.length - index - 1;
      if (charactersAfter > 0 &&
          chunkLength == chunkSize &&
          charactersAfter != 1) {
        // 均匀断点可让身份自然换行；合并单字符尾段，避免末行孤字。
        result.write('\u200B');
        chunkLength = 0;
      }
    }
    return result.toString();
  }
}
