import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../ledger/ledger_workspace.dart';
import 'market_models.dart';
import 'market_repository.dart';
import 'market_settings.dart';

typedef MarketQuery = ({String scope, String? underlying, int ledgerVersion});

/// A different login or server produces a different client, even when the same
/// account name is used. Quote revisions and editor state follow that identity.
final marketAuthenticatedClientProvider = Provider<LyraApiClient?>((ref) {
  final session = ref.watch(sessionProvider).value;
  return session is SessionAuthenticated
      ? ref.read(sessionProvider.notifier).client
      : null;
});

class MarketSettingsRevisionController extends Notifier<int> {
  @override
  int build() {
    ref.watch(marketAuthenticatedClientProvider);
    return 0;
  }

  void observe(int revision) {
    if (revision > state) state = revision;
  }
}

/// Survives closing the settings sheet, but resets for a new authenticated
/// client. Every quote request observes it, including inactive account views.
final marketSettingsRevisionProvider =
    NotifierProvider<MarketSettingsRevisionController, int>(
      MarketSettingsRevisionController.new,
    );

class MarketSettingsPendingController extends Notifier<MarketRoutes?> {
  @override
  MarketRoutes? build() {
    ref.watch(marketAuthenticatedClientProvider);
    return null;
  }

  void begin(MarketRoutes routes) => state = routes;
  void finish() => state = null;
}

/// Shared with reopened sheets while an immediate change is still committing.
/// A different authenticated client resets it without affecting that client.
final marketSettingsPendingProvider =
    NotifierProvider<MarketSettingsPendingController, MarketRoutes?>(
      MarketSettingsPendingController.new,
    );

const _sessionChanged = LyraApiError(
  code: 'AUTH_SESSION_CHANGED',
  message: '登录状态或服务端已变化，请重新打开行情设置。',
  statusCode: 401,
  retryable: false,
);

class MarketSettingsController extends AsyncNotifier<MarketSettings> {
  int _generation = 0;
  bool _saving = false;

  @override
  Future<MarketSettings> build() async {
    final generation = ++_generation;
    _saving = false;
    final client = ref.watch(marketAuthenticatedClientProvider);
    if (client == null) {
      throw const LyraApiError(
        code: 'AUTH_REQUIRED',
        message: '登录后设置行情来源',
        statusCode: 401,
        retryable: false,
      );
    }
    final settings = await MarketRepository(client).settings();
    if (!_active(client, generation)) throw _sessionChanged;
    ref
        .read(marketSettingsRevisionProvider.notifier)
        .observe(settings.revision);
    return settings;
  }

  bool _active(LyraApiClient client, int generation) =>
      ref.mounted &&
      generation == _generation &&
      identical(client, ref.read(marketAuthenticatedClientProvider));

  Future<MarketSettings> save({
    required int expectedRevision,
    required MarketRoutes routes,
  }) async {
    final client = ref.read(marketAuthenticatedClientProvider);
    if (client == null) throw _sessionChanged;
    if (_saving) throw StateError('行情设置正在保存');
    final generation = _generation;
    // The sheet may be dismissed while the server is committing. Keep this
    // operation alive so a confirmed same-session save still refreshes quotes.
    final keepAlive = ref.keepAlive();
    _saving = true;
    ref.read(marketSettingsPendingProvider.notifier).begin(routes);
    try {
      final saved = await MarketRepository(client)
          .saveSettings(expectedRevision: expectedRevision, routes: routes);
      if (!_active(client, generation)) throw _sessionChanged;
      // Only a confirmed save changes effective UI state. Publishing the
      // revision invalidates pending reads before any saved-state repaint.
      ref.read(marketSettingsRevisionProvider.notifier).observe(saved.revision);
      ref.invalidate(marketPositionsProvider);
      state = AsyncData(saved);
      return saved;
    } finally {
      if (_active(client, generation)) {
        _saving = false;
        ref.read(marketSettingsPendingProvider.notifier).finish();
      }
      keepAlive.close();
    }
  }
}

final marketSettingsProvider =
    AsyncNotifierProvider.autoDispose<MarketSettingsController, MarketSettings>(
      MarketSettingsController.new,
      retry: (retryCount, error) => null,
    );

/// Quotes have their own failure state and server-side cache. No polling and no
/// provider API keys in the App. Scope/session rules match the ledger reader.
final marketPositionsProvider = FutureProvider.autoDispose
    .family<MarketPositionsRead, MarketQuery>((ref, query) async {
      final settingsRevision = ref.watch(marketSettingsRevisionProvider);
      final session = await ref.watch(sessionProvider.future);
      if (session is! SessionAuthenticated) {
        throw const LyraApiError(
          code: 'AUTH_REQUIRED',
          message: '登录后查看行情与估值',
          statusCode: 401,
          retryable: false,
        );
      }
      final client = ref.read(sessionProvider.notifier).client;
      if (client == null) throw StateError('登录会话缺少 API 客户端');
      await ref.read(accountScopeProvider.notifier).retryStartupRestore();
      await ref.watch(accountScopeReadyProvider.future);
      bool active() =>
          ref.mounted &&
          identical(client, ref.read(sessionProvider.notifier).client) &&
          settingsRevision == ref.read(marketSettingsRevisionProvider) &&
          query.scope == ref.read(accountScopeProvider).id;
      if (!active()) throw StateError('账户范围或登录状态已变化');
      final read = await MarketRepository(client)
          .positions(scope: query.scope, underlying: query.underlying);
      if (!active()) throw StateError('账户范围或登录状态已变化');
      if (read.settingsRevision < settingsRevision) {
        throw const LyraApiError(
          code: 'MARKET_SETTINGS_CHANGED',
          message: '行情来源已切换，请刷新行情',
          statusCode: 409,
          retryable: true,
        );
      }
      if (read.ledgerVersion != query.ledgerVersion) {
        throw const LyraApiError(
          code: 'MARKET_LEDGER_CHANGED',
          message: '持仓已更新，请刷新账本后查看估值',
          statusCode: 409,
          retryable: true,
        );
      }
      return read;
    }, retry: (retryCount, error) => null);
