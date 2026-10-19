import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_error_message.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_widgets.dart';
import '../../core/preferences/app_preferences.dart';
import '../accounts/account_workspace.dart';
import '../ledger/ledger_view.dart';
import '../ledger/ledger_workspace.dart';
import 'market_models.dart';
import 'market_settings_widgets.dart';
import 'market_workspace.dart';

/// Formats the ISO timestamp [value] as a UTC date and time.
/// [includeZone] may be false when the surrounding range labels UTC once.
/// Throws [FormatException] if [value] is not a valid timestamp.
String marketTimeLabel(String value, {bool includeZone = true}) {
  final time = DateTime.parse(value).toUtc();
  String two(int part) => part.toString().padLeft(2, '0');
  return '${time.year}-${two(time.month)}-${two(time.day)} '
      '${two(time.hour)}:${two(time.minute)}${includeZone ? ' UTC' : ''}';
}

String _timingLabel(MarketQuote? quote) => switch (quote?.timingKind) {
  'end_of_day' => '日终报价',
  'delayed' =>
    quote!.delayMinutes == null ? '延迟报价' : '延迟 ${quote.delayMinutes} 分钟',
  _ => '暂无报价',
};

String _basisLabel(String? basis) => switch (basis) {
  'mid' => '买卖中间价',
  'last' => '最近成交价',
  'close' => '区间收盘价',
  _ => '参考价',
};

/// A separate, optional read never turns a healthy ledger into an error page.
/// The ledger snapshot supplies the exact position identities to be displayed.
class MarketPositionsPanel extends ConsumerWidget {
  const MarketPositionsPanel({
    super.key,
    required this.ledger,
    this.underlying,
    this.initiallyExpanded = false,
    this.compact = false,
  });
  final LedgerRead ledger;
  final String? underlying;
  final bool initiallyExpanded;

  /// Embeds a concise status tile in the holdings card and reveals full details
  /// on demand without dropping quote coverage, timing, source, or error state.
  final bool compact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(accountScopeProvider);
    final query = (
      scope: scope.id,
      underlying: underlying == null || underlying!.isEmpty ? null : underlying,
      ledgerVersion: (ledger.metadata['ledgerVersion'] as num).toInt(),
    );
    final state = ref.watch(marketPositionsProvider(query));
    if (compact) {
      return _CompactMarketPositionsPanel(
        ledger: ledger,
        underlying: underlying,
        query: query,
        state: state,
        initiallyExpanded: initiallyExpanded,
      );
    }
    final details = _marketDetails(
      context,
      ref,
      ledger: ledger,
      underlying: underlying,
      query: query,
      state: state,
      initiallyExpanded: initiallyExpanded,
      compactDetails: false,
      settingsInCurrentView: true,
    );
    return LyraCard(
      key: const ValueKey('market-positions-panel'),
      padding: const EdgeInsets.fromLTRB(14, 8, 14, 14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  '参考估值',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                ),
              ),
              IconButton(
                tooltip: '刷新行情',
                onPressed: state.isLoading
                    ? null
                    : () => ref.invalidate(marketPositionsProvider(query)),
                icon: const Icon(LyraIcons.sync, size: 18),
              ),
            ],
          ),
          details,
        ],
      ),
    );
  }
}

class _CompactMarketPositionsPanel extends ConsumerWidget {
  const _CompactMarketPositionsPanel({
    required this.ledger,
    required this.underlying,
    required this.query,
    required this.state,
    required this.initiallyExpanded,
  });

  final LedgerRead ledger;
  final String? underlying;
  final MarketQuery query;
  final AsyncValue<MarketPositionsRead> state;
  final bool initiallyExpanded;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final status = state.when(
      skipLoadingOnRefresh: false,
      loading: () => '正在读取估值…',
      error: (error, _) => _marketErrorLabel(error),
      data: (read) {
        if (!read.enabled) return '未开启';
        if (read.count == 0) return '暂无持仓';
        final statuses = <String>[
          if (read.rows.any((row) => row.status == 'stale')) '含过期报价',
          if (read.pricedCount < read.count) '存在缺价',
        ];
        final status = statuses.isEmpty ? '报价可用' : statuses.join(' · ');
        return '${read.pricedCount} / ${read.count} 项有估值 · $status';
      },
    );
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: LyraCard(
        inset: true,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        child: Theme(
          data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
          child: ExpansionTile(
            key: PageStorageKey(('market-compact-details', query)),
            tilePadding: EdgeInsets.zero,
            minTileHeight: 56,
            dense: true,
            childrenPadding: const EdgeInsets.only(bottom: 12),
            initiallyExpanded: initiallyExpanded,
            title: LayoutBuilder(
              builder: (context, constraints) {
                final stacked =
                    constraints.maxWidth < 240 ||
                    MediaQuery.textScalerOf(context).scale(14) > 18;
                final label = const Text(
                  '参考估值',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                );
                final stateLabel = Text(
                  status,
                  textAlign: stacked ? TextAlign.left : TextAlign.right,
                  style: const TextStyle(fontSize: 12, color: LyraColors.muted),
                );
                return Row(
                  children: [
                    const Icon(
                      LyraIcons.analytics,
                      size: 24,
                      color: LyraColors.inkSecondary,
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: stacked
                          ? Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                label,
                                const SizedBox(height: 2),
                                stateLabel,
                              ],
                            )
                          : Row(
                              children: [
                                Expanded(child: label),
                                const SizedBox(width: 8),
                                Flexible(child: stateLabel),
                              ],
                            ),
                    ),
                  ],
                );
              },
            ),
            children: [
              Row(
                children: [
                  const Expanded(child: _Note('历史成本仍为权威账本金额。')),
                  IconButton(
                    tooltip: '刷新行情',
                    onPressed: state.isLoading
                        ? null
                        : () => ref.invalidate(marketPositionsProvider(query)),
                    icon: const Icon(LyraIcons.sync, size: 18),
                  ),
                ],
              ),
              _marketDetails(
                context,
                ref,
                ledger: ledger,
                underlying: underlying,
                query: query,
                state: state,
                initiallyExpanded: initiallyExpanded,
                compactDetails: true,
                settingsInCurrentView: false,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

String _marketErrorLabel(Object error) => switch (error) {
  LyraApiError(code: 'MARKET_LEDGER_CHANGED') => '持仓已更新，估值暂不可用',
  LyraApiError(code: 'MARKET_SETTINGS_CHANGED') => '行情来源已切换，请刷新',
  _ => lyraErrorMessage(error, fallback: '行情读取失败，请展开查看并重试。'),
};

/// Full read content is shared by the regular panel and the compact inset tile.
Widget _marketDetails(
  BuildContext context,
  WidgetRef ref, {
  required LedgerRead ledger,
  required String? underlying,
  required MarketQuery query,
  required AsyncValue<MarketPositionsRead> state,
  required bool initiallyExpanded,
  required bool compactDetails,
  required bool settingsInCurrentView,
}) => state.when(
  skipLoadingOnRefresh: false,
  loading: () => const Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      LinearProgressIndicator(minHeight: 2),
      SizedBox(height: 8),
      _Note('正在读取行情…'),
    ],
  ),
  error: (error, _) => _Note(switch (error) {
    LyraApiError(code: 'MARKET_LEDGER_CHANGED') => '持仓已更新，请刷新账本后查看参考估值。',
    LyraApiError(code: 'MARKET_SETTINGS_CHANGED') => '行情来源已切换，请刷新行情。',
    _ => lyraErrorMessage(error, fallback: '行情暂不可用，历史成本与账本记录仍可查看。可稍后刷新行情。'),
  }),
  data: (read) {
    if (!read.enabled) {
      return _Note(
        settingsInCurrentView
            ? '行情估值未开启：请确认服务端已配置行情源，再选择可用来源。'
            : '行情估值未开启：请确认服务端已配置行情源，再到“我的 → 行情与估值”选择来源。',
      );
    }
    final positions = <({Map<String, Object?> row, String? currency})>[];
    for (final section in ledger.sections) {
      for (final raw in section.data['rows'] as List<Object?>) {
        final row = raw as Map<String, Object?>;
        final symbol = row['underlying'] ?? row['symbolKey'];
        if (underlying != null && symbol != underlying) continue;
        positions.add((
          row: row,
          currency: section.currency ?? ledger.currencyFor(row),
        ));
      }
    }
    if (positions.isEmpty) return const _Note('暂无持仓，导入交易后可查看参考估值。');
    final aligned =
        positions.length == read.rows.length &&
        positions.every(
          (item) => read.match(item.row, currency: item.currency) != null,
        );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const _Note('按各持仓报价时点估算 · 未实现盈亏不含费用'),
        if (!aligned)
          const Padding(
            padding: EdgeInsets.only(top: 8),
            child: _Note('持仓已变化，估值合计暂不可用；请刷新账本后重试。'),
          ),
        if (read.rows.any((row) => row.status == 'stale'))
          const Padding(
            padding: EdgeInsets.only(top: 8),
            child: _Note('包含过期报价，请留意各项报价时间。'),
          ),
        if (aligned)
          for (final group in read.currencyGroups)
            _CurrencySummary(group: group),
        if (compactDetails)
          for (final item in positions)
            _PositionQuote(
              position: item.row,
              currency: item.currency,
              market: read.match(item.row, currency: item.currency),
            )
        else
          Theme(
            data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
            child: Material(
              color: Colors.transparent,
              child: ExpansionTile(
                key: PageStorageKey(('market-quote-details', query)),
                tilePadding: EdgeInsets.zero,
                childrenPadding: EdgeInsets.zero,
                initiallyExpanded: initiallyExpanded,
                title: const Text('查看参考报价', style: TextStyle(fontSize: 13)),
                subtitle: Text(
                  '${read.pricedCount} / ${read.count} 项持仓有估值',
                  style: const TextStyle(fontSize: 12, color: LyraColors.muted),
                ),
                children: [
                  for (final item in positions)
                    _PositionQuote(
                      position: item.row,
                      currency: item.currency,
                      market: read.match(item.row, currency: item.currency),
                    ),
                ],
              ),
            ),
          ),
      ],
    );
  },
);

class _CurrencySummary extends StatelessWidget {
  const _CurrencySummary({required this.group});
  final MarketCurrencyGroup group;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 14),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          group.currency,
          style: const TextStyle(fontSize: 12, color: LyraColors.inkSecondary),
        ),
        const SizedBox(height: 8),
        _Amounts(
          values: [
            (
              label: '参考市值',
              value: group.complete ? group.marketValue : null,
              profit: false,
              exact: false,
            ),
            (
              label: '未实现盈亏',
              value: group.complete ? group.unrealizedPnl : null,
              profit: true,
              exact: false,
            ),
          ],
        ),
        const SizedBox(height: 8),
        if (!group.complete)
          _Note(
            '${group.pricedPositions} / ${group.positions} 项有估值；报价缺失或过期，暂不展示合计。',
          ),
        if (group.asOfFrom != null && group.asOfTo != null)
          if (group.asOfFrom == group.asOfTo)
            _Note('报价截至 ${marketTimeLabel(group.asOfFrom!)}')
          else
            Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const _Note('报价时点不同 · UTC'),
                const SizedBox(height: 4),
                _Note(
                  '${marketTimeLabel(group.asOfFrom!, includeZone: false)} 至 '
                  '${marketTimeLabel(group.asOfTo!, includeZone: false)}',
                ),
              ],
            ),
      ],
    ),
  );
}

class _PositionQuote extends ConsumerWidget {
  const _PositionQuote({
    required this.position,
    required this.currency,
    required this.market,
  });
  final Map<String, Object?> position;
  final String? currency;
  final MarketPosition? market;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hidden = ref.watch(amountsHiddenProvider);
    final option = position['assetType'] == 'option';
    final accounts = ref.watch(brokerageAccountsProvider).asData?.value;
    final names = accounts?.where((value) => value.id == position['accountId']);
    final accountName = names != null && names.isNotEmpty
        ? names.first.displayName
        : null;
    final item = market;
    final quote = item?.quote;
    final reference = item?.referencePrice;
    final contract = option
        ? '${ledgerText(position['expiry'])} · '
              '${hidden ? '••••' : ledgerText(position['strike'])} ${ledgerText(position['optionType'])}'
        : '股票';
    return Container(
      key: ValueKey(('market-position', marketPositionIdentity(position))),
      padding: const EdgeInsets.symmetric(vertical: 12),
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: LyraColors.hairline)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            ledgerSecurityText(position, amountsHidden: hidden),
            style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w500),
          ),
          const SizedBox(height: 4),
          _Note(
            '$contract · ${ledgerText(position['direction'])}'
            '${accountName == null ? '' : ' · $accountName'}${currency == null ? '' : ' · $currency'}',
          ),
          const SizedBox(height: 10),
          if (reference == null || item?.status == 'unavailable')
            _Note(
              item == null
                  ? '当前估值响应未匹配到该持仓，保留历史成本。'
                  : '暂无可用报价${item.issue == null ? '' : ' · ${_marketIssueLabel(item.issue!)}'}，保留历史成本。',
            )
          else ...[
            _Amounts(
              values: [
                (
                  label: '参考价 · ${_basisLabel(item!.referenceBasis)}',
                  value: reference.value,
                  profit: false,
                  exact: true,
                ),
                (
                  label: '参考市值',
                  value: item.marketValue,
                  profit: false,
                  exact: false,
                ),
                (
                  label: '未实现盈亏（不含费用）',
                  value: item.unrealizedPnl,
                  profit: true,
                  exact: false,
                ),
              ],
            ),
            const SizedBox(height: 8),
            _Note(
              '${_timingLabel(quote)}${item.status == 'stale' ? ' · 已过期' : ''}'
              '${item.cached ? ' · 缓存报价' : ''}'
              '${item.issue == null ? '' : ' · ${_marketIssueLabel(item.issue!)}'}'
              '\n报价 ${marketTimeLabel(reference.asOf)}',
            ),
            if (quote != null) ...[
              const SizedBox(height: 6),
              _Note('来源 ${quote.providerId} · ${quote.feed}'),
              const SizedBox(height: 6),
              for (final field in ['bid', 'ask'])
                if (quote.prices[field] != null) ...[
                  _Amounts(
                    values: [
                      (
                        label: field == 'bid' ? '买价' : '卖价',
                        value: quote.prices[field]!.value,
                        profit: false,
                        exact: true,
                      ),
                    ],
                  ),
                  _Note('报价 ${marketTimeLabel(quote.prices[field]!.asOf)}'),
                  const SizedBox(height: 8),
                ],
              _Note('获取 ${marketTimeLabel(quote.fetchedAt)}'),
            ],
          ],
        ],
      ),
    );
  }
}

/// Known issue codes get concise Chinese labels; unknown server codes remain visible.
String _marketIssueLabel(String issue) => switch (issue) {
  'not_configured' => '未配置行情来源',
  'unsupported_instrument' => '该标的不受支持',
  'currency_unsupported' => '该报价币种不受支持',
  'no_data' => '未找到可用报价',
  'rate_limited' => '行情请求超出限额',
  'credentials_rejected' => '行情来源鉴权失败',
  'provider_unavailable' => '行情来源暂不可用',
  'invalid_response' => '行情响应无效',
  'timeout' => '行情请求超时',
  _ => issue,
};

/// Keeps quote unit prices exact while formatting valuation amounts as money.
/// Each value uses [LyraAmount] so visibility, tooltips and semantics stay shared.
class _Amounts extends StatelessWidget {
  const _Amounts({required this.values});
  final List<({String label, String? value, bool profit, bool exact})> values;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final wide =
          constraints.maxWidth >= 300 &&
          MediaQuery.textScalerOf(context).scale(14) <= 18;
      final width = wide
          ? (constraints.maxWidth - 16) / 2
          : constraints.maxWidth;
      return Wrap(
        spacing: 16,
        runSpacing: 12,
        children: [
          for (final value in values)
            SizedBox(
              width: width,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _Note(value.label),
                  const SizedBox(height: 4),
                  FittedBox(
                    fit: BoxFit.scaleDown,
                    alignment: Alignment.centerLeft,
                    child: LyraAmount(
                      value.value ?? '—',
                      size: 18,
                      colorBySign: value.profit,
                      showPositiveSign: value.profit,
                      exact: value.exact,
                    ),
                  ),
                ],
              ),
            ),
        ],
      );
    },
  );
}

class _Note extends StatelessWidget {
  const _Note(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Text(
    text,
    style: const TextStyle(
      fontSize: 12,
      height: 1.5,
      color: LyraColors.inkSecondary,
    ),
  );
}

/// Opens the market settings sheet while keeping its ledger read in the sheet session.
Future<void> showMarketDataSheet(BuildContext context) =>
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      backgroundColor: LyraColors.canvas,
      builder: (context) => ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * .85,
        ),
        child: const SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(14, 0, 14, 20),
          child: _MarketDataSheet(),
        ),
      ),
    );

/// Builds the market settings sheet and its separately loaded valuation group.
class _MarketDataSheet extends ConsumerWidget {
  const _MarketDataSheet();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final signedIn = ref.watch(sessionProvider).value is SessionAuthenticated;
    final scope = ref.watch(accountScopeProvider);
    final query = (
      path: LedgerView.positions.path,
      parameters: '',
      scope: scope.id,
    );
    final read = signedIn ? ref.watch(ledgerReadProvider(query)) : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const LyraSheetHeader(title: '行情与估值'),
        const MarketSettingsPanel(),
        const SizedBox(height: 16),
        if (!signedIn)
          const LyraCard(
            padding: EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  '参考估值',
                  style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                ),
                SizedBox(height: 8),
                _Note('登录后查看当前账户的行情状态。'),
              ],
            ),
          )
        else
          read!.when(
            loading: () => const LyraCard(
              padding: EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    '参考估值',
                    style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                  ),
                  SizedBox(height: 10),
                  LinearProgressIndicator(minHeight: 2),
                ],
              ),
            ),
            error: (error, _) => LyraCard(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    '参考估值',
                    style: TextStyle(fontSize: 14, fontWeight: FontWeight.w500),
                  ),
                  const SizedBox(height: 8),
                  _Note(lyraErrorMessage(error, fallback: '暂时无法读取持仓，请稍后重试。')),
                  TextButton(
                    onPressed: () => ref.invalidate(ledgerReadProvider(query)),
                    child: const Text('重试'),
                  ),
                ],
              ),
            ),
            data: (ledger) => MarketPositionsPanel(ledger: ledger),
          ),
      ],
    );
  }
}
