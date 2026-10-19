import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_error_message.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_widgets.dart';
import '../../core/preferences/app_preferences.dart';
import 'market_settings.dart';
import 'market_widgets.dart' show marketTimeLabel;
import 'market_workspace.dart'
    show
        marketSettingsPendingProvider,
        marketSettingsProvider,
        marketSettingsRevisionProvider;
import 'market_underlying_quote_models.dart';
import 'market_underlying_quote_workspace.dart';

/// Shows a compact stock quote for a stock or option row's real underlying.
///
/// [position] remains the actual ledger row even when it is an option;
/// [ledgerVersion] and [originScope] bind the summary to the visible ledger.
/// [onOpenHistory] opens the separate underlying history experience.
class MarketUnderlyingQuotePreview extends ConsumerStatefulWidget {
  const MarketUnderlyingQuotePreview({
    super.key,
    required this.position,
    required this.ledgerVersion,
    required this.originScope,
    required this.onOpenHistory,
  });

  final Map<String, Object?> position;
  final int ledgerVersion;
  final String originScope;
  final VoidCallback onOpenHistory;

  @override
  ConsumerState<MarketUnderlyingQuotePreview> createState() =>
      _MarketUnderlyingQuotePreviewState();
}

class _MarketUnderlyingQuotePreviewState
    extends ConsumerState<MarketUnderlyingQuotePreview> {
  bool _refreshing = false;

  @override
  Widget build(BuildContext context) {
    final hidden = ref.watch(amountsHiddenProvider);
    final settings = ref.watch(marketSettingsProvider).asData?.value;
    final settingsPending = ref.watch(marketSettingsPendingProvider) != null;
    MarketUnderlyingQuoteQuery? query;
    String? identityError;
    try {
      query = MarketUnderlyingQuoteQuery.fromPosition(
        position: widget.position,
        ledgerVersion: widget.ledgerVersion,
        originScope: widget.originScope,
      );
    } on FormatException catch (error) {
      identityError = error.message;
    }

    final state = query == null
        ? null
        : ref.watch(marketUnderlyingQuoteProvider(query));
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          LayoutBuilder(
            builder: (context, constraints) {
              final textScale = MediaQuery.textScalerOf(context).scale(1);
              final title = const Padding(
                padding: EdgeInsets.symmetric(horizontal: 8),
                child: Text(
                  '正股',
                  style: TextStyle(
                    color: LyraColors.inkSecondary,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              );
              final actions = Wrap(
                crossAxisAlignment: WrapCrossAlignment.center,
                spacing: 2,
                children: [
                  TextButton.icon(
                    key: const ValueKey('market-underlying-history'),
                    onPressed: widget.onOpenHistory,
                    icon: const Icon(LyraIcons.analytics, size: 17),
                    label: const Text('正股走势'),
                    style: TextButton.styleFrom(
                      minimumSize: const Size(44, 40),
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      visualDensity: VisualDensity.compact,
                    ),
                  ),
                  IconButton(
                    key: const ValueKey('market-underlying-quote-refresh'),
                    tooltip: state?.isLoading == true || _refreshing
                        ? '正在刷新正股行情'
                        : '刷新正股行情',
                    onPressed:
                        query == null ||
                            settingsPending ||
                            state?.isLoading == true ||
                            _refreshing
                        ? null
                        : () => _refresh(query!),
                    icon: const Icon(LyraIcons.sync, size: 17),
                    style: IconButton.styleFrom(
                      minimumSize: const Size(40, 40),
                      padding: const EdgeInsets.all(8),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      visualDensity: VisualDensity.compact,
                    ),
                  ),
                ],
              );
              if (constraints.maxWidth < 224 * textScale) {
                return Wrap(
                  alignment: WrapAlignment.spaceBetween,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  runSpacing: 0,
                  children: [title, actions],
                );
              }
              return Row(children: [title, const Spacer(), actions]);
            },
          ),
          if (identityError != null)
            _QuoteMessage(
              title: identityError.contains('币种')
                  ? '持仓币种不可用，无法安全匹配正股行情。'
                  : '持仓缺少有效标识，无法读取正股行情。',
            )
          else if (state == null)
            const _QuoteMessage(title: '正股行情暂不可用。')
          else
            state.when(
              skipLoadingOnRefresh: false,
              loading: () => const _QuoteLoading(),
              error: (error, _) => _QuoteRequestError(error: error),
              data: (read) => read.status == 'unavailable'
                  ? _QuoteUnavailable(issue: read.issue!, settings: settings)
                  : hidden
                  ? const _QuoteMessage(title: '金额已隐藏')
                  : _AvailableQuote(read: read),
            ),
        ],
      ),
    );
  }

  /// Refreshes saved settings first, then explicitly invalidates a stable-revision quote.
  Future<void> _refresh(MarketUnderlyingQuoteQuery query) async {
    if (_refreshing || ref.read(marketSettingsPendingProvider) != null) return;
    setState(() => _refreshing = true);
    final previousRevision = ref.read(marketSettingsRevisionProvider);
    try {
      final settings = await ref.refresh(marketSettingsProvider.future);
      if (!mounted) return;
      if (ref.read(marketSettingsPendingProvider) != null) return;
      // A changed revision already rebuilds this read; a stable one needs a fresh summary.
      if (settings.revision == previousRevision) {
        ref.invalidate(marketUnderlyingQuoteProvider(query));
      }
    } on Object {
      if (mounted) ref.invalidate(marketUnderlyingQuoteProvider(query));
    } finally {
      if (mounted) setState(() => _refreshing = false);
    }
  }
}

/// Shows price precision, change and its actual observation time.
class _AvailableQuote extends StatelessWidget {
  const _AvailableQuote({required this.read});

  final MarketUnderlyingQuoteRead read;

  @override
  Widget build(BuildContext context) {
    final price = read.price!;
    final previousClose = read.previousClose;
    return Padding(
      padding: const EdgeInsets.fromLTRB(8, 2, 8, 0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: 10,
            runSpacing: 4,
            children: [
              LyraAmount(
                price.value,
                size: 22,
                exact: true,
                currency: read.currency,
              ),
              if (previousClose != null) ...[
                Tooltip(
                  message: '前收交易日 ${previousClose.sessionDate}',
                  child: const Text(
                    '较前收',
                    style: TextStyle(
                      color: LyraColors.inkSecondary,
                      fontSize: 12,
                    ),
                  ),
                ),
                LyraAmount(
                  read.changePercent!,
                  size: 14,
                  currency: '%',
                  colorBySign: true,
                  showPositiveSign: true,
                ),
              ] else
                Text(
                  _changeIssueLabel(read.changeIssue!),
                  style: const TextStyle(
                    color: LyraColors.inkSecondary,
                    fontSize: 12,
                  ),
                ),
            ],
          ),
          const SizedBox(height: 2),
          Wrap(
            spacing: 10,
            runSpacing: 2,
            children: [
              Text(
                '报价时间 ${marketTimeLabel(price.asOf)}',
                style: const TextStyle(color: LyraColors.muted, fontSize: 11),
              ),
              Text(
                '延迟至少 ${read.delayMinutes} 分钟',
                style: TextStyle(color: LyraColors.muted, fontSize: 11),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

/// Explains provider-side unavailability without turning it into a network error.
class _QuoteUnavailable extends StatelessWidget {
  const _QuoteUnavailable({required this.issue, required this.settings});

  final String issue;
  final MarketSettings? settings;

  @override
  Widget build(BuildContext context) => _QuoteMessage(
    title: _quoteIssueLabel(issue),
    detail: issue == 'not_configured' ? _notConfiguredDetail(settings) : null,
  );
}

/// Distinguishes a disabled route from one whose source is absent on the server.
String _notConfiguredDetail(MarketSettings? settings) {
  if (settings == null) return '服务端尚未配置正股行情来源。';
  final selected = settings.routes.stock;
  if (selected.isEmpty) {
    final selectable = settings.providers.any(
      (source) => source.configured && source.supports('stock'),
    );
    return selectable ? '尚未启用正股行情来源，请在行情设置中选择。' : '服务端尚未配置可用的正股行情来源。';
  }
  final needsSetup = selected.any((id) {
    final source = settings.source(id);
    return source == null || !source.configured || !source.supports('stock');
  });
  return needsSetup ? '当前正股行情来源尚未完成服务端配置。' : '请检查正股行情来源设置后重试。';
}

/// Converts stable quote issue codes into actionable short text.
String _quoteIssueLabel(String issue) => switch (issue) {
  'not_configured' => '正股行情来源未就绪',
  'summary_not_supported' => '所选来源暂不支持正股摘要',
  'unsupported_instrument' => '该正股标的不受支持',
  'currency_unsupported' => '该正股报价币种不受支持',
  'no_data' => '暂无可用正股报价',
  'rate_limited' => '正股行情请求受限，请稍后重试',
  'credentials_rejected' => '正股行情来源鉴权失败',
  'provider_unavailable' => '正股行情来源暂不可用',
  'invalid_response' => '正股行情来源返回的数据无效',
  'timeout' => '正股行情请求超时',
  _ => '正股行情不可用 · $issue',
};

/// Displays failures separately from valid provider-unavailable summaries.
class _QuoteRequestError extends StatelessWidget {
  const _QuoteRequestError({required this.error});

  final Object error;

  @override
  Widget build(BuildContext context) {
    final message = switch (error) {
      LyraApiError(code: 'MARKET_UNDERLYING_SCOPE_CHANGED') =>
        '账户查看范围已变化，请刷新账本后重试。',
      LyraApiError(code: 'MARKET_UNDERLYING_REQUEST_OBSOLETE') =>
        '登录状态已变化，请重新打开持仓。',
      LyraApiError(code: 'MARKET_LEDGER_CHANGED') => '持仓已变化，请刷新账本后重试。',
      LyraApiError(code: 'MARKET_SETTINGS_CHANGED') ||
      LyraApiError(
        code: 'MARKET_UNDERLYING_SETTINGS_PENDING',
      ) => '行情来源正在变化，请稍后刷新。',
      _ => lyraErrorMessage(error, fallback: '正股行情读取失败，请重试。'),
    };
    return _QuoteMessage(title: message);
  }
}

/// Keeps loading compact and lets the surrounding ledger retain its hierarchy.
class _QuoteLoading extends StatelessWidget {
  const _QuoteLoading();

  @override
  Widget build(BuildContext context) => const Padding(
    padding: EdgeInsets.fromLTRB(8, 8, 8, 4),
    child: SizedBox(
      width: 34,
      height: 2,
      child: LinearProgressIndicator(minHeight: 2),
    ),
  );
}

/// Shows concise status text and a manual retry affordance when needed.
class _QuoteMessage extends StatelessWidget {
  const _QuoteMessage({required this.title, this.detail});

  final String title;
  final String? detail;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(8, 4, 8, 4),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: const TextStyle(
                  color: LyraColors.inkSecondary,
                  fontSize: 12,
                ),
              ),
              if (detail != null)
                Text(
                  detail!,
                  style: const TextStyle(color: LyraColors.muted, fontSize: 11),
                ),
            ],
          ),
        ),
      ],
    ),
  );
}

/// Reports missing previous-close comparison without substituting a zero.
String _changeIssueLabel(String issue) => switch (issue) {
  'previous_close_unavailable' || 'no_previous_close' => '前收暂不可用',
  'change_unavailable' => '涨跌幅暂不可用',
  'rate_limited' => '涨跌幅读取受限，请稍后重试',
  'timeout' => '涨跌幅读取超时',
  'credentials_rejected' => '行情来源鉴权失败',
  'provider_unavailable' => '正股行情来源暂不可用',
  'invalid_response' => '前收数据异常',
  'no_data' => '暂无可比较的前收',
  'unsupported_instrument' => '该标的不支持涨跌比较',
  'currency_unsupported' => '该币种暂不支持涨跌比较',
  _ => '涨跌幅不可用 · $issue',
};
