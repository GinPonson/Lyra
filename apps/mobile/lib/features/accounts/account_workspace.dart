import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../ledger/ledger_workspace.dart';
import 'account_repository.dart';

/// 首页、范围选择器共用账户列表；不同登录身份不复用缓存。
final brokerageAccountsProvider =
    FutureProvider.autoDispose<List<BrokerageAccount>>((ref) async {
      final session = await ref.watch(sessionProvider.future);
      final client = ref.read(sessionProvider.notifier).client;
      if (session is! SessionAuthenticated || client == null) {
        throw const LyraApiError(
          code: 'AUTH_REQUIRED',
          message: '请先使用 Google 登录',
          statusCode: 401,
          retryable: false,
        );
      }
      return AccountRepository(client: client).accounts();
    }, retry: (retryCount, error) => null);

/// 金融写入刷新读模型，保留登录和显式账户范围；迟到结果不能影响另一身份。
Future<void> refreshLedgerWorkspace({
  required ProviderContainer container,
  required LyraApiClient client,
  BrokerageAccount? selectAccount,
}) async {
  final controller = container.read(sessionProvider.notifier);
  if (!identical(controller.client, client)) return;
  if (selectAccount != null) {
    container
        .read(accountScopeProvider.notifier)
        .select(selectAccount.id, selectAccount.displayName);
  }
  container.invalidate(brokerageAccountsProvider);
  container.invalidate(ledgerReadProvider);
  container.invalidate(scopedSummaryProvider);
  await controller.refreshLedger();
}
