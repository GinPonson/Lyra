import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';

/// 一个只在测试中使用的模拟 Gmail HTTP 适配器，不触达生产邮箱或服务器。
class GmailFixtureAdapter implements HttpClientAdapter {
  GmailFixtureAdapter(this.handle);

  final Future<ResponseBody> Function(RequestOptions options) handle;
  final List<RequestOptions> requests = <RequestOptions>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    return handle(options);
  }

  @override
  void close({bool force = false}) {}
}

/// 编码一条合成 JSON 响应及其 HTTP 状态码。
ResponseBody gmailJson(Object body, [int status = 200]) =>
    ResponseBody.fromString(
      jsonEncode(body),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

/// 返回一条公开 Gmail 连接的测试合同样本。
Map<String, Object?> gmailConnectionJson({
  String id = 'b3000000-0000-4000-8000-000000000001',
  String status = 'pending_initial_scan',
  String? lastErrorCode,
  String? lastSyncAt,
}) => {
  'id': id,
  'googleEmail': 'trades@example.com',
  'status': status,
  'lastErrorCode': lastErrorCode,
  'lastSyncAt': lastSyncAt,
  'createdAt': '2026-10-01T08:00:00Z',
  'updatedAt': '2026-10-01T08:00:00Z',
};
