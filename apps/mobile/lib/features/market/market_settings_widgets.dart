import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_error_message.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_widgets.dart';
import 'market_settings.dart';
import 'market_workspace.dart';

/// Shows server-backed market routes without depending on a ledger read.
class MarketSettingsPanel extends ConsumerWidget {
  const MarketSettingsPanel({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    final client = ref.watch(marketAuthenticatedClientProvider);
    final settings = client == null ? null : ref.watch(marketSettingsProvider);
    final settingsPending = ref.watch(marketSettingsPendingProvider);
    final canRefresh =
        client != null &&
        !session.isLoading &&
        settings?.isLoading != true &&
        settingsPending == null;
    return Column(
      key: const ValueKey('market-source-settings'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            const Expanded(
              child: Text(
                '行情来源',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w500),
              ),
            ),
            IconButton(
              key: const ValueKey('market-settings-refresh'),
              tooltip: '刷新行情来源',
              onPressed: canRefresh ? () => _refreshMarketSettings(ref) : null,
              icon: const Icon(LyraIcons.sync, size: 18),
            ),
          ],
        ),
        const SizedBox(height: 6),
        const _Hint('选择后自动生效，并同步到所有账户与设备。'),
        const SizedBox(height: 14),
        if (session.isLoading && client == null)
          const LinearProgressIndicator(minHeight: 2)
        else if (client == null)
          const _Hint('登录后可以切换行情来源。')
        else
          settings!.when(
            skipLoadingOnRefresh: false,
            loading: () => const Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                LinearProgressIndicator(minHeight: 2),
                SizedBox(height: 8),
                _Hint('正在读取行情来源…'),
              ],
            ),
            error: (error, _) => Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _Hint(
                  error is LyraApiError && error.statusCode == 404
                      ? '暂时无法切换行情来源，请稍后重试。'
                      : lyraErrorMessage(error, fallback: '暂时无法读取行情来源，请稍后重试。'),
                ),
                TextButton(
                  key: const ValueKey('market-settings-retry'),
                  onPressed: () => ref.invalidate(marketSettingsProvider),
                  child: const Text('重试'),
                ),
              ],
            ),
            data: (settings) => _MarketSettingsEditor(
              key: ValueKey(client),
              settings: settings,
            ),
          ),
      ],
    );
  }

  /// Re-reads server settings and quotes without changing saved routes.
  /// [ref] belongs to the current widget and authenticated provider scope.
  void _refreshMarketSettings(WidgetRef ref) {
    ref.invalidate(marketSettingsProvider);
    ref.invalidate(marketPositionsProvider);
  }
}

class _MarketSettingsEditor extends ConsumerStatefulWidget {
  const _MarketSettingsEditor({super.key, required this.settings});
  final MarketSettings settings;

  @override
  ConsumerState<_MarketSettingsEditor> createState() =>
      _MarketSettingsEditorState();
}

class _MarketSettingsEditorState extends ConsumerState<_MarketSettingsEditor> {
  MarketRoutes? _retryRoutes;
  int? _retryRevision;
  bool _applied = false;
  LyraApiError? _error;

  bool get _needsReload => const {
    'MARKET_SETTINGS_CHANGED',
    'MARKET_PROVIDER_NOT_CONFIGURED',
    'INVALID_ARGUMENT',
  }.contains(_error?.code);

  void _choose(String assetType, {required bool backup, required String id}) {
    if (ref.read(marketSettingsPendingProvider) != null || _needsReload) return;
    final settings =
        ref.read(marketSettingsProvider).asData?.value ?? widget.settings;
    final route = settings.routes.forAsset(assetType);
    final primary = route.isEmpty ? '' : route.first;
    final previousBackup = route.length == 2 ? route.last : '';
    final next = backup
        ? [
            if (primary.isNotEmpty) primary,
            if (id.isNotEmpty && id != primary) id,
          ]
        : [
            if (id.isNotEmpty) id,
            if (id.isNotEmpty &&
                previousBackup.isNotEmpty &&
                previousBackup != id)
              previousBackup,
          ];
    final routes = settings.routes.replace(assetType, next);
    if (routes == settings.routes) return;
    _apply(routes, expectedRevision: settings.revision);
  }

  Future<void> _apply(
    MarketRoutes routes, {
    required int expectedRevision,
  }) async {
    if (ref.read(marketSettingsPendingProvider) != null) return;
    final client = ref.read(marketAuthenticatedClientProvider);
    if (client == null) return;
    setState(() {
      _retryRoutes = routes;
      _retryRevision = expectedRevision;
      _applied = false;
      _error = null;
    });
    try {
      await ref
          .read(marketSettingsProvider.notifier)
          .save(expectedRevision: expectedRevision, routes: routes);
      if (!mounted ||
          !identical(client, ref.read(marketAuthenticatedClientProvider))) {
        return;
      }
      setState(() {
        _retryRoutes = null;
        _retryRevision = null;
        _applied = true;
      });
    } on Object catch (error) {
      if (!mounted ||
          !identical(client, ref.read(marketAuthenticatedClientProvider))) {
        return;
      }
      setState(() {
        _error = LyraApiError.from(error);
      });
    }
  }

  void _reload() {
    setState(() {
      _retryRoutes = null;
      _retryRevision = null;
      _applied = false;
      _error = null;
    });
    ref.invalidate(marketSettingsProvider);
  }

  @override
  Widget build(BuildContext context) {
    final error = _error;
    final settings = widget.settings;
    final pending = ref.watch(marketSettingsPendingProvider);
    final routes = pending ?? settings.routes;
    final busy = pending != null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final asset in ['stock', 'option']) ...[
          _RouteCard(
            assetType: asset,
            settings: settings,
            route: routes.forAsset(asset),
            saving: busy || _needsReload,
            onChoose: (backup, id) => _choose(asset, backup: backup, id: id),
          ),
          const SizedBox(height: 12),
        ],
        if (!settings.providers.any(
          (source) =>
              source.configured &&
              (source.supports('stock') || source.supports('option')),
        )) ...[
          const _Hint('服务端尚未配置行情源。'),
          const SizedBox(height: 10),
        ],
        if (busy) ...[
          Semantics(liveRegion: true, child: const _Hint('正在切换…')),
          const SizedBox(height: 10),
        ] else if (error != null) ...[
          Semantics(
            liveRegion: true,
            child: _Hint(switch (error.code) {
              'MARKET_SETTINGS_CHANGED' => '其他设备已修改行情来源。请重新加载后再选择。',
              'MARKET_PROVIDER_NOT_CONFIGURED' => '所选来源未配置，已恢复原选择。请重新加载后再选择。',
              'INVALID_ARGUMENT' => '来源状态已变化。请重新加载后再选择。',
              _ =>
                '未能确认切换，界面已恢复原选择。'
                    '${lyraErrorMessage(error, fallback: '请重试。')}',
            }),
          ),
          if (_needsReload)
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton(
                key: const ValueKey('market-settings-reload'),
                onPressed: _reload,
                child: const Text('重新加载'),
              ),
            )
          else if (_retryRoutes != null && _retryRevision != null)
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton(
                key: const ValueKey('market-settings-retry-change'),
                onPressed: () =>
                    _apply(_retryRoutes!, expectedRevision: _retryRevision!),
                child: const Text('重试切换'),
              ),
            ),
          const SizedBox(height: 10),
        ],
        if (!busy && error == null && _applied) ...[
          Semantics(liveRegion: true, child: const _Hint('已切换，新的行情来源已生效。')),
          const SizedBox(height: 10),
        ],
      ],
    );
  }
}

/// Groups an asset's primary and backup routes in one responsive settings card.
class _RouteCard extends StatelessWidget {
  const _RouteCard({
    required this.assetType,
    required this.settings,
    required this.route,
    required this.saving,
    required this.onChoose,
  });
  final String assetType;
  final MarketSettings settings;
  final List<String> route;
  final bool saving;
  final void Function(bool backup, String id) onChoose;

  @override
  Widget build(BuildContext context) {
    final primary = route.isEmpty ? '' : route.first;
    final backup = route.length < 2 ? '' : route.last;
    final source = settings.source(primary);
    final effective = settings.effectiveRoutes.forAsset(assetType);
    final routeUnavailable =
        !saving &&
        (route.length != effective.length ||
            !List<int>.generate(
              route.length,
              (index) => index,
            ).every((index) => route[index] == effective[index]));
    final String currentState;
    if (primary.isEmpty) {
      currentState = '已关闭';
    } else if (source == null) {
      currentState = '原来源已不可用';
    } else if (!source.supports(assetType)) {
      currentState = '当前来源不支持此类资产';
    } else if (!source.configured) {
      currentState = '当前来源未配置';
    } else if (routeUnavailable) {
      currentState = effective.isNotEmpty && effective.first == primary
          ? '备用来源暂不可用'
          : '当前来源暂不可用';
    } else {
      currentState = source.timingLabel;
    }
    return LyraCard(
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Icon(
                assetType == 'stock'
                    ? LyraIcons.stocks
                    : LyraIcons.accountScope,
                size: 18,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  assetType == 'stock' ? '股票' : '期权',
                  style: const TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
              Flexible(
                child: Align(
                  alignment: Alignment.centerRight,
                  child: Text(
                    currentState,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    textAlign: TextAlign.right,
                    style: const TextStyle(
                      fontSize: 12,
                      color: LyraColors.inkSecondary,
                    ),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          LayoutBuilder(
            builder: (context, constraints) {
              final stacked =
                  constraints.maxWidth < 300 ||
                  MediaQuery.textScalerOf(context).scale(14) > 18;
              final primarySelector = _SourceSelector(
                assetType: assetType,
                backup: false,
                selected: primary,
                excluded: '',
                settings: settings,
                enabled: !saving,
                onChanged: (id) => onChoose(false, id),
              );
              final backupSelector = _SourceSelector(
                assetType: assetType,
                backup: true,
                selected: backup,
                excluded: primary,
                settings: settings,
                enabled: !saving && primary.isNotEmpty,
                onChanged: (id) => onChoose(true, id),
              );
              if (stacked) {
                return Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    primarySelector,
                    const SizedBox(height: 10),
                    backupSelector,
                  ],
                );
              }
              return Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(child: primarySelector),
                  const SizedBox(width: 12),
                  Expanded(child: backupSelector),
                ],
              );
            },
          ),
          if (backup.isNotEmpty) ...[
            const SizedBox(height: 8),
            const _Hint('主来源无可用报价时尝试备用。'),
          ],
        ],
      ),
    );
  }
}

class _SourceSelector extends StatelessWidget {
  const _SourceSelector({
    required this.assetType,
    required this.backup,
    required this.selected,
    required this.excluded,
    required this.settings,
    required this.enabled,
    required this.onChanged,
  });
  final String assetType;
  final bool backup;
  final String selected;
  final String excluded;
  final MarketSettings settings;
  final bool enabled;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    final ids = [
      '',
      for (final source in settings.providers)
        if (source.supports(assetType) && source.id != excluded) source.id,
    ];
    // A saved source can later disappear from the catalog. Keep it visible and
    // invalid until the user deliberately replaces it, instead of changing it.
    if (selected.isNotEmpty && !ids.contains(selected)) ids.add(selected);
    String label(String id) {
      if (id.isEmpty) return backup ? '不使用备用' : '关闭';
      final source = settings.source(id);
      if (source == null || !source.supports(assetType)) return '原来源已不可用';
      return '${source.displayName}${source.configured ? '' : ' · 未配置'}';
    }

    return DropdownButtonFormField<String>(
      key: ValueKey(
        'market-source-$assetType-${backup ? 'backup' : 'primary'}-$selected',
      ),
      initialValue: selected,
      isExpanded: true,
      itemHeight: null,
      menuMaxHeight: 320,
      style: Theme.of(context).textTheme.bodyMedium
          ?.copyWith(fontSize: 14, color: LyraColors.ink),
      decoration: InputDecoration(
        labelText: backup ? '备用来源' : '主来源',
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 12,
        ),
      ),
      selectedItemBuilder: (context) => [
        for (final id in ids)
          Align(
            alignment: Alignment.centerLeft,
            child: Text(
              label(id),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
      ],
      items: [
        for (final id in ids)
          DropdownMenuItem(
            value: id,
            enabled:
                id.isEmpty ||
                (id != excluded && settings.canSelect(assetType, id)),
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 10),
              child: Text(
                label(id),
                style: TextStyle(
                  color: id.isEmpty || settings.canSelect(assetType, id)
                      ? LyraColors.ink
                      : LyraColors.muted,
                ),
              ),
            ),
          ),
      ],
      onChanged: enabled
          ? (id) {
              if (id != null) onChanged(id);
            }
          : null,
    );
  }
}

class _Hint extends StatelessWidget {
  const _Hint(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Text(
    text,
    style: const TextStyle(
      fontSize: 12,
      color: LyraColors.inkSecondary,
      height: 1.5,
    ),
  );
}
