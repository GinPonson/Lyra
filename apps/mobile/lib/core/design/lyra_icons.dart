import 'package:flutter/material.dart';

/// Material Icons Outlined 的业务语义映射。
///
/// 页面代码只引用业务名称，避免同一含义在不同页面使用不一致图标。
abstract final class LyraIcons {
  static const IconData overview = Icons.space_dashboard_outlined;
  static const IconData holdings = Icons.account_balance_wallet_outlined;
  static const IconData analytics = Icons.query_stats_outlined;
  static const IconData ledger = Icons.receipt_long_outlined;
  static const IconData agent = Icons.bubble_chart_outlined;
  static const IconData stocks = Icons.candlestick_chart_outlined;
  static const IconData search = Icons.search_outlined;
  static const IconData arrowUpRight = Icons.north_east_outlined;
  static const IconData arrowDownRight = Icons.south_east_outlined;
  static const IconData back = Icons.arrow_back_outlined;
  static const IconData filter = Icons.tune_outlined;
  static const IconData portfolio = Icons.pie_chart_outline;
  static const IconData error = Icons.error_outline;
  static const IconData expand = Icons.expand_more_outlined;

  static const IconData accountCenter = Icons.manage_accounts_outlined;
  static const IconData addAccount = Icons.add_card_outlined;
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
  static const IconData showAmounts = Icons.visibility_outlined;
  static const IconData hideAmounts = Icons.visibility_off_outlined;
  static const IconData profile = Icons.person_outline;
  static const IconData connection = Icons.dns_outlined;
  static const IconData about = Icons.info_outline;
  static const IconData openLink = Icons.open_in_new_outlined;
  static const IconData logout = Icons.logout_outlined;
  static const IconData profitColors = Icons.palette_outlined;
  static const IconData unselectedChoice = Icons.radio_button_unchecked;

  // 动作语义:发送走发送箭头,取消走关闭,不复用导航类图标。
  static const IconData send = Icons.arrow_upward;
  static const IconData dismiss = Icons.close;
  static const IconData confirm = Icons.check_outlined;
}
