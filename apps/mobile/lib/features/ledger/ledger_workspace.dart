import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/preferences/app_preferences.dart';
import '../accounts/account_repository.dart' show BrokerageAccount;
import '../accounts/account_workspace.dart';
import 'ledger_models.dart';
import 'ledger_repository.dart';

/// 跨账号、股票与交易页共享的显式账户范围；不改变既有 AI 会话范围。
class AccountScopeController extends Notifier<({String id, String label})> {
  Completer<void>? _ready;
  String? _identity;
  int _generation = 0;
  int _manualRevision = 0;
  bool _restoreFailed = false;

  @override
  ({String id, String label}) build() {
    ref.watch(_scopeSessionIdentityProvider);
    final session = ref.read(sessionProvider).value;
    _identity = session is SessionAuthenticated
        ? preferencesIdentityKey(session.snapshot)
        : null;
    ++_generation;
    _manualRevision = 0;
    _restoreFailed = false;
    final previous = _ready;
    if (previous != null && !previous.isCompleted) {
      previous.completeError(StateError('登录状态已变化'));
    }
    _ready = Completer<void>()..future.ignore();
    ref.onDispose(() {
      ++_generation;
      final ready = _ready;
      if (ready != null && !ready.isCompleted) {
        ready.completeError(StateError('账户范围已关闭'));
      }
    });
    // A successful settings retry can unblock startup without rebuilding the
    // session or applying subsequent preference changes to the current scope.
    ref.listen(appPreferencesProvider, (_, next) {
      if (_restoreFailed &&
          next.hasValue &&
          !next.hasError &&
          !next.isLoading) {
        unawaited(retryStartupRestore().catchError((Object _) {}));
      }
    });
    if (_identity == null) {
      _ready!.complete();
    } else {
      unawaited(_restore(_generation, _manualRevision, _identity!));
    }
    return (id: 'all', label: '全部账号');
  }

  Future<void> get ready => _ready!.future;

  /// [id] 为服务端公开 UUID 或 all；[label] 为可见账户名称。
  /// 明确选择优先于尚未完成的启动恢复；只持久化该次调用捕获的登录身份。
  void select(String id, String label) {
    final replaceFailedReady = _restoreFailed;
    ++_manualRevision;
    _restoreFailed = false;
    if (replaceFailedReady) _ready = Completer<void>()..future.ignore();
    state = (id: id, label: label);
    if (!_ready!.isCompleted) _ready!.complete();
    // Selecting all after a failed startup can keep the same visible scope,
    // but the failed readiness future still needs to be replaced and observed.
    if (replaceFailedReady) ref.notifyListeners();
    final identity = _identity;
    if (identity != null) {
      unawaited(
        ref
            .read(appPreferencesProvider.notifier)
            .rememberAccountScope(identity, id)
            .catchError((Object _) {
              // The preferences controller retains AsyncError for retry. A local
              // settings failure must not undo an explicit, valid UI selection.
            }),
      );
    }
  }

  /// 查询/设置读取恢复后可重试；成功后的偏好修改只影响下一次启动。
  Future<void> retryStartupRestore() async {
    if (!_restoreFailed || _identity == null || _manualRevision > 0) return;
    _restoreFailed = false;
    _ready = Completer<void>()..future.ignore();
    ref.invalidate(brokerageAccountsProvider);
    // Notify this provider's listeners when the readiness future changes. A
    // notifier cannot invalidate a provider which already depends on itself.
    ref.notifyListeners();
    await _restore(_generation, _manualRevision, _identity!);
    await ready;
  }

  Future<void> _restore(int generation, int revision, String identity) async {
    bool active() =>
        ref.mounted &&
        generation == _generation &&
        revision == _manualRevision &&
        identity == _identity;
    try {
      final preferences = await ref.read(appPreferencesProvider.future);
      if (!active()) return;
      final saved = preferences.forIdentity(identity);
      final target = switch (preferences.startupAccountMode) {
        StartupAccountMode.all => 'all',
        StartupAccountMode.rememberLast => saved.lastScopeId,
        StartupAccountMode.specific => saved.specificAccountId ?? 'all',
      };
      var selected = (id: 'all', label: '全部账号');
      if (target != 'all') {
        final subscription = ref.listen(brokerageAccountsProvider, (_, _) {});
        final List<BrokerageAccount> accounts;
        try {
          accounts = await ref.read(brokerageAccountsProvider.future);
        } finally {
          subscription.close();
        }
        if (!active()) return;
        final matches = accounts.where(
          (account) => account.id == target && account.isActive,
        );
        if (matches.isNotEmpty) {
          final account = matches.first;
          selected = (id: account.id, label: account.displayName);
        }
      }
      if (!active()) return;
      state = selected;
      if (!_ready!.isCompleted) _ready!.complete();
    } on Object catch (error, stack) {
      if (!active()) return;
      _restoreFailed = true;
      if (!_ready!.isCompleted) _ready!.completeError(error, stack);
    }
  }
}

/// Token refresh and ledger refresh retain scope; a new login/device restores
/// from the stable user's preferences after revalidating account availability.
final _scopeSessionIdentityProvider = Provider<Object?>((ref) {
  final session = ref.watch(sessionProvider).value;
  return session is SessionAuthenticated
      ? (preferencesIdentityKey(session.snapshot), session.snapshot.deviceId)
      : null;
});

final accountScopeProvider =
    NotifierProvider<AccountScopeController, ({String id, String label})>(
      AccountScopeController.new,
    );

/// 金融查询在账户默认值及其有效性确认前等待，避免先显示全部账户的金额。
final accountScopeReadyProvider = FutureProvider<void>((ref) {
  ref.watch(_scopeSessionIdentityProvider);
  final controller = ref.read(accountScopeProvider.notifier);
  // A restored/manual selection completes this same future. Selecting only its
  // identity prevents that state change from canceling the in-flight wait.
  return ref.watch(accountScopeProvider.select((_) => controller.ready));
}, retry: (retryCount, error) => null);

/// 值类型查询键使同范围、同筛选的读请求共享缓存，切换范围不会串数据。
typedef LedgerQuery = ({String path, String parameters, String scope});

/// 只读响应保留服务端元数据与币种分组，不在客户端聚合金融金额。
class LedgerRead {
  LedgerRead.fromJson(Map<String, Object?> json)
    : metadata = json['metadata'] as Map<String, Object?>,
      data = json['data'] as Map<String, Object?> {
    _validateData(data);
  }

  /// 在异步请求边界校验列表及图形字段；畸形响应进入可重试错误态，不能在 build 中崩溃。
  static void _validateData(Map<String, Object?> data) {
    final groups = data['groups'];
    if (groups is List<Object?>) {
      for (final value in groups) {
        if (value is! Map<String, Object?> ||
            value['currency'] is! String ||
            value['data'] is! Map<String, Object?>) {
          throw const FormatException('账本币种分组格式无效');
        }
        _validateData(value['data'] as Map<String, Object?>);
      }
      return;
    }
    final rows = data['rows'];
    if (rows is! List<Object?>) throw const FormatException('账本缺少记录列表');
    for (final value in rows) {
      if (value is! Map<String, Object?>) {
        throw const FormatException('账本记录格式无效');
      }
      if (value.containsKey('trade') &&
          value['trade'] is! Map<String, Object?>) {
        throw const FormatException('平仓交易明细格式无效');
      }
      for (final field in ['netPnl', 'cumulativePnl', 'allocationPct']) {
        final amount = value[field];
        if (amount == null) continue;
        if (amount is! String ||
            !RegExp(r'^-?\d+(?:\.\d+)?$').hasMatch(amount)) {
          throw FormatException('账本字段 $field 必须为十进制字符串');
        }
        final coordinate = double.tryParse(amount);
        if (coordinate == null || !coordinate.isFinite) {
          throw FormatException('账本字段 $field 超出图形展示范围');
        }
      }
    }
  }

  final Map<String, Object?> metadata;
  final Map<String, Object?> data;

  /// 将按币种返回的结果展开为独立区块；非分组接口保持原样。
  List<({String? currency, Map<String, Object?> data})> get sections {
    if (!data.containsKey('groups') || data['groups'] is! List) {
      return [(currency: metadata['aggregateCurrency'] as String?, data: data)];
    }
    return (data['groups'] as List<Object?>)
        .map((value) {
          final group = value as Map<String, Object?>;
          return (
            currency: group['currency'] as String,
            data: group['data'] as Map<String, Object?>,
          );
        })
        .toList(growable: false);
  }

  /// 多币种明细使用 accountId 定位币种，禁止用首个币种标记全部记录。
  String? currencyFor(Map<String, Object?> row) {
    final groups = metadata['currencyGroups'] as List<Object?>?;
    if (groups != null) {
      for (final item in groups) {
        final group = item as Map<String, Object?>;
        if ((group['accountIds'] as List<Object?>).contains(row['accountId'])) {
          return group['currency'] as String;
        }
      }
    }
    return metadata['aggregateCurrency'] as String?;
  }
}

/// 请求失败直接成为 AsyncError；界面展示具体错误并提供重试。
final ledgerReadProvider = FutureProvider.autoDispose
    .family<LedgerRead, LedgerQuery>((ref, query) async {
      final session = await ref.watch(sessionProvider.future);
      if (session is! SessionAuthenticated) {
        throw const LyraApiError(
          code: 'DEVICE_NOT_PAIRED',
          message: '请先连接账户',
          statusCode: 0,
          retryable: false,
        );
      }
      final client = ref.read(sessionProvider.notifier).client;
      if (client == null) throw StateError('登录会话缺少 API 客户端');
      await ref.read(accountScopeProvider.notifier).retryStartupRestore();
      await ref.watch(accountScopeReadyProvider.future);
      if (!ref.mounted ||
          !identical(client, ref.read(sessionProvider.notifier).client)) {
        throw StateError('登录状态已变化');
      }
      if (query.scope != ref.read(accountScopeProvider).id) {
        throw StateError('账户范围已变化，请使用当前范围重新读取');
      }
      final response = await client.get<Map<String, Object?>>(
        query.path,
        <String, String>{
          ...Uri.splitQueryString(query.parameters),
          'scope': query.scope,
        },
      );
      final body = response.data;
      if (body == null) throw const FormatException('账本响应为空');
      return LedgerRead.fromJson(body);
    }, retry: (retryCount, error) => null);

/// 总览复用既有会话汇总；选择单账户时调用相同仓储并显式传入 UUID。
final scopedSummaryProvider = FutureProvider.autoDispose<LedgerSummaryRead?>((
  ref,
) async {
  final scope = ref.watch(accountScopeProvider);
  final session = await ref.watch(sessionProvider.future);
  if (session is! SessionAuthenticated) return null;
  await ref.read(accountScopeProvider.notifier).retryStartupRestore();
  await ref.watch(accountScopeReadyProvider.future);
  if (!ref.mounted || scope.id != ref.read(accountScopeProvider).id) {
    return null;
  }
  if (scope.id == 'all' && session.summary != null) return session.summary;
  final client = ref.read(sessionProvider.notifier).client;
  if (client == null) throw StateError('登录会话缺少 API 客户端');
  return LedgerRepository(client).summary(scope: scope.id);
}, retry: (retryCount, error) => null);
