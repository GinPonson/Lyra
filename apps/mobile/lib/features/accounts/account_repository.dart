import 'dart:convert';
import 'dart:math';

import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

/// 证券账户公开视图。完整券商账号及服务端账本映射不进入界面。
class BrokerageAccount {
  const BrokerageAccount({
    required this.id,
    required this.displayName,
    required this.maskedSuffix,
    required this.baseCurrency,
    required this.status,
  });

  final String id;
  final String displayName;
  final String maskedSuffix;
  final String baseCurrency;
  final String status;

  bool get isActive => status == 'active';

  factory BrokerageAccount.fromJson(Map<String, Object?> json) {
    final id = json['id'];
    final displayName = json['displayName'];
    final maskedSuffix = json['maskedSuffix'];
    final baseCurrency = json['baseCurrency'];
    final status = json['status'];
    if (id is! String ||
        id.isEmpty ||
        displayName is! String ||
        maskedSuffix is! String ||
        baseCurrency is! String ||
        !RegExp(r'^[A-Z]{3}$').hasMatch(baseCurrency) ||
        (status != 'active' && status != 'archived')) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '证券账户响应格式无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return BrokerageAccount(
      id: id,
      displayName: displayName,
      maskedSuffix: maskedSuffix,
      baseCurrency: baseCurrency,
      status: status as String,
    );
  }
}

/// 一次用户确认的创建请求；网络重试保留同一请求体和幂等键。
class AccountCreationRequest {
  AccountCreationRequest({
    required String displayName,
    required String accountIdentifier,
    String baseCurrency = 'USD',
  }) : displayName = displayName.trim(),
       accountIdentifier = accountIdentifier.trim().toUpperCase(),
       baseCurrency = baseCurrency.trim().toUpperCase(),
       idempotencyKey = _newIdempotencyKey();

  final String displayName;
  final String accountIdentifier;
  final String baseCurrency;
  final String idempotencyKey;

  bool matches({
    required String displayName,
    required String accountIdentifier,
    required String baseCurrency,
  }) =>
      this.displayName == displayName.trim() &&
      this.accountIdentifier == accountIdentifier.trim().toUpperCase() &&
      this.baseCurrency == baseCurrency.trim().toUpperCase();

  Map<String, Object?> get body => {
    'displayName': displayName,
    'accountIdentifier': accountIdentifier,
    'baseCurrency': baseCurrency,
  };

  static String _newIdempotencyKey() {
    final random = Random.secure();
    final suffix = base64UrlEncode(
      List<int>.generate(24, (_) => random.nextInt(256)),
    ).replaceAll('=', '');
    return 'account-$suffix';
  }
}

/// 文件名只作表单提示，创建仍需用户确认；不从交易金额推断账户。
String? accountIdentifierFromSchwabFile(String? fileName) {
  if (fileName == null) return null;
  final value = RegExp(
    r'Individual_([^_]+)_',
    caseSensitive: false,
  ).firstMatch(fileName)?.group(1)?.toUpperCase();
  return value != null && isValidAccountIdentifier(value) ? value : null;
}

bool isValidAccountIdentifier(String value) =>
    RegExp(r'^[A-Z0-9-]{1,29}[0-9]{3}$').hasMatch(value.trim().toUpperCase());

class AccountRepository {
  AccountRepository({required this._client});

  final LyraApiClient _client;

  Future<List<BrokerageAccount>> accounts() async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/ledger/accounts',
      const {'scope': 'all'},
    );
    final data = response.data?['data'];
    if (data is! Map<String, Object?> || data['rows'] is! List<Object?>) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '证券账户列表响应格式无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return (data['rows'] as List<Object?>)
        .map((row) => BrokerageAccount.fromJson(row as Map<String, Object?>))
        .where((account) => account.isActive)
        .toList(growable: false);
  }

  Future<BrokerageAccount> create(AccountCreationRequest request) async {
    final response = await _client.execute<Map<String, Object?>>(
      (dio) => dio.post<Map<String, Object?>>(
        '/v1/ledger/accounts',
        data: request.body,
        options: Options(headers: {'idempotency-key': request.idempotencyKey}),
      ),
    );
    final account = response.data?['account'];
    if (account is! Map<String, Object?>) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '创建证券账户响应格式无效，请重试确认结果',
        statusCode: 0,
        retryable: true,
      );
    }
    return BrokerageAccount.fromJson(account);
  }
}
