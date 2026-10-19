import 'package:flutter/foundation.dart';

/// Gmail 连接可以处于的服务端业务状态。
enum GmailConnectionStatus {
  pendingInitialScan('pending_initial_scan'),
  active('active'),
  needsResync('needs_resync'),
  disconnected('disconnected'),
  error('error');

  const GmailConnectionStatus(this.apiValue);

  final String apiValue;

  /// 按服务端公开枚举解析连接状态，拒绝未知状态以避免错误引导用户。
  factory GmailConnectionStatus.parse(Object? value) {
    if (value is String) {
      for (final status in GmailConnectionStatus.values) {
        if (status.apiValue == value) return status;
      }
    }
    throw const FormatException('Gmail 连接状态无效');
  }

  /// 返回当前状态对应的简短中文标签。
  String get label => switch (this) {
    GmailConnectionStatus.pendingInitialScan => '待首次扫描',
    GmailConnectionStatus.active => '已连接',
    GmailConnectionStatus.needsResync => '需要重扫',
    GmailConnectionStatus.disconnected => '已断开',
    GmailConnectionStatus.error => '连接异常',
  };

  /// 首次扫描及游标失效重扫都需要用户明确选择扫描范围。
  bool get requiresRescanRange =>
      this == GmailConnectionStatus.pendingInitialScan ||
      this == GmailConnectionStatus.needsResync;
}

/// 当前 Lyra 用户下可展示的 Gmail 邮箱连接列表。
@immutable
class GmailConnectionList {
  const GmailConnectionList({required this.configured, required this.rows});

  final bool configured;
  final List<GmailConnection> rows;
}

/// 单个 Google 邮箱的公开同步状态，不包含令牌或内部同步游标。
@immutable
class GmailConnection {
  const GmailConnection({
    required this.id,
    required this.googleEmail,
    required this.status,
    required this.lastErrorCode,
    required this.lastSyncAt,
    required this.createdAt,
    required this.updatedAt,
  });

  final String id;
  final String googleEmail;
  final GmailConnectionStatus status;
  final String? lastErrorCode;
  final DateTime? lastSyncAt;
  final DateTime createdAt;
  final DateTime updatedAt;

  /// 校验服务端公开连接字段，拒绝缺失字段及未知状态。
  factory GmailConnection.fromJson(Object? value) {
    final json = _object(value, 'Gmail 连接格式无效');
    final id = json['id'];
    final email = json['googleEmail'];
    final errorCode = json['lastErrorCode'];
    if (id is! String ||
        !_isUuid(id) ||
        email is! String ||
        email.trim().isEmpty ||
        (errorCode != null && errorCode is! String)) {
      throw const FormatException('Gmail 连接字段无效');
    }
    return GmailConnection(
      id: id,
      googleEmail: email,
      status: GmailConnectionStatus.parse(json['status']),
      lastErrorCode: errorCode as String?,
      lastSyncAt: _optionalTimestamp(json['lastSyncAt'], 'Gmail 同步时间无效'),
      createdAt: _timestamp(json['createdAt'], 'Gmail 创建时间无效'),
      updatedAt: _timestamp(json['updatedAt'], 'Gmail 更新时间无效'),
    );
  }
}

/// 一次 Gmail 同步的服务端结果，以账本交易计数呈现去重后的真实写入量。
@immutable
class GmailSyncResult {
  const GmailSyncResult({
    required this.connectionId,
    required this.mode,
    required this.truncated,
    required this.insertedTransactions,
    required this.existingTransactions,
    required this.conflictMessages,
    required this.replayOrderConflictMessages,
    required this.rejectedMessages,
    required this.connectionStatus,
    required this.lastSyncAt,
  });

  final String connectionId;
  final String mode;
  final bool truncated;
  final int insertedTransactions;
  final int existingTransactions;
  final int conflictMessages;
  final int replayOrderConflictMessages;
  final int rejectedMessages;
  final GmailConnectionStatus connectionStatus;
  final DateTime? lastSyncAt;

  /// 严格读取服务器同步摘要及公开连接 ID；不以邮件数推算新增交易数。
  factory GmailSyncResult.fromJson(Object? value) {
    final json = _object(value, 'Gmail 同步结果格式无效');
    final mode = json['mode'];
    final truncated = json['truncated'];
    final connection = _object(json['connection'], 'Gmail 同步连接结果无效');
    final connectionId = connection['id'];
    if ((mode != 'incremental' && mode != 'bounded_rescan') ||
        truncated is! bool ||
        connectionId is! String ||
        !_isUuid(connectionId)) {
      throw const FormatException('Gmail 同步结果字段无效');
    }
    final conflictMessages = _count(json['conflictMessages'], '账户冲突数量无效');
    final replayOrderConflictMessages = _count(
      json['replayOrderConflictMessages'],
      '同日成交顺序冲突数量无效',
    );
    if (replayOrderConflictMessages > conflictMessages) {
      throw const FormatException('同日成交顺序冲突数量超过总冲突数量');
    }
    return GmailSyncResult(
      connectionId: connectionId,
      mode: mode as String,
      truncated: truncated,
      insertedTransactions: _count(json['insertedTransactions'], '新增交易数量无效'),
      existingTransactions: _count(json['existingTransactions'], '已存在交易数量无效'),
      conflictMessages: conflictMessages,
      replayOrderConflictMessages: replayOrderConflictMessages,
      rejectedMessages: _count(json['rejectedMessages'], '解析失败数量无效'),
      connectionStatus: GmailConnectionStatus.parse(connection['status']),
      lastSyncAt: _optionalTimestamp(connection['lastSyncAt'], 'Gmail 同步时间无效'),
    );
  }
}

/// 将 JSON 对象收窄为可安全读取的字符串键映射。
Map<String, Object?> _object(Object? value, String message) {
  if (value is Map<String, Object?>) return value;
  throw FormatException(message);
}

/// 校验 OAuth 连接标识使用规范 UUID 字符串。
bool _isUuid(String value) => RegExp(
  r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
).hasMatch(value);

/// 解析必需的服务器时间戳。
DateTime _timestamp(Object? value, String message) {
  if (value is! String) throw FormatException(message);
  final parsed = DateTime.tryParse(value);
  if (parsed == null) throw FormatException(message);
  return parsed;
}

/// 解析可空的服务器时间戳。
DateTime? _optionalTimestamp(Object? value, String message) =>
    value == null ? null : _timestamp(value, message);

/// 读取非负整数计数，并拒绝浮点数、负数及超出安全范围的值。
int _count(Object? value, String message) {
  if (value is! int || value < 0 || value > 9007199254740991) {
    throw FormatException(message);
  }
  return value;
}
