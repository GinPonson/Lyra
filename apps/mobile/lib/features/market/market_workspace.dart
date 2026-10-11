import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../ledger/ledger_workspace.dart';
import 'market_models.dart';
import 'market_repository.dart';

typedef MarketQuery = ({String scope, String? underlying, int ledgerVersion});

/// Quotes have their own failure state and server-side cache. No polling and no
/// provider API keys in the App. Scope/session rules match the ledger reader.
final marketPositionsProvider = FutureProvider.autoDispose
    .family<MarketPositionsRead, MarketQuery>((ref, query) async {
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
          query.scope == ref.read(accountScopeProvider).id;
      if (!active()) throw StateError('账户范围或登录状态已变化');
      final read = await MarketRepository(client)
          .positions(scope: query.scope, underlying: query.underlying);
      if (!active()) throw StateError('账户范围或登录状态已变化');
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
