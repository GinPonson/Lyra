import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/features/accounts/account_repository.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';
import 'package:lyra_mobile/features/auth/user_profile.dart';
import 'package:lyra_mobile/features/import/import_repository.dart';
import 'package:lyra_mobile/features/ledger/ledger_models.dart';
import 'package:lyra_mobile/features/market/market_settings.dart';

/// 用**服务端真实产出的响应**回归客户端解析器（计划 §8）。
///
/// 两边各自手写样本的问题是：两份样本可以同时"自洽"却互相不符。这里读的是
/// `server/scripts/export_contract_samples.py` 从真实 `/v1/auth/{provider}/exchange`
/// 与 `GET /v1/me` 抓下来的响应，交给**生产解析器**解析。任一侧改了字段名或形状，
/// 这条测试立刻失败——这正是它存在的唯一理由。
///
/// 样本文件缺失时**明确失败**而不是跳过：跳过会让"没跑生成器"看起来像"协议一致"。
void main() {
  /// 共享样本的位置：仓库根的 `contracts/`，既不属于服务端也不属于客户端。
  final samplesFile = File('../../contracts/samples.json');

  Map<String, Object?> loadSample(String name) {
    if (!samplesFile.existsSync()) {
      fail(
        '缺少 ${samplesFile.path}。请先在 server/ 下运行 '
        '`uv run python scripts/export_contract_samples.py` 生成契约样本。',
      );
    }
    final document =
        jsonDecode(samplesFile.readAsStringSync()) as Map<String, Object?>;
    final cases = (document['cases'] as List<Object?>)
        .cast<Map<String, Object?>>();
    final match = cases.firstWhere(
      (item) => item['name'] == name,
      orElse: () => fail('契约样本里没有 "$name" 用例'),
    );
    // 创建类端点返回 201，读取类返回 200——两者都是成功，不要写死 200。
    expect(
      match['status'],
      anyOf(200, 201),
      reason: '$name 样本应当是成功响应',
    );
    return match['body']! as Map<String, Object?>;
  }

  test('登录响应的 user 对象可由生产解析器读取', () {
    final body = loadSample('login');

    final profile = LyraUserProfile.fromJson(body['user']! as Map<String, Object?>);

    expect(profile.status, LyraUserStatus.active);
    // 自动注册一律是 member；客户端不得自行提升角色。
    expect(profile.role, LyraUserRole.member);
    expect(profile.userId, isNotEmpty);
    expect(profile.identity.provider, AuthProvider.github);
    expect(profile.identity.subject, isNotEmpty);
  });

  test('登录响应同时带设备与令牌字段', () {
    final body = loadSample('login');

    for (final key in const [
      'deviceId',
      'displayName',
      'accessToken',
      'accessTokenExpiresAt',
      'refreshToken',
      'refreshTokenExpiresAt',
    ]) {
      expect(body.containsKey(key), isTrue, reason: '登录响应缺少 $key');
    }
  });

  test('/v1/me 的 user 对象与登录响应完全一致', () {
    final login = loadSample('login');
    final me = loadSample('me');

    // 契约 §3.9 要求两个接口返回**同一个** user 对象。解析器对两侧都必须成功，
    // 否则客户端"核对当前身份"的流程会因为形状差异直接报错。
    final fromLogin =
        LyraUserProfile.fromJson(login['user']! as Map<String, Object?>);
    final fromMe = LyraUserProfile.fromJson(me['user']! as Map<String, Object?>);

    expect(fromMe.userId, fromLogin.userId);
    expect(fromMe.displayName, fromLogin.displayName);
    expect(fromMe.status, fromLogin.status);
    expect(fromMe.role, fromLogin.role);
    expect(fromMe.identity.provider, fromLogin.identity.provider);
    expect(fromMe.identity.subject, fromLogin.identity.subject);
    expect(fromMe.identity.email, fromLogin.identity.email);
  });

  test('user 对象字段集是闭集——多一个字段就是协议变更', () {
    final user = loadSample('me')['user']! as Map<String, Object?>;
    final identity = user['identity']! as Map<String, Object?>;

    expect(user.keys.toSet(), {'userId', 'displayName', 'status', 'role', 'identity'});
    expect(identity.keys.toSet(), {'provider', 'subject', 'email'});
  });

  test('创建账户的响应可由生产解析器读取', () {
    final body = loadSample('createAccount');

    // 创建响应的形状与账本读取不同：**不带 `{metadata,data}` 信封**，直接是 `{account}`。
    final account = BrokerageAccount.fromJson(body['account']! as Map<String, Object?>);

    expect(account.id, isNotEmpty);
    expect(account.isActive, isTrue);
    // 完整券商账号不得出站，只暴露脱敏后缀。
    expect(account.maskedSuffix, isNotEmpty);
    expect(body['account'], isNot(contains('accountIdentifier')));
  });

  test('账户清单响应可由生产解析器读取', () {
    final data = loadSample('ledgerAccounts')['data']! as Map<String, Object?>;
    final rows = (data['rows']! as List<Object?>).cast<Map<String, Object?>>();

    expect(rows, isNotEmpty);
    expect(data['count'], rows.length);
    for (final row in rows) {
      BrokerageAccount.fromJson(row);
    }
  });

  test('账本汇总响应可由生产解析器读取', () {
    final body = loadSample('ledgerSummary');

    final summary = LedgerSummaryRead.fromJson(body);

    expect(summary.entries, isNotEmpty);
    expect(summary.metadata.ledgerVersion, isA<int>());
    // 金额是**定点十进制字符串**，解析器不接受 float。
    expect(summary.metadata.aggregateCurrency, hasLength(3));
  });

  test('导入预览响应可由生产解析器读取', () {
    final body = loadSample('importPreview');

    final preview = ImportPreviewView.fromJson(body);

    expect(preview.importId, isNotEmpty);
    expect(preview.confirmToken, isNotEmpty);
    expect(preview.baseLedgerVersion, isA<int>());
    expect(preview.totalRows, greaterThanOrEqualTo(preview.insertableRows));
  });

  test('行情设置响应可由生产解析器读取', () {
    final body = loadSample('marketSettings');

    final settings = MarketSettings.fromJson(body);

    // 设置按用户保存：新用户没有保存记录时 revision 为 0。
    expect(settings.revision, isA<int>());
    expect(settings.providers, isNotEmpty);
    expect(settings.routes.stock, isA<List<String>>());
  });
}
