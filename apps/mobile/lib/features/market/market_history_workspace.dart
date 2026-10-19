import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../ledger/ledger_workspace.dart';
import '../ledger/ledger_view.dart';
import 'market_history_models.dart';
import 'market_history_repository.dart';
import 'market_workspace.dart';

const _historyScopeChanged = LyraApiError(
  code: 'MARKET_HISTORY_SCOPE_CHANGED',
  message: '账户查看范围已变化，请重新打开行情走势。',
  statusCode: 409,
  retryable: false,
);
const _historyLedgerChanged = LyraApiError(
  code: 'MARKET_HISTORY_LEDGER_CHANGED',
  message: '持仓已变化，请刷新账本后重新查看行情走势。',
  statusCode: 409,
  retryable: true,
);
const _historySettingsChanged = LyraApiError(
  code: 'MARKET_HISTORY_SETTINGS_CHANGED',
  message: '行情来源已变化，请刷新行情走势。',
  statusCode: 409,
  retryable: true,
);
const _historySettingsPending = LyraApiError(
  code: 'MARKET_HISTORY_SETTINGS_PENDING',
  message: '行情来源正在切换，请稍后重试。',
  statusCode: 409,
  retryable: true,
);
const _historyRequestObsolete = LyraApiError(
  code: 'MARKET_HISTORY_REQUEST_OBSOLETE',
  message: '行情历史请求已失效，请重新打开行情走势。',
  statusCode: 409,
  retryable: false,
);

/// Loads history in a distinct read lane guarded by session, scope and revisions.
final marketHistoryProvider = FutureProvider.autoDispose
    .family<MarketHistoryRead, MarketHistoryQuery>((ref, query) async {
      final client = ref.watch(marketAuthenticatedClientProvider);
      final session = await ref.watch(sessionProvider.future);
      if (!ref.mounted) throw _historyRequestObsolete;
      if (session is! SessionAuthenticated || client == null) {
        throw const LyraApiError(
          code: 'AUTH_REQUIRED',
          message: '登录后查看行情走势',
          statusCode: 401,
          retryable: false,
        );
      }

      ref.watch(accountScopeProvider);
      await ref.read(accountScopeProvider.notifier).retryStartupRestore();
      if (!ref.mounted) throw _historyRequestObsolete;
      await ref.watch(accountScopeReadyProvider.future);
      if (!ref.mounted) throw _historyRequestObsolete;
      if (ref.read(accountScopeProvider).id != query.originScope) {
        throw _historyScopeChanged;
      }

      final pending = ref.watch(marketSettingsPendingProvider);
      if (pending != null) throw _historySettingsPending;
      // Watch the shared revision so a save or refresh cancels this read.
      ref.watch(marketSettingsRevisionProvider);
      // Read shared settings without subscribing to refresh cycles: this lane
      // reloads only when the settings revision changes or the user retries.
      final settings = await ref.read(marketSettingsProvider.future);
      if (!ref.mounted) throw _historyRequestObsolete;
      if (ref.read(marketSettingsPendingProvider) != null) {
        throw _historySettingsPending;
      }
      if (ref.read(marketSettingsRevisionProvider) != settings.revision) {
        throw _historySettingsChanged;
      }

      final ledgerQuery = (
        path: LedgerView.positions.path,
        parameters: '',
        scope: query.originScope,
      );
      final ledger = await ref.watch(ledgerReadProvider(ledgerQuery).future);
      if (!ref.mounted) throw _historyRequestObsolete;
      final ledgerVersion = (ledger.metadata['ledgerVersion'] as num).toInt();
      if (ledgerVersion != query.ledgerVersion) throw _historyLedgerChanged;

      bool active() {
        if (!ref.mounted) return false;
        final currentLedger = ref
            .read(ledgerReadProvider(ledgerQuery))
            .maybeWhen(data: (value) => value, orElse: () => null);
        return identical(client, ref.read(marketAuthenticatedClientProvider)) &&
            query.originScope == ref.read(accountScopeProvider).id &&
            settings.revision == ref.read(marketSettingsRevisionProvider) &&
            ref.read(marketSettingsPendingProvider) == null &&
            currentLedger != null &&
            (currentLedger.metadata['ledgerVersion'] as num).toInt() ==
                query.ledgerVersion;
      }

      if (!active()) throw _historySettingsChanged;
      final read = await MarketHistoryRepository(client)
          .history(query: query, settingsRevision: settings.revision);
      if (!ref.mounted) throw _historyRequestObsolete;
      if (!active()) {
        if (query.originScope != ref.read(accountScopeProvider).id) {
          throw _historyScopeChanged;
        }
        if (settings.revision != ref.read(marketSettingsRevisionProvider) ||
            ref.read(marketSettingsPendingProvider) != null) {
          throw _historySettingsChanged;
        }
        throw _historyLedgerChanged;
      }
      return read;
    }, retry: (retryCount, error) => null);
