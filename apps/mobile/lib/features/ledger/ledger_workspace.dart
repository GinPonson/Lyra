import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import 'ledger_models.dart';
import 'ledger_repository.dart';

/// 跨账号、股票与交易页共享的显式账户范围；不改变既有 AI 会话范围。
class AccountScopeController extends Notifier<({String id, String label})> {
  @override
  ({String id, String label}) build() => (id: 'all', label: '全部账号');

  /// [id] 为服务端公开 UUID 或 all；[label] 为可见账户名称。
  void select(String id, String label) => state = (id: id, label: label);
}

final accountScopeProvider =
    NotifierProvider<AccountScopeController, ({String id, String label})>(
      AccountScopeController.new,
    );

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
      if (session is! SessionPaired) {
        throw const LyraApiError(
          code: 'DEVICE_NOT_PAIRED',
          message: '请先连接账户',
          statusCode: 0,
          retryable: false,
        );
      }
      final client = ref.read(sessionProvider.notifier).client;
      if (client == null) throw StateError('配对会话缺少 API 客户端');
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
  if (session is! SessionPaired) return null;
  if (scope.id == 'all' && session.summary != null) return session.summary;
  final client = ref.read(sessionProvider.notifier).client;
  if (client == null) throw StateError('配对会话缺少 API 客户端');
  return LedgerRepository(client).summary(scope: scope.id);
}, retry: (retryCount, error) => null);
