import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

/// 导入账户选择器使用的最小账户公开视图；与 `/v1/ledger/accounts` 投影一致，
/// 不包含完整账户号码或服务端旧账户键。
class ImportAccountView {
  const ImportAccountView({
    required this.id,
    required this.displayName,
    required this.maskedSuffix,
    required this.status,
  });

  final String id;
  final String displayName;
  final String maskedSuffix;

  /// 服务端闭域状态：`active` 或 `archived`。
  final String status;

  bool get isActive => status == 'active';

  factory ImportAccountView.fromJson(Map<String, Object?> json) {
    final id = json['id'];
    final displayName = json['displayName'];
    final maskedSuffix = json['maskedSuffix'];
    final status = json['status'];
    if (id is! String ||
        displayName is! String ||
        maskedSuffix is! String ||
        status is! String) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '账户列表字段无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return ImportAccountView(
      id: id,
      displayName: displayName,
      maskedSuffix: maskedSuffix,
      status: status,
    );
  }
}

/// `GET /v1/ledger/accounts` 数据载荷中的账户行集合。
///
/// 服务端 `scope=all` 只返回活跃账户；客户端仍显式过滤归档行，
/// 不把归档账户作为导入目标候选（服务端领域规则同样拒绝归档目标）。
class ImportAccountsPage {
  const ImportAccountsPage({required this.rows});

  final List<ImportAccountView> rows;

  factory ImportAccountsPage.fromJson(Map<String, Object?> json) {
    final rows = json['rows'] as List<Object?>? ?? const <Object?>[];
    return ImportAccountsPage(
      rows: rows
          .map((row) => ImportAccountView.fromJson(row as Map<String, Object?>))
          .where((account) => account.isActive)
          .toList(growable: false),
    );
  }
}

/// 导入预览的公开统计；交易与指纹留在服务端，App 只看统计。
class ImportPreviewView {
  const ImportPreviewView({
    required this.importId,
    required this.confirmToken,
    required this.fileName,
    required this.accountId,
    required this.baseLedgerVersion,
    required this.totalRows,
    required this.insertableRows,
    required this.existingRows,
    required this.byAction,
  });

  final String importId;
  final String confirmToken;
  final String fileName;
  final String accountId;
  final int baseLedgerVersion;
  final int totalRows;
  final int insertableRows;
  final int existingRows;
  final Map<String, int> byAction;

  factory ImportPreviewView.fromJson(Map<String, Object?> json) {
    final preview = json['preview'] as Map<String, Object?>;
    final byActionRaw =
        preview['byAction'] as Map<String, Object?>? ?? const {};
    return ImportPreviewView(
      importId: json['importId'] as String,
      confirmToken: json['confirmToken'] as String,
      fileName: preview['fileName'] as String,
      accountId: preview['accountId'] as String,
      baseLedgerVersion: (preview['baseLedgerVersion'] as num).toInt(),
      totalRows: (preview['totalRows'] as num).toInt(),
      insertableRows: (preview['insertableRows'] as num).toInt(),
      existingRows: (preview['existingRows'] as num).toInt(),
      byAction: byActionRaw.map(
        (key, value) => MapEntry(key, (value as num).toInt()),
      ),
    );
  }
}

/// 提交结果：changed=false 表示全部重复，未产生新账本版本。
class ImportCommitResult {
  const ImportCommitResult({
    required this.changed,
    required this.ledgerVersion,
    required this.insertedRows,
    required this.skippedRows,
  });

  final bool changed;
  final int ledgerVersion;
  final int insertedRows;
  final int skippedRows;

  factory ImportCommitResult.fromJson(Map<String, Object?> json) {
    return ImportCommitResult(
      changed: json['changed'] == true,
      ledgerVersion: (json['ledgerVersion'] as num).toInt(),
      insertedRows: (json['insertedRows'] as num).toInt(),
      skippedRows: (json['skippedRows'] as num).toInt(),
    );
  }
}

/// 导入数据层：账户列表、上传预览（octet-stream + 仅展示用文件名头）与确认提交。
///
/// 走 [LyraApiClient] 复用协议头、令牌注入与 401 刷新重放；上传使用原始字节流，
/// 由客户端拦截器统一附加认证。
class ImportRepository {
  ImportRepository({required this._client});

  final LyraApiClient _client;

  /// 读取服务端公开账户列表；选择器只把活跃账户作为导入目标。
  Future<ImportAccountsPage> accounts() async {
    final response = await _client.get<Map<String, Object?>>(
      '/v1/ledger/accounts',
      const <String, String>{'scope': 'all'},
    );
    final body = response.data;
    final data = body?['data'];
    if (data is! Map<String, Object?>) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '账户列表响应格式无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return ImportAccountsPage.fromJson(data);
  }

  /// 上传文件字节并生成预览；请求体为原始字节流。
  ///
  /// [accountId] 为用户显式选择的账户；缺省时服务端只接受唯一精确旧键匹配。
  Future<ImportPreviewView> uploadPreview({
    required Uint8List bytes,
    required String fileName,
    String? accountId,
  }) async {
    try {
      final response = await _client.execute<dynamic>(
        (dio) => dio.post<dynamic>(
          '/v1/imports/preview',
          data: Stream<List<int>>.fromIterable(<List<int>>[bytes]),
          queryParameters: <String, String>{'accountId': ?accountId},
          options: Options(
            headers: <String, Object?>{
              'content-type': 'application/octet-stream',
              'x-lyra-file-name': fileName,
            },
          ),
        ),
      );
      final data = response.data;
      if (data is! Map) {
        throw const LyraApiError(
          code: 'CLIENT_REQUEST_FAILED',
          message: '导入预览响应格式无效',
          statusCode: 0,
          retryable: false,
        );
      }
      return ImportPreviewView.fromJson(data.cast<String, Object?>());
    } on DioException catch (error) {
      throw LyraApiError.fromResponseBody(
        error.response?.data,
        error.response?.statusCode ?? 0,
        requestId: error.response?.headers.value('x-request-id'),
      );
    }
  }

  /// 用一次性确认令牌提交导入；重复提交返回 409 IMPORT_PREVIEW_STALE。
  ///
  /// 携带设备生成的 [idempotencyKey]（16–200 位安全字符）时，服务端在
  /// 网络超时重试后按幂等记录重放持久化结果，不产生重复账本变更。
  Future<ImportCommitResult> commit({
    required String importId,
    required String confirmToken,
    String? idempotencyKey,
  }) async {
    final response = await _client.execute<Map<String, Object?>>(
      (dio) => dio.post<Map<String, Object?>>(
        '/v1/imports/$importId/commit',
        data: <String, Object?>{'confirmToken': confirmToken},
        options: idempotencyKey == null
            ? null
            : Options(
                headers: <String, Object?>{'idempotency-key': idempotencyKey},
              ),
      ),
    );
    final data = response.data;
    if (data is! Map<String, Object?>) {
      throw const LyraApiError(
        code: 'CLIENT_REQUEST_FAILED',
        message: '导入提交响应格式无效',
        statusCode: 0,
        retryable: false,
      );
    }
    return ImportCommitResult.fromJson(data);
  }
}
