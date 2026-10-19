import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import 'gmail_models.dart';
import 'gmail_repository.dart';

/// Gmail 数据请求绑定到当前已认证 Lyra API 客户端，登录切换会立即改换身份。
final gmailAuthenticatedClientProvider = Provider<LyraApiClient?>((ref) {
  final session = ref.watch(sessionProvider).value;
  return session is SessionAuthenticated
      ? ref.read(sessionProvider.notifier).client
      : null;
});

/// 为 Gmail 页面提供当前会话的数据仓储；测试可替换仓储而不触达网络。
final gmailRepositoryProvider = Provider<GmailRepository>((ref) {
  final client = ref.watch(gmailAuthenticatedClientProvider);
  if (client == null) {
    throw const LyraApiError(
      code: 'AUTH_REQUIRED',
      message: '请先登录 Lyra',
      statusCode: 401,
      retryable: false,
    );
  }
  return GmailRepository(client);
});

/// 读取当前用户 Gmail 连接；请求结束时重新核对客户端身份以丢弃迟到结果。
final gmailConnectionsProvider = FutureProvider.autoDispose
    .family<GmailConnectionList, LyraApiClient>((ref, client) async {
      if (!identical(client, ref.read(gmailAuthenticatedClientProvider))) {
        throw const LyraApiError(
          code: 'AUTH_SESSION_CHANGED',
          message: '登录状态已变化，请重新打开页面。',
          statusCode: 401,
          retryable: false,
        );
      }
      final rows = await ref.read(gmailRepositoryProvider).connections();
      if (!ref.mounted ||
          !identical(client, ref.read(gmailAuthenticatedClientProvider))) {
        throw const LyraApiError(
          code: 'AUTH_SESSION_CHANGED',
          message: '登录状态已变化，请重新打开页面。',
          statusCode: 401,
          retryable: false,
        );
      }
      return rows;
    }, retry: (retryCount, error) => null);
