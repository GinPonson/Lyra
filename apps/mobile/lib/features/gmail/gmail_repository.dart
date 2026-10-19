import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../auth/authorization_browser.dart';
import 'gmail_models.dart';

typedef GmailAuthorizationLauncher = Future<bool> Function(
  Uri url,
  CancelToken cancelToken,
);
typedef GmailDelay = Future<void> Function(Duration duration);

/// 通过当前 Lyra 登录客户端读取 Gmail 状态并执行授权、同步与断开操作。
class GmailRepository {
  GmailRepository(
    this._client, {
    GmailAuthorizationLauncher? authorizationLauncher,
    GmailDelay? delay,
    DateTime Function()? clock,
  }) : _authorizationLauncher = authorizationLauncher ?? authorizeInBrowser,
       _delay = delay ?? Future<void>.delayed,
       _clock = clock ?? DateTime.now;

  final LyraApiClient _client;
  final GmailAuthorizationLauncher _authorizationLauncher;
  final GmailDelay _delay;
  final DateTime Function() _clock;

  /// 读取当前用户的 Gmail 配置状态与公开连接列表。
  ///
  /// Returns: OAuth 是否已配置及连接列表。
  /// Throws: HTTP 或响应字段不符合服务端公开合同的异常。
  Future<GmailConnectionList> connections() async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/google/connections',
      const <String, String>{},
    );
    final json = _object(response.data, 'Gmail 连接列表响应无效');
    final configured = json['configured'];
    final rows = json['connections'];
    if (configured is! bool || rows is! List<Object?>) {
      throw const FormatException('Gmail 连接列表字段无效');
    }
    return GmailConnectionList(
      configured: configured,
      rows: List<GmailConnection>.unmodifiable(
        rows.map(GmailConnection.fromJson),
      ),
    );
  }

  /// 创建独立 Gmail OAuth 授权并在系统授权窗口关闭后等待绑定事务完成。
  ///
  /// [cancelToken] 在页面身份切换或离开时取消授权等待及后续轮询；邮箱连接仍由
  /// 当前设备和 Lyra 用户的 API 会话绑定。Returns: 完成授权的公开邮箱连接。
  /// Throws: 浏览器取消、事务过期、非法授权地址或服务端返回异常时抛出错误。
  Future<GmailConnection> authorize({required CancelToken cancelToken}) async {
    final created = await _client.execute<Map<String, Object?>>(
      (dio) => dio.post<Map<String, Object?>>(
        '/v1/google/authorization-transactions',
        data: const <String, Object?>{},
        cancelToken: cancelToken,
      ),
    );
    final transaction = _authorizationTransaction(created.data);
    if (!_clock().isBefore(transaction.expiresAt)) {
      throw const LyraApiError(
        code: 'GMAIL_AUTHORIZATION_EXPIRED',
        message: 'Gmail 授权已超时，请重新开始。',
        statusCode: 410,
        retryable: true,
      );
    }
    final opened = await _authorizationLauncher(
      transaction.authorizationUrl,
      cancelToken,
    );
    if (!opened) {
      throw const LyraApiError(
        code: 'GMAIL_AUTH_BROWSER_FAILED',
        message: '无法打开 Google 授权窗口，请重试。',
        statusCode: 0,
        retryable: true,
      );
    }
    return _waitForAuthorization(transaction, cancelToken);
  }

  /// 同步指定 Gmail 连接到用户已创建的证券账户。
  ///
  /// [connectionId] 是当前用户公开连接 UUID；[explicitAccountId] 是用户选中的现有
  /// 证券账户 UUID。首次扫描或重扫须明确选择全量，或传入自选本地 ``YYYY-MM-DD`` 日期；
  /// active 连接未传范围时走增量，显式传入范围时重新扫描所选范围。
  /// Returns: 与 [connectionId] 一致的公开连接 ID、去重后交易计数及连接状态。
  /// Throws: 首扫/重扫未选择范围、范围字段矛盾、错连接响应或服务器同步失败时抛出错误。
  Future<GmailSyncResult> sync({
    required String connectionId,
    required String explicitAccountId,
    required GmailConnectionStatus status,
    bool? scanAll,
    String? confirmedRescanAfterDate,
    required CancelToken cancelToken,
  }) async {
    if (!_isUuid(connectionId) || !_isUuid(explicitAccountId)) {
      throw const FormatException('Gmail 同步账户标识无效');
    }
    final body = <String, Object?>{'explicitAccountId': explicitAccountId};
    final hasExplicitRescanRange =
        status.requiresRescanRange ||
        scanAll != null ||
        confirmedRescanAfterDate != null;
    if (hasExplicitRescanRange) {
      if (scanAll == true) {
        if (confirmedRescanAfterDate != null) {
          throw const FormatException('Gmail 全量扫描不能同时指定起始日期');
        }
        body['scanAll'] = true;
      } else if (scanAll == false && _isLocalDate(confirmedRescanAfterDate)) {
        body['scanAll'] = false;
        body['confirmedRescanAfterDate'] = confirmedRescanAfterDate;
      } else {
        throw const LyraApiError(
          code: 'GMAIL_RESYNC_CONFIRMATION_REQUIRED',
          message: '请选择 Gmail 扫描范围后再同步。',
          statusCode: 409,
          retryable: false,
        );
      }
    } else if (status != GmailConnectionStatus.active) {
      throw const LyraApiError(
        code: 'GMAIL_CONNECTION_NOT_SYNCABLE',
        message: '此 Gmail 连接当前不可同步，请先重新授权或刷新状态。',
        statusCode: 409,
        retryable: false,
      );
    }
    final response = await _client.execute<Map<String, Object?>>(
      (dio) => dio.post<Map<String, Object?>>(
        '/v1/google/connections/${Uri.encodeComponent(connectionId)}/sync',
        data: body,
        cancelToken: cancelToken,
        // Gmail 扫描可能处理数百封邮件；只放宽本次 POST 的响应期限，不对写请求重放。
        options: Options(receiveTimeout: const Duration(minutes: 10)),
      ),
    );
    final result = GmailSyncResult.fromJson(response.data);
    if (result.connectionId != connectionId) {
      // A mismatched response could show counts from another user's mailbox;
      // treat it as a protocol error rather than rendering a success summary.
      throw const FormatException('Gmail 同步连接与请求不一致');
    }
    return result;
  }

  /// 撤销 Google 授权并断开当前用户的 Gmail 连接。
  ///
  /// [connectionId] 是当前用户公开连接 UUID；撤销通过请求体显式传 `revoke: true`。
  /// Returns: 服务端确认已断开的公开连接。
  /// Throws: 服务端拒绝撤销或返回无效连接时抛出错误。
  Future<GmailConnection> disconnect({
    required String connectionId,
    required CancelToken cancelToken,
  }) async {
    if (!_isUuid(connectionId)) {
      throw const FormatException('Gmail 连接标识无效');
    }
    final response = await _client.execute<Map<String, Object?>>(
      (dio) => dio.delete<Map<String, Object?>>(
        '/v1/google/connections/${Uri.encodeComponent(connectionId)}',
        data: const <String, Object?>{'revoke': true},
        cancelToken: cancelToken,
      ),
    );
    final json = _object(response.data, 'Gmail 断开响应无效');
    return GmailConnection.fromJson(json['connection']);
  }

  /// 在系统授权窗口返回后读取授权事务，直到完成或事务过期。
  Future<GmailConnection> _waitForAuthorization(
    _AuthorizationTransaction transaction,
    CancelToken cancelToken,
  ) async {
    final path =
        '/v1/google/authorization-transactions/'
        '${Uri.encodeComponent(transaction.authorizationId)}';
    while (_clock().isBefore(transaction.expiresAt)) {
      if (cancelToken.isCancelled) throw cancelToken.cancelError!;
      final response = await _client.execute<Map<String, Object?>>(
        (dio) => dio.get<Map<String, Object?>>(path, cancelToken: cancelToken),
      );
      final json = _object(response.data, 'Gmail 授权事务响应无效');
      if (response.statusCode == 200) {
        if (json['status'] != 'complete') {
          throw const FormatException('Gmail 授权完成状态无效');
        }
        return GmailConnection.fromJson(json['connection']);
      }
      if (response.statusCode != 202 || json['status'] != 'pending') {
        throw const FormatException('Gmail 授权事务状态无效');
      }
      await _delay(transaction.pollInterval);
    }
    throw const LyraApiError(
      code: 'GMAIL_AUTHORIZATION_EXPIRED',
      message: 'Gmail 授权已超时，请重新开始。',
      statusCode: 410,
      retryable: true,
    );
  }

  /// 校验并解析授权事务公开字段及 Google OAuth HTTPS 地址。
  _AuthorizationTransaction _authorizationTransaction(Object? value) {
    final json = _object(value, 'Gmail 授权事务响应无效');
    final authorizationId = json['authorizationId'];
    final authorizationUrl = json['authorizationUrl'];
    final expiresAt = json['expiresAt'];
    final pollSeconds = json['pollIntervalSeconds'];
    if (authorizationId is! String ||
        !_isUuid(authorizationId) ||
        authorizationUrl is! String ||
        expiresAt is! String ||
        pollSeconds is! int ||
        pollSeconds < 1 ||
        pollSeconds > 30) {
      throw const FormatException('Gmail 授权事务字段无效');
    }
    final parsedExpiration = DateTime.tryParse(expiresAt);
    if (parsedExpiration == null) {
      throw const FormatException('Gmail 授权过期时间无效');
    }
    final uri = Uri.tryParse(authorizationUrl);
    if (!_isGoogleAuthorizationUri(uri)) {
      throw const FormatException('Gmail 授权地址来源无效');
    }
    return _AuthorizationTransaction(
      authorizationId: authorizationId,
      authorizationUrl: uri!,
      expiresAt: parsedExpiration,
      pollInterval: Duration(seconds: pollSeconds),
    );
  }
}

/// 保存一次授权事务的严格校验结果。
class _AuthorizationTransaction {
  const _AuthorizationTransaction({
    required this.authorizationId,
    required this.authorizationUrl,
    required this.expiresAt,
    required this.pollInterval,
  });

  final String authorizationId;
  final Uri authorizationUrl;
  final DateTime expiresAt;
  final Duration pollInterval;
}

/// 将 JSON 对象收窄为可安全读取的字符串键映射。
Map<String, Object?> _object(Object? value, String message) {
  if (value is Map<String, Object?>) return value;
  throw FormatException(message);
}

/// 检查 Google OAuth 地址必须来自固定 HTTPS 授权端点且不携带凭据参数。
bool _isGoogleAuthorizationUri(Uri? uri) {
  if (uri == null ||
      uri.scheme != 'https' ||
      uri.host != 'accounts.google.com' ||
      uri.userInfo.isNotEmpty ||
      (uri.hasPort && uri.port != 443) ||
      uri.path != '/o/oauth2/v2/auth' ||
      uri.hasFragment) {
    return false;
  }
  const forbiddenParameters = {
    'code',
    'access_token',
    'refresh_token',
    'id_token',
    'token',
  };
  return !uri.queryParameters.keys.any(forbiddenParameters.contains);
}

/// 校验连接及账户标识使用 UUID 格式。
bool _isUuid(String value) => RegExp(
  r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
).hasMatch(value);

/// 校验用户显式选择的真实日历日期，拒绝越界日期和非 ISO 文本。
bool _isLocalDate(String? value) {
  if (value == null || !RegExp(r'^\d{4}-\d{2}-\d{2}$').hasMatch(value)) {
    return false;
  }
  final parsed = DateTime.tryParse('${value}T00:00:00Z');
  return parsed != null &&
      '${parsed.year.toString().padLeft(4, '0')}-'
              '${parsed.month.toString().padLeft(2, '0')}-'
              '${parsed.day.toString().padLeft(2, '0')}' ==
          value;
}
