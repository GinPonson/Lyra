import 'package:flutter/material.dart';

/// Material Icons Outlined 的业务语义映射。
///
/// 页面代码只引用业务名称，避免同一含义在不同页面使用不一致图标。
abstract final class LyraIcons {
  static const IconData overview = Icons.space_dashboard_outlined;
  static const IconData holdings = Icons.account_balance_wallet_outlined;
  static const IconData analytics = Icons.query_stats_outlined;
  static const IconData ledger = Icons.receipt_long_outlined;
  static const IconData agent = Icons.auto_awesome_outlined;

  static const IconData accountCenter = Icons.manage_accounts_outlined;
  static const IconData accountScope = Icons.layers_outlined;
  static const IconData ledgerVersion = Icons.storage_outlined;
  static const IconData dataCutoff = Icons.schedule_outlined;
  static const IconData sync = Icons.sync_outlined;
  static const IconData deposit = Icons.savings_outlined;
  static const IconData cost = Icons.stacked_line_chart_outlined;
  static const IconData cash = Icons.account_balance_outlined;
  static const IconData importFile = Icons.upload_file_outlined;
  static const IconData gmail = Icons.alternate_email_outlined;
  static const IconData security = Icons.shield_outlined;
  static const IconData disclosure = Icons.chevron_right;
}
