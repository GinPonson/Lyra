import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_error_message.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_number.dart';
import '../../core/design/lyra_widgets.dart';
import '../../core/preferences/app_preferences.dart';
import '../ledger/ledger_view.dart';
import 'market_history_models.dart';
import 'market_history_workspace.dart';
import 'market_settings.dart';
import 'market_widgets.dart' show marketTimeLabel;
import 'market_workspace.dart'
    show
        marketSettingsPendingProvider,
        marketSettingsProvider,
        marketSettingsRevisionProvider;

/// Opens one position or underlying-stock history sheet from its real ledger row.
///
/// [position] remains the source holding even when [target] requests its
/// underlying stock. [ledgerVersion] and [originScope] bind the read to the
/// currently displayed ledger snapshot; [target] defaults to exact position.
Future<void> showMarketHistorySheet(
  BuildContext context, {
  required Map<String, Object?> position,
  required int ledgerVersion,
  required String originScope,
  MarketHistoryTarget target = MarketHistoryTarget.position,
}) => showModalBottomSheet<void>(
  context: context,
  isScrollControlled: true,
  useSafeArea: true,
  showDragHandle: true,
  backgroundColor: LyraColors.canvas,
  builder: (context) => ConstrainedBox(
    constraints: BoxConstraints(
      maxHeight: MediaQuery.sizeOf(context).height * .9,
    ),
    child: SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(14, 0, 14, 20),
      child: MarketHistorySheet(
        position: Map.unmodifiable(position),
        ledgerVersion: ledgerVersion,
        originScope: originScope,
        target: target,
      ),
    ),
  ),
);

/// Shows range controls and history states for the supplied source position.
class MarketHistorySheet extends ConsumerStatefulWidget {
  const MarketHistorySheet({
    super.key,
    required this.position,
    required this.ledgerVersion,
    required this.originScope,
    required this.target,
  });

  final Map<String, Object?> position;
  final int ledgerVersion;
  final String originScope;
  final MarketHistoryTarget target;

  @override
  ConsumerState<MarketHistorySheet> createState() => _MarketHistorySheetState();
}

class _MarketHistorySheetState extends ConsumerState<MarketHistorySheet> {
  MarketHistoryRange _range = MarketHistoryRange.month;
  bool _refreshing = false;

  @override
  Widget build(BuildContext context) {
    final hidden = ref.watch(amountsHiddenProvider);
    MarketHistoryQuery query;
    try {
      query = MarketHistoryQuery.fromPosition(
        position: widget.position,
        ledgerVersion: widget.ledgerVersion,
        originScope: widget.originScope,
        range: _range,
        target: widget.target,
      );
    } on FormatException catch (error) {
      return _sheetContent(
        context,
        hidden: hidden,
        query: null,
        state: null,
        identityError: error.message,
      );
    }
    return _sheetContent(
      context,
      hidden: hidden,
      query: query,
      state: ref.watch(marketHistoryProvider(query)),
      identityError: null,
    );
  }

  Widget _sheetContent(
    BuildContext context, {
    required bool hidden,
    required MarketHistoryQuery? query,
    required AsyncValue<MarketHistoryRead>? state,
    required String? identityError,
  }) {
    final position = widget.position;
    final settings = ref.watch(marketSettingsProvider).asData?.value;
    final settingsPending = ref.watch(marketSettingsPendingProvider) != null;

    Future<void> refreshHistory(MarketHistoryQuery? query) async {
      if (_refreshing ||
          ref.read(marketSettingsPendingProvider) != null ||
          query == null) {
        return;
      }
      setState(() => _refreshing = true);
      final previousRevision = ref.read(marketSettingsRevisionProvider);
      try {
        final refreshedSettings = await ref.refresh(
          marketSettingsProvider.future,
        );
        if (!mounted) return;
        if (ref.read(marketSettingsPendingProvider) != null) return;
        // A changed revision already invalidates history; a stable revision
        // needs an explicit bars refresh to fetch newly cached provider data.
        if (refreshedSettings.revision == previousRevision) {
          ref.invalidate(marketHistoryProvider(query));
        }
      } on Object {
        if (mounted) ref.invalidate(marketHistoryProvider(query));
        return;
      } finally {
        if (mounted) setState(() => _refreshing = false);
      }
    }

    void refresh() => unawaited(refreshHistory(query));

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        LyraSheetHeader(
          title: widget.target == MarketHistoryTarget.underlying
              ? '正股走势'
              : '行情走势',
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 0, 8, 14),
          child: _InstrumentHeading(
            position: position,
            hidden: hidden,
            target: widget.target,
          ),
        ),
        LyraCard(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  const Expanded(
                    child: Text(
                      '历史行情',
                      style: TextStyle(
                        fontSize: 15,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  ),
                  IconButton(
                    key: const ValueKey('market-history-refresh'),
                    tooltip: '刷新行情走势',
                    onPressed:
                        query == null ||
                            state?.isLoading == true ||
                            settingsPending ||
                            _refreshing
                        ? null
                        : refresh,
                    icon: const Icon(LyraIcons.sync, size: 18),
                  ),
                  const LyraAmountsButton(),
                ],
              ),
              const SizedBox(height: 4),
              Wrap(
                spacing: 8,
                runSpacing: 4,
                children: [
                  for (final range in MarketHistoryRange.values)
                    ChoiceChip(
                      key: ValueKey('market-history-range-${range.apiValue}'),
                      label: Text(range.label),
                      selected: _range == range,
                      onSelected: (selected) {
                        if (selected && range != _range) {
                          setState(() => _range = range);
                        }
                      },
                    ),
                ],
              ),
              const SizedBox(height: 14),
              if (identityError != null)
                _StatusMessage(
                  title: '无法读取该持仓的行情走势',
                  message: identityError.contains('币种')
                      ? '持仓币种不可用，无法安全匹配历史行情。'
                      : '持仓缺少完整标识，无法确认具体标的或期权合约。',
                )
              else if (state == null)
                const _StatusMessage(
                  title: '行情走势不可用',
                  message: '持仓标识无效，请返回账本后重试。',
                )
              else
                state.when(
                  skipLoadingOnRefresh: false,
                  loading: () => const _LoadingHistory(),
                  error: (error, _) =>
                      _HistoryRequestError(error: error, onRetry: refresh),
                  data: (read) => read.status == 'unavailable'
                      ? _UnavailableHistory(
                          issue: read.issue!,
                          assetType: read.assetType,
                          settings: settings,
                          onRetry: refresh,
                        )
                      : _AvailableHistory(
                          read: read,
                          hidden: hidden,
                          isOption: read.assetType == 'option',
                        ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

/// Presents the requested instrument without implying an underlying holding.
class _InstrumentHeading extends StatelessWidget {
  const _InstrumentHeading({
    required this.position,
    required this.hidden,
    required this.target,
  });

  final Map<String, Object?> position;
  final bool hidden;
  final MarketHistoryTarget target;

  @override
  Widget build(BuildContext context) {
    if (target == MarketHistoryTarget.underlying) {
      final rawTicker = position['underlying'] ?? position['symbolKey'];
      final ticker = rawTicker is String && rawTicker.trim().isNotEmpty
          ? rawTicker.trim().toUpperCase()
          : '—';
      return Text(
        '正股 · $ticker',
        style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500),
      );
    }
    if (position['assetType'] != 'option') {
      return Text(
        ledgerSecurityText(position, amountsHidden: hidden),
        style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500),
      );
    }
    final strike = position['strike'];
    final strikeWidget = hidden || strike is! String
        ? const Text('••••')
        : LyraAmount(strike, size: 15, exact: true);
    return Wrap(
      crossAxisAlignment: WrapCrossAlignment.center,
      spacing: 5,
      runSpacing: 2,
      children: [
        Text(ledgerSecurityText(position, amountsHidden: hidden)),
        Text('· ${ledgerText(position['expiry'])} ·'),
        strikeWidget,
        Text(ledgerText(position['optionType'])),
      ],
    );
  }
}

/// Shows the request-in-progress state without implying that a quote is live.
class _LoadingHistory extends StatelessWidget {
  const _LoadingHistory();

  @override
  Widget build(BuildContext context) => const Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      LinearProgressIndicator(minHeight: 2),
      SizedBox(height: 10),
      Text(
        '正在读取历史行情…',
        style: TextStyle(fontSize: 12, color: LyraColors.inkSecondary),
      ),
    ],
  );
}

/// Shows an explicit history issue and offers a user-triggered retry.
class _UnavailableHistory extends StatelessWidget {
  const _UnavailableHistory({
    required this.issue,
    required this.assetType,
    required this.settings,
    required this.onRetry,
  });

  final String issue;
  final String assetType;
  final MarketSettings? settings;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final message = switch (issue) {
      'not_configured' => _notConfiguredMessage(settings, assetType),
      'no_data' => '所选周期没有可用区间数据。',
      _ => '历史行情与当前持仓估值相互独立；此状态不会改变持仓价格或估值。',
    };
    return _StatusMessage(
      title: _historyIssueLabel(issue),
      message: message,
      onRetry: onRetry,
    );
  }
}

/// Distinguishes an empty route from a provider that still needs server setup.
String _notConfiguredMessage(MarketSettings? settings, String assetType) {
  if (settings == null) return '服务端尚未配置该资产类型的行情来源，或来源尚未启用。';
  final selected = settings.routes.forAsset(assetType);
  if (selected.isEmpty) {
    final selectable = settings.providers.any(
      (source) => source.configured && source.supports(assetType),
    );
    return selectable ? '尚未启用行情来源，请在行情设置中选择。' : '服务端尚未配置该资产类型可用的行情来源。';
  }
  final selectedSourceNeedsSetup = selected.any((id) {
    final source = settings.source(id);
    return source == null || !source.configured || !source.supports(assetType);
  });
  return selectedSourceNeedsSetup
      ? '当前选中的行情来源尚未完成服务端配置。'
      : '服务端尚未配置该资产类型的历史行情来源。';
}

/// Converts stable market issue codes into concise user-facing guidance.
String _historyIssueLabel(String issue) => switch (issue) {
  'not_configured' => '行情来源尚未配置或启用',
  'history_not_supported' => '所选来源暂不支持历史行情',
  'unsupported_instrument' => '该标的不受支持',
  'currency_unsupported' => '该报价币种不受支持',
  'no_data' => '暂无历史行情',
  'rate_limited' => '行情请求受限，请稍后重试',
  'credentials_rejected' => '行情来源鉴权失败',
  'provider_unavailable' => '行情来源暂不可用',
  'invalid_response' => '行情来源返回的数据无效',
  'timeout' => '行情请求超时',
  _ => '历史行情不可用 · $issue',
};

/// Presents request errors separately from valid provider-unavailable responses.
class _HistoryRequestError extends StatelessWidget {
  const _HistoryRequestError({required this.error, required this.onRetry});

  final Object error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final message = switch (error) {
      LyraApiError(code: 'MARKET_HISTORY_SCOPE_CHANGED') =>
        '账户查看范围已变化，请关闭并重新打开行情走势。',
      LyraApiError(code: 'MARKET_HISTORY_REQUEST_OBSOLETE') =>
        '登录或页面状态已变化，请关闭并重新打开行情走势。',
      LyraApiError(code: 'MARKET_HISTORY_LEDGER_CHANGED') ||
      LyraApiError(code: 'MARKET_LEDGER_CHANGED') => '持仓已变化，请刷新账本后重试。',
      LyraApiError(code: 'MARKET_HISTORY_SETTINGS_CHANGED') ||
      LyraApiError(code: 'MARKET_SETTINGS_CHANGED') ||
      LyraApiError(
        code: 'MARKET_HISTORY_SETTINGS_PENDING',
      ) => '行情来源正在变化，请稍后刷新。',
      _ => lyraErrorMessage(error, fallback: '行情走势读取失败，请检查网络后重试。'),
    };
    return _StatusMessage(
      title: '无法读取历史行情',
      message: message,
      onRetry: onRetry,
    );
  }
}

/// Shared compact status presentation for invalid, empty and failed reads.
class _StatusMessage extends StatelessWidget {
  const _StatusMessage({
    required this.title,
    required this.message,
    this.onRetry,
  });

  final String title;
  final String message;
  final VoidCallback? onRetry;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 4),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Icon(LyraIcons.dataCutoff, color: LyraColors.inkSecondary),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: const TextStyle(fontSize: 14)),
                  const SizedBox(height: 6),
                  Text(
                    message,
                    style: const TextStyle(
                      fontSize: 12,
                      height: 1.5,
                      color: LyraColors.inkSecondary,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
        if (onRetry != null) ...[
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(onPressed: onRetry, child: const Text('重试')),
          ),
        ],
      ],
    ),
  );
}

/// Renders the latest returned bar series while omitting all chart data when hidden.
class _AvailableHistory extends StatelessWidget {
  const _AvailableHistory({
    required this.read,
    required this.hidden,
    required this.isOption,
  });

  final MarketHistoryRead read;
  final bool hidden;
  final bool isOption;

  @override
  Widget build(BuildContext context) {
    if (hidden) {
      return Semantics(
        label: '金额已隐藏，行情走势图已收起',
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 30),
          child: Column(
            children: [
              Icon(LyraIcons.hideAmounts, color: LyraColors.muted, size: 22),
              SizedBox(height: 10),
              Text(
                '金额已隐藏',
                style: TextStyle(fontSize: 13, color: LyraColors.inkSecondary),
              ),
              SizedBox(height: 5),
              Text(
                '显示金额后查看行情走势',
                style: TextStyle(fontSize: 12, color: LyraColors.muted),
              ),
            ],
          ),
        ),
      );
    }
    final provider = _providerLabel(read.providerId);
    final timing = switch (read.timing.kind) {
      'delayed' => '延迟至少 ${read.timing.delayMinutes} 分钟',
      'end_of_day' => '日终历史行情',
      _ => '历史行情',
    };
    final treatment = isOption
        ? '原始合约行情'
        : read.adjustment == 'split'
        ? '拆股复权'
        : '未复权';
    final sourceLabel = [
      ?provider,
      timing,
      treatment,
      if (read.cached) '缓存数据',
      if (read.expired) '已到期合约历史行情',
    ].join(' · ');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          sourceLabel,
          style: const TextStyle(
            fontSize: 12,
            height: 1.5,
            color: LyraColors.inkSecondary,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          '数据区间截至 ${marketTimeLabel(read.cutoff)}',
          style: const TextStyle(fontSize: 11, color: LyraColors.muted),
        ),
        const SizedBox(height: 8),
        _MarketHistoryPlot(read: read),
        const SizedBox(height: 4),
        const Text(
          '每个点代表对应时间区间的开始，收盘价仅表示该区间记录。',
          style: TextStyle(fontSize: 11, height: 1.5, color: LyraColors.muted),
        ),
      ],
    );
  }
}

String? _providerLabel(String? providerId) => switch (providerId) {
  null => null,
  'alpaca' => 'Alpaca',
  _ => providerId,
};

/// Draws actual close bars on their timestamps and exposes exact selected values.
class _MarketHistoryPlot extends StatefulWidget {
  const _MarketHistoryPlot({required this.read});

  final MarketHistoryRead read;

  @override
  State<_MarketHistoryPlot> createState() => _MarketHistoryPlotState();
}

class _MarketHistoryPlotState extends State<_MarketHistoryPlot> {
  int? _selectedIndex;

  @override
  void didUpdateWidget(covariant _MarketHistoryPlot oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.read.range != widget.read.range ||
        !identical(oldWidget.read.bars, widget.read.bars)) {
      _selectedIndex = null;
    }
  }

  /// Selects the nearest actual bar by timestamp; missing intervals stay empty.
  void _select(double x, double width, double fontScale) {
    final bars = widget.read.bars;
    if (bars.length == 1) return;
    final left = _MarketHistoryPlotPainter.leftInset(fontScale);
    final right = math.max(left + 1, width - 8);
    final first = DateTime.parse(bars.first.timestamp).millisecondsSinceEpoch;
    final last = DateTime.parse(bars.last.timestamp).millisecondsSinceEpoch;
    if (last <= first) return;
    final ratio = ((x - left) / (right - left)).clamp(0.0, 1.0);
    final target = first + (last - first) * ratio;
    var nearestIndex = 0;
    var nearestDistance = double.infinity;
    for (final (index, bar) in bars.indexed) {
      final time = DateTime.parse(bar.timestamp).millisecondsSinceEpoch;
      final distance = (time - target).abs().toDouble();
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestIndex = index;
      }
    }
    if (nearestIndex != _selectedIndex) {
      setState(() => _selectedIndex = nearestIndex);
    }
  }

  @override
  Widget build(BuildContext context) {
    final bars = widget.read.bars;
    final selectedIndex = (_selectedIndex ?? bars.length - 1).clamp(
      0,
      bars.length - 1,
    );
    final selected = bars[selectedIndex];
    final textScale = MediaQuery.textScalerOf(context).scale(12) / 12;
    final selectedTime = _selectedBarTimeLabel(
      selected.timestamp,
      widget.read.range,
    );
    final closeLabel = switch (widget.read.range) {
      MarketHistoryRange.day => '15分钟收盘价',
      MarketHistoryRange.week => '每小时收盘价',
      MarketHistoryRange.month || MarketHistoryRange.quarter => '日收盘价',
    };
    final semanticLabel = bars.length == 1
        ? '单个历史行情区间，区间开始 $selectedTime，收盘价 ${selected.close} ${widget.read.currency}'
        : '历史行情折线，区间开始 $selectedTime，收盘价 ${selected.close} ${widget.read.currency}';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        LayoutBuilder(
          builder: (context, constraints) {
            final stacked = constraints.maxWidth / textScale < 330;
            final time = Text(
              '区间开始 $selectedTime',
              style: const TextStyle(
                fontSize: 12,
                height: 1.4,
                color: LyraColors.inkSecondary,
              ),
            );
            final close = Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  '$closeLabel ',
                  style: TextStyle(
                    fontSize: 12,
                    color: LyraColors.inkSecondary,
                  ),
                ),
                Flexible(
                  child: LyraAmount(
                    selected.close,
                    size: 15,
                    currency: widget.read.currency,
                    exact: true,
                  ),
                ),
              ],
            );
            return stacked
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [time, const SizedBox(height: 4), close],
                  )
                : Row(
                    children: [
                      Expanded(child: time),
                      const SizedBox(width: 8),
                      close,
                    ],
                  );
          },
        ),
        const SizedBox(height: 8),
        Semantics(
          label: semanticLabel,
          child: ExcludeSemantics(
            child: LayoutBuilder(
              builder: (context, constraints) => MouseRegion(
                cursor: SystemMouseCursors.precise,
                onHover: (event) => _select(
                  event.localPosition.dx,
                  constraints.maxWidth,
                  textScale,
                ),
                child: GestureDetector(
                  onTapDown: (details) => _select(
                    details.localPosition.dx,
                    constraints.maxWidth,
                    textScale,
                  ),
                  onHorizontalDragUpdate: (details) => _select(
                    details.localPosition.dx,
                    constraints.maxWidth,
                    textScale,
                  ),
                  child: SizedBox(
                    key: const ValueKey('market-history-plot'),
                    height: 214,
                    width: double.infinity,
                    child: CustomPaint(
                      painter: _MarketHistoryPlotPainter(
                        bars: bars,
                        range: widget.read.range,
                        selectedIndex: selectedIndex,
                        fontScale: textScale,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Uses a date-only label for daily bars and a full UTC start time otherwise.
String _selectedBarTimeLabel(String value, MarketHistoryRange range) {
  final time = DateTime.parse(value).toUtc();
  if (range == MarketHistoryRange.month ||
      range == MarketHistoryRange.quarter) {
    String two(int part) => part.toString().padLeft(2, '0');
    return '${time.year}-${two(time.month)}-${two(time.day)} UTC';
  }
  return marketTimeLabel(value);
}

/// Paints a close-price line without inserting zeros or synthetic intervals.
class _MarketHistoryPlotPainter extends CustomPainter {
  const _MarketHistoryPlotPainter({
    required this.bars,
    required this.range,
    required this.selectedIndex,
    required this.fontScale,
  });

  final List<MarketHistoryBar> bars;
  final MarketHistoryRange range;
  final int selectedIndex;
  final double fontScale;

  static double leftInset(double scale) =>
      56 * scale.clamp(1.0, 1.8).toDouble();

  @override
  void paint(Canvas canvas, Size size) {
    if (bars.isEmpty) return;
    final scale = fontScale.clamp(1.0, 1.8).toDouble();
    final left = leftInset(scale);
    final right = math.max(left + 1, size.width - 8);
    final top = 10.0;
    final bottom = size.height - 27 * scale;
    if (right <= left || bottom <= top) return;

    final values = bars.map((bar) => double.parse(bar.close)).toList();
    final minValue = values.reduce(math.min);
    final maxValue = values.reduce(math.max);
    final rawSpan = maxValue - minValue;
    final pad = rawSpan == 0
        ? math.max(maxValue.abs() * .015, .01)
        : rawSpan * .12;
    final low = math.max(0.0, minValue - pad);
    final high = math.max(low + .01, maxValue + pad);
    final rangeValue = high - low;
    final firstTime = DateTime.parse(bars.first.timestamp)
        .millisecondsSinceEpoch;
    final lastTime = DateTime.parse(bars.last.timestamp).millisecondsSinceEpoch;
    double xFor(int index) {
      if (lastTime <= firstTime) return (left + right) / 2;
      final time = DateTime.parse(bars[index].timestamp).millisecondsSinceEpoch;
      return left +
          (time - firstTime) / (lastTime - firstTime) * (right - left);
    }

    double yFor(double value) =>
        bottom - (value - low) / rangeValue * (bottom - top);

    final gridPaint = Paint()
      ..color = LyraColors.hairlineStrong
      ..strokeWidth = 1;
    final textStyle = TextStyle(
      fontFamily: 'Geist Mono',
      fontSize: 10 * scale,
      height: 1,
      color: LyraColors.inkSecondary,
    );
    final decimals = rangeValue < 1 ? 3 : 2;
    for (var tick = 0; tick < 3; tick++) {
      final fraction = tick / 2;
      final y = bottom - fraction * (bottom - top);
      canvas.drawLine(Offset(left, y), Offset(right, y), gridPaint);
      final value = low + rangeValue * fraction;
      _drawLabel(
        canvas,
        size,
        LyraNumber.amount(value.toStringAsFixed(decimals), decimals: decimals),
        Offset(left - 7, y - 5 * scale),
        style: textStyle,
        alignRight: true,
        maxWidth: left - 10,
      );
    }

    final linePaint = Paint()
      ..color = LyraColors.accent
      ..strokeWidth = 2.2
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    if (bars.length > 1) {
      final path = Path()..moveTo(xFor(0), yFor(values.first));
      for (var index = 1; index < bars.length; index++) {
        path.lineTo(xFor(index), yFor(values[index]));
      }
      canvas.drawPath(path, linePaint);
    }

    final selected = selectedIndex.clamp(0, bars.length - 1);
    final selectedPoint = Offset(xFor(selected), yFor(values[selected]));
    canvas.drawLine(
      Offset(selectedPoint.dx, top),
      Offset(selectedPoint.dx, bottom),
      Paint()
        ..color = LyraColors.accent.withValues(alpha: .28)
        ..strokeWidth = 1,
    );
    canvas.drawCircle(selectedPoint, 4.5, Paint()..color = LyraColors.accent);
    canvas.drawCircle(
      selectedPoint,
      6.5,
      Paint()
        ..color = LyraColors.accent.withValues(alpha: .2)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2,
    );

    if (bars.length == 1) {
      _drawLabel(
        canvas,
        size,
        _axisTime(bars.single.timestamp, range),
        Offset(left, bottom + 8 * scale),
        style: textStyle,
        maxWidth: right - left,
      );
    } else {
      _drawLabel(
        canvas,
        size,
        _axisTime(bars.first.timestamp, range),
        Offset(left, bottom + 8 * scale),
        style: textStyle,
        maxWidth: (right - left) / 2,
      );
      _drawLabel(
        canvas,
        size,
        _axisTime(bars.last.timestamp, range),
        Offset(right, bottom + 8 * scale),
        style: textStyle,
        maxWidth: (right - left) / 2,
        alignRight: true,
      );
    }
  }

  /// Formats a sparse x-axis label while the selected point keeps full UTC time.
  String _axisTime(String value, MarketHistoryRange range) {
    final time = DateTime.parse(value).toUtc();
    String two(int part) => part.toString().padLeft(2, '0');
    return range == MarketHistoryRange.day
        ? '${two(time.hour)}:${two(time.minute)}'
        : '${two(time.month)}/${two(time.day)}';
  }

  /// Constrains one axis label to the plot size to avoid overflow at large text.
  void _drawLabel(
    Canvas canvas,
    Size size,
    String text,
    Offset anchor, {
    required TextStyle style,
    required double maxWidth,
    bool alignRight = false,
  }) {
    final painter = TextPainter(
      text: TextSpan(text: text, style: style),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    )..layout(maxWidth: math.max(0.0, maxWidth));
    final x = alignRight ? anchor.dx - painter.width : anchor.dx;
    painter.paint(
      canvas,
      Offset(
        x.clamp(0.0, math.max(0.0, size.width - painter.width)).toDouble(),
        anchor.dy,
      ),
    );
    painter.dispose();
  }

  @override
  bool shouldRepaint(covariant _MarketHistoryPlotPainter oldDelegate) =>
      oldDelegate.range != range ||
      oldDelegate.selectedIndex != selectedIndex ||
      oldDelegate.fontScale != fontScale ||
      !listEquals(oldDelegate.bars, bars);
}
