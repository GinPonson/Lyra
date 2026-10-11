import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../accounts/account_repository.dart';
import '../accounts/account_sheet.dart';
import '../accounts/account_workspace.dart';
import '../overview/login_panel.dart';
import 'profile_widgets.dart';

class BrokerageAccountsScreen extends ConsumerWidget {
  const BrokerageAccountsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    final signedIn = session.value is SessionAuthenticated;
    final accounts = signedIn ? ref.watch(brokerageAccountsProvider) : null;
    return ProfileSubpage(
      title: '证券账户',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text(
            '证券账户用于归集导入的交易记录，与你的 Lyra 登录身份分开。',
            style: TextStyle(color: LyraColors.muted, height: 1.7),
          ),
          const SizedBox(height: 20),
          if (session.isLoading)
            const LinearProgressIndicator(minHeight: 2)
          else if (!signedIn)
            const LoginPanel()
          else ...[
            accounts!.when(
              loading: () => const Padding(
                padding: EdgeInsets.symmetric(vertical: 24),
                child: LinearProgressIndicator(minHeight: 2),
              ),
              error: (error, _) => ProfileNotice(
                message: '暂时无法读取证券账户。${LyraApiError.from(error).message}',
                onRetry: () => ref.invalidate(brokerageAccountsProvider),
                retryLabel: '重新加载账户',
              ),
              data: (rows) => rows.isEmpty
                  ? const ProfileNotice(
                      message: '还没有证券账户。创建后即可导入券商文件，开始记录你的投资。',
                    )
                  : Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        for (final (index, account) in rows.indexed) ...[
                          if (index > 0) const Divider(height: 1, indent: 48),
                          _AccountRow(account: account),
                        ],
                      ],
                    ),
            ),
            const SizedBox(height: 24),
            FilledButton.icon(
              key: const ValueKey('profile-add-account'),
              onPressed: () => showCreateAccountSheet(context),
              icon: const Icon(LyraIcons.addAccount, size: 20),
              label: const Text('新增证券账户'),
            ),
          ],
        ],
      ),
    );
  }
}

class _AccountRow extends StatelessWidget {
  const _AccountRow({required this.account});

  final BrokerageAccount account;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 16),
    child: Row(
      children: [
        Container(
          width: 36,
          height: 36,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: LyraColors.surface,
            borderRadius: BorderRadius.circular(10),
          ),
          child: const LyraPrimaryIcon(
            LyraPrimaryGlyph.brokerage,
            color: LyraColors.inkSecondary,
            size: 21,
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                account.displayName,
                style: Theme.of(context).textTheme.titleMedium
                    ?.copyWith(fontWeight: FontWeight.w500),
              ),
              const SizedBox(height: 4),
              Text(
                'Schwab · 尾号 ${account.maskedSuffix} · ${account.baseCurrency}',
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(height: 1.6),
              ),
            ],
          ),
        ),
      ],
    ),
  );
}
