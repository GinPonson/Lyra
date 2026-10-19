import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../ledger/ledger_view.dart';
import '../ledger/ledger_workspace.dart';
import 'market_underlying_quote_models.dart';
import 'market_underlying_quote_repository.dart';
import 'market_workspace.dart';

const _quoteScopeChanged = LyraApiError(
  code: 'MARKET_UNDERLYING_SCOPE_CHANGED',
  message: '账户查看范围已变化，请重新打开正股行情。',
  statusCode: 409,
  retryable: false,
);
const _quoteLedgerChanged = LyraApiError(
  code: 'MARKET_LEDGER_CHANGED',
  message: '持仓已变化，请刷新账本后重试正股行情。',
  statusCode: 409,
  retryable: true,
);
const _quoteSettingsChanged = LyraApiError(
  code: 'MARKET_SETTINGS_CHANGED',
  message: '行情来源已变化，请刷新正股行情。',
  statusCode: 409,
  retryable: true,
);
const _quoteSettingsPending = LyraApiError(
  code: 'MARKET_UNDERLYING_SETTINGS_PENDING',
  message: '行情来源正在切换，请稍后重试。',
  statusCode: 409,
  retryable: true,
);
const _quoteRequestObsolete = LyraApiError(
  code: 'MARKET_UNDERLYING_REQUEST_OBSOLETE',
  message: '登录或页面状态已变化，请重新打开正股行情。',
  statusCode: 409,
  retryable: false,
);

/// Loads one stock summary in a session-, scope- and revision-bound read lane.
final marketUnderlyingQuoteProvider = FutureProvider.autoDispose
    .family<MarketUnderlyingQuoteRead, MarketUnderlyingQuoteQuery>((
      ref,
      query,
    ) async {
      final client = ref.watch(marketAuthenticatedClientProvider);
      final session = await ref.watch(sessionProvider.future);
      if (!ref.mounted) throw _quoteRequestObsolete;
      if (session is! SessionAuthenticated || client == null) {
        throw const LyraApiError(
          code: 'AUTH_REQUIRED',
          message: '登录后查看正股行情',
          statusCode: 401,
          retryable: false,
        );
      }

      ref.watch(accountScopeProvider);
      await ref.read(accountScopeProvider.notifier).retryStartupRestore();
      if (!ref.mounted) throw _quoteRequestObsolete;
      await ref.watch(accountScopeReadyProvider.future);
      if (!ref.mounted) throw _quoteRequestObsolete;
      if (ref.read(accountScopeProvider).id != query.originScope) {
        throw _quoteScopeChanged;
      }

      if (ref.watch(marketSettingsPendingProvider) != null) {
        throw _quoteSettingsPending;
      }
      // Settings refreshes and saves invalidate this summary through revision.
      ref.watch(marketSettingsRevisionProvider);
      final settings = await ref.read(marketSettingsProvider.future);
      if (!ref.mounted) throw _quoteRequestObsolete;
      if (ref.read(marketSettingsPendingProvider) != null) {
        throw _quoteSettingsPending;
      }
      if (ref.read(marketSettingsRevisionProvider) != settings.revision) {
        throw _quoteSettingsChanged;
      }

      final ledgerQuery = (
        path: LedgerView.positions.path,
        parameters: '',
        scope: query.originScope,
      );
      final ledger = await ref.watch(ledgerReadProvider(ledgerQuery).future);
      if (!ref.mounted) throw _quoteRequestObsolete;
      final currentLedgerVersion = (ledger.metadata['ledgerVersion'] as num)
          .toInt();
      if (currentLedgerVersion != query.ledgerVersion) {
        throw _quoteLedgerChanged;
      }

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

      if (!active()) _throwInactive(ref, client, query, settings.revision);
      final read = await MarketUnderlyingQuoteRepository(client)
          .quote(query: query, settingsRevision: settings.revision);
      if (!ref.mounted) throw _quoteRequestObsolete;
      if (!active()) _throwInactive(ref, client, query, settings.revision);
      return read;
    }, retry: (retryCount, error) => null);

/// Reports which bound state changed after a summary request became inactive.
Never _throwInactive(
  Ref ref,
  Object client,
  MarketUnderlyingQuoteQuery query,
  int settingsRevision,
) {
  if (!ref.mounted) throw _quoteRequestObsolete;
  if (!identical(client, ref.read(marketAuthenticatedClientProvider))) {
    throw _quoteRequestObsolete;
  }
  if (query.originScope != ref.read(accountScopeProvider).id) {
    throw _quoteScopeChanged;
  }
  if (settingsRevision != ref.read(marketSettingsRevisionProvider) ||
      ref.read(marketSettingsPendingProvider) != null) {
    throw _quoteSettingsChanged;
  }
  throw _quoteLedgerChanged;
}
