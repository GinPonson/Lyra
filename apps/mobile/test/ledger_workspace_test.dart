import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/features/ledger/ledger_screen.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';
import 'package:lyra_mobile/features/market/market_history_widgets.dart';

import 'support/ledger_fixture.dart';

final _captureKey = GlobalKey();
final _reviewDir = Platform.environment['LYRA_REVIEW_DIR'];

/// 保持公开响应结构，仅裁剪走势记录，验证新首页的空值与单点边界。
class _SparseTrendFixture extends LedgerFixture {
  _SparseTrendFixture(this.count);
  final int count;

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command == 'monthly' || command == 'drawdown') {
      final group =
          (data['groups'] as List<Object?>).first as Map<String, Object?>;
      final section = group['data'] as Map<String, Object?>;
      section['rows'] = (section['rows'] as List<Object?>).take(count).toList();
    }
    return data;
  }
}

/// 同一标的同时存在股票、两份期权，用于验证分组及摘要截取顺序。
class _GroupedPositionFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command == 'positions') {
      final rows = (data['rows'] as List).cast<Map<String, Object?>>();
      final stock = rows.first;
      data['rows'] = [
        stock,
        for (final strike in ['385', '430'])
          {
            ...stock,
            'symbolKey': 'AAPL-$strike-C',
            'assetType': 'option',
            'strike': strike,
            'optionType': 'C',
            'expiry': '2027-03-19',
            'quantity': '1',
            'cost': strike == '385' ? '18.6500' : '20.4000',
            'costBasis': strike == '385' ? '1865.0000' : '2040.0000',
          },
        ...rows.skip(1),
      ];
    }
    return data;
  }
}

/// 为分组入口返回独立合成历史价格，检查股票与不同合约不会共用请求身份。
class _GroupedHistoryFixture extends _GroupedPositionFixture {
  _GroupedHistoryFixture({this.onlyOptions = false});

  final bool onlyOptions;

  /// 独立正股摘要使用不同于图表及历史成本的合成价格，验证展开后直接可见。
  @override
  Map<String, Object?> underlyingQuoteResponse(Map<String, dynamic> query) {
    final response = super.underlyingQuoteResponse(query);
    final data = response['data'] as Map<String, Object?>;
    data.addAll({
      'status': 'available',
      'issue': null,
      'providerId': 'alpaca',
      'feed': 'delayed_sip',
      'fetchedAt': LedgerFixture.timestamp,
      'price': {'value': '205.1250', 'asOf': '2026-09-04T19:59:59.000Z'},
      'previousClose': {'value': '200.00', 'sessionDate': '2026-09-03'},
      'changePercent': '2.5625',
    });
    return response;
  }

  /// 只持有期权时保留真实期权行，不为正股走势图伪造股票持仓。
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (onlyOptions && command == 'positions') {
      final rows = (data['rows'] as List)
          .cast<Map<String, Object?>>()
          .where((row) => row['assetType'] == 'option')
          .toList();
      data['rows'] = rows;
      data['count'] = rows.length;
    }
    return data;
  }

  @override
  Map<String, Object?> marketSettingsResponse() => {
    ...super.marketSettingsResponse(),
    'routes': {
      'stock': ['alpaca'],
      'option': ['alpaca'],
    },
    'effectiveRoutes': {
      'stock': ['alpaca'],
      'option': ['alpaca'],
    },
    'providers': [
      {
        'id': 'alpaca',
        'displayName': 'Alpaca',
        'assetTypes': ['stock', 'option'],
        'configured': true,
        'timing': {'kind': 'delayed', 'delayMinutes': 16},
        'cacheTtlSeconds': 900,
      },
    ],
  };

  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    if (command != 'history') return super.response(command, query);
    final position = (payload('positions', const {})['rows'] as List)
        .cast<Map<String, Object?>>()
        .singleWhere((row) => row['symbolKey'] == query['symbol_key']);
    final target = query['target'] ?? 'position';
    final option = position['assetType'] == 'option' && target == 'position';
    return {
      'metadata': {
        'accountScope': {'kind': 'account', 'account': LedgerFixture.account},
        'ledgerVersion': 12,
        'settingsRevision': 0,
        'fetchedAt': LedgerFixture.timestamp,
      },
      'data': {
        'accountId': LedgerFixture.account,
        'symbolKey': position['symbolKey'],
        'direction': position['direction'],
        'target': target,
        'positionAssetType': position['assetType'],
        'assetType': option ? 'option' : 'stock',
        'currency': 'USD',
        'instrumentKey': option
            ? 'US:option:AAPL:2027-03-19:C:${position['strike']}'
            : 'US:stock:AAPL',
        'range': query['range'],
        'timeframe': '1Day',
        'status': 'available',
        'issue': null,
        'cached': false,
        'providerId': 'alpaca',
        'feed': option ? 'alpaca_option_bars' : 'sip',
        'timing': {'kind': 'delayed', 'delayMinutes': 16},
        'adjustment': option ? 'raw' : 'split',
        'expired': false,
        'cutoff': '2026-09-08T07:44:00.000Z',
        'fetchedAt': LedgerFixture.timestamp,
        'bars': [
          for (final day in ['03', '04'])
            {
              'timestamp': '2026-09-${day}T04:00:00.000Z',
              'open': option ? '20.125' : '200.125',
              'high': option ? '22.5' : '202.5',
              'low': option ? '19.5' : '199.5',
              'close': option ? '21.25' : '201.25',
              'volume': 100,
            },
        ],
      },
    };
  }
}

/// 未知币种的重复标的保持分离，验证展开状态不会串用。
class _UnknownCurrencyDuplicateFixture extends LedgerFixture {
  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    final response = super.response(command, query);
    if (command != 'positions') return response;

    final metadata = Map<String, Object?>.from(
      response['metadata']! as Map<String, Object?>,
    );
    metadata
      ..['aggregateCurrency'] = null
      ..remove('currencyGroups');
    response['metadata'] = metadata;
    final data = response['data']! as Map<String, Object?>;
    final rows = (data['rows']! as List).cast<Map<String, Object?>>();
    data
      ..['rows'] = [
        rows.first,
        {
          ...rows.first,
          'quantity': '10',
          'cost': '5000.00',
          'costBasis': '5000.00',
        },
      ]
      ..['count'] = 2;
    return response;
  }
}

/// 合成同标的的不同合约，确保折叠行直接显示完整到期日、行权价和类型。
class _ContractRowFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (command == 'positions' || command == 'trades') {
      final original = (data['rows'] as List).first as Map<String, Object?>;
      final option = <String, Object?>{
        ...original,
        'underlying': 'AAPL',
        'symbolKey': 'AAPL 03/19/2027 385.00 C',
        'symbol': 'AAPL 03/19/2027 385.00 C',
        'assetType': 'option',
        'direction': 'long',
        'expiry': '2027-03-19',
        'strike': '385',
        'optionType': 'C',
        'quantity': '2',
        'cost': '38.10',
        'costBasis': '3810.00',
      };
      data['rows'] = [
        option,
        if (command == 'trades')
          {
            ...option,
            'symbol': 'AAPL 06/18/2027 430.00 P',
            'expiry': '2027-06-18',
            'strike': '430',
            'optionType': 'P',
          },
      ];
      data['count'] = command == 'trades' ? 2 : 1;
      if (command == 'trades') data['nextCursor'] = null;
    }
    return data;
  }
}

/// 把持仓成本替换为非法形态，验证畸形金额在数据边界失败而不是进入 build。
class _MalformedPositionFixture extends LedgerFixture {
  /// 打开后 positions 行的 cost 不再是十进制字符串。
  bool malformed = true;

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final data = super.payload(command, query);
    if (malformed && command == 'positions') {
      final rows = (data['rows'] as List).cast<Map<String, Object?>>();
      data['rows'] = <Map<String, Object?>>[
        <String, Object?>{
          ...rows.first,
          'cost': <String, Object?>{'x': 1},
        },
        ...rows.skip(1),
      ];
    }
    return data;
  }
}

/// Gives a later page a different currency while keeping the public response shape.
class _MixedCurrencyFixture extends LedgerFixture {
  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    final result = super.response(command, query);
    if (command != 'trades') return result;
    final metadata = result['metadata'] as Map<String, Object?>;
    result['metadata'] = <String, Object?>{
      ...metadata,
      'aggregateCurrency': null,
      'currencyGroups': [
        {
          'currency': 'USD',
          'accountIds': [LedgerFixture.account],
        },
        {
          'currency': 'EUR',
          'accountIds': [LedgerFixture.euroAccount],
        },
      ],
    };
    final data = result['data'] as Map<String, Object?>;
    final rows = (data['rows'] as List).cast<Map<String, Object?>>();
    if (query['cursor'] != null && rows.isNotEmpty) {
      rows.single['accountId'] = LedgerFixture.euroAccount;
    }
    return result;
  }
}

/// Simulates a broken cursor contract that points back to the active page.
class _RepeatedCursorFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final result = super.payload(command, query);
    if (command == 'trades' && query['cursor'] != null) {
      result['nextCursor'] = 'next-page';
    }
    return result;
  }
}

/// 可选生成真实 Flutter 渲染截图；普通测试不依赖系统字体或文件输出。
Future<void> _capture(WidgetTester tester, String name) async {
  if (_reviewDir == null) return;
  // 本地图片解码使用真实异步队列，截图前等候当前列表标识就绪。
  await tester.runAsync(() async {
    for (final element in find.byType(Image).evaluate()) {
      await precacheImage((element.widget as Image).image, element);
    }
  });
  await tester.pumpAndSettle();
  final boundary =
      _captureKey.currentContext!.findRenderObject()! as RenderRepaintBoundary;
  await tester.runAsync(() async {
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = (await image.toByteData(format: ui.ImageByteFormat.png))!;
    await Directory(_reviewDir!).create(recursive: true);
    await File('$_reviewDir/$name.png')
        .writeAsBytes(bytes.buffer.asUint8List());
    image.dispose();
  });
}

/// 推进受控的 Flutter 帧，直到 Riverpod 的真实读取完成或明确失败。
Future<void> _pumpUntilReady(
  WidgetTester tester,
  bool Function() ready,
  String dependency,
) async {
  for (var attempt = 0; attempt < 150; attempt++) {
    if (ready()) return;
    await tester.pump(const Duration(milliseconds: 20));
  }
  fail('$dependency在测试等待期限内仍未完成');
}

void main() {
  setUpAll(() async {
    if (_reviewDir == null) return;
    for (final (family, prefix) in [
      ('Geist', 'geist'),
      ('Geist Mono', 'geist-mono'),
    ]) {
      final loader = FontLoader(family);
      for (final weight in [400, 500, 600, 700]) {
        loader.addFont(rootBundle.load('assets/fonts/$prefix-$weight.ttf'));
      }
      // 中文视觉验收使用本机字体；不向项目复制操作系统字体资产。

      await loader.load();
    }
    final cjk = FontLoader('PingFang SC')
      ..addFont(
        File(Platform.environment['LYRA_CJK_FONT']!)
            .readAsBytes()
            .then((bytes) => ByteData.sublistView(bytes)),
      );
    await cjk.load();
    final icons = FontLoader('MaterialIcons')
      ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
    await icons.load();
  });

  void viewport(WidgetTester tester, Size size) {
    tester.view.physicalSize = size;
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
  }

  for (final size in [const Size(390, 844), const Size(1440, 960)]) {
    testWidgets('有数据账号与股票视觉验收 ${size.width}', (tester) async {
      viewport(tester, size);
      final fixture = LedgerFixture();
      final dependencies = await fixture.dependencies();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
          ],
          child: RepaintBoundary(key: _captureKey, child: const LyraApp()),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('+2,846.72'), findsOneWidget);
      // 汇总集中在一张卡内，导入由页头提供，不再重复底部导航功能。
      expect(
        find.byKey(const ValueKey('overview-summary-card')),
        findsOneWidget,
      );
      expect(find.text('查看持仓'), findsNothing);
      expect(find.text('交易记录'), findsNothing);
      expect(find.text('数据截至 2026年9月8日 08:00:00 UTC'), findsOneWidget);
      expect(find.textContaining('账本 v'), findsNothing);
      expect(find.textContaining(LedgerFixture.timestamp), findsNothing);
      expect(find.byTooltip('导入文件'), findsOneWidget);
      final preview = find.byKey(const ValueKey('overview-holdings-preview'));
      expect(
        find.descendant(of: preview, matching: find.byType(ExpansionTile)),
        findsNWidgets(3),
      );
      expect(find.text('按月'), findsOneWidget);
      await _capture(tester, 'accounts-${size.width.toInt()}-fixture');
      final openHoldings = find.byTooltip('查看全部持仓');
      await tester.ensureVisible(openHoldings);
      await tester.tap(openHoldings);
      await tester.pumpAndSettle();
      expect(find.text('AAPL'), findsOneWidget);
      expect(find.text('4,986.00'), findsNothing);
      await _capture(tester, 'stocks-${size.width.toInt()}-fixture');
      final aaplGroup = find.byKey(
        const PageStorageKey<String>('holding-group-AAPL-USD-1'),
      );
      await tester.tap(aaplGroup);
      await tester.pumpAndSettle();
      expect(find.text('4,286.40'), findsOneWidget);
      expect(find.text('首次开仓'), findsNothing);
      // 展开组内原始行后，精确成本与数量仍来自服务端记录。
      await tester.tap(
        find.byKey(
          const PageStorageKey<String>(
            'holding-row-a2000000-0000-4000-8000-000000000001-AAPL-null-null-long',
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('首次开仓'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('账号摘要按标的截取，展开同标的后保留全部股票与期权明细', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = _GroupedPositionFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: RepaintBoundary(key: _captureKey, child: const LyraApp()),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('AAPL'), findsOneWidget);
    expect(find.text('NVDA'), findsOneWidget);
    expect(find.text('MSFT'), findsOneWidget);
    expect(find.text('VOO'), findsNothing);
    expect(find.text('3 项持仓'), findsOneWidget);
    expect(find.text('385 看涨'), findsNothing);
    await _capture(tester, 'accounts-grouped-fixture');
    await Scrollable.ensureVisible(
      tester.element(find.text('AAPL')),
      alignment: .4,
    );
    await tester.tap(find.text('AAPL'));
    await tester.pumpAndSettle();
    expect(find.text('385 看涨'), findsOneWidget);
    expect(find.text('430 看涨'), findsOneWidget);
    expect(find.text('1,865.00'), findsOneWidget);
    expect(find.text('2,040.00'), findsOneWidget);
    expect(find.text('18.65'), findsNothing);
    expect(find.text('20.40'), findsNothing);
    await Scrollable.ensureVisible(
      tester.element(find.text('385 看涨')),
      alignment: .4,
    );
    await tester.tap(find.text('385 看涨'));
    await tester.pumpAndSettle();
    expect(find.text('1865.0000'), findsOneWidget);
    expect(find.text('18.6500'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('股票页单条和多条标的统一折叠，展开保留成本与合约', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = _GroupedPositionFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: LyraTheme.dark,
          home: const ReportScreen(view: LedgerView.positions),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final singleGroup = find.byKey(
      const PageStorageKey<String>('holding-group-NVDA-USD-1'),
    );
    final multiGroup = find.byKey(
      const PageStorageKey<String>('holding-group-AAPL-USD-3'),
    );
    // 展开后卡片中心会落在子项上；始终点击组标题验证折叠行为。
    final singleHeader = find.descendant(
      of: singleGroup,
      matching: find.text('1 项持仓'),
    );
    final multiHeader = find.descendant(
      of: multiGroup,
      matching: find.text('3 项持仓'),
    );
    expect(singleGroup, findsOneWidget);
    expect(multiGroup, findsOneWidget);
    expect(find.text('1 项持仓'), findsNWidgets(3));
    expect(find.text('3 项持仓'), findsOneWidget);
    expect(find.text('3,924.72'), findsNothing);
    expect(find.text('385 看涨'), findsNothing);
    expect(find.text('1,865.00'), findsNothing);

    await tester.ensureVisible(singleGroup);
    await tester.tap(singleHeader);
    await tester.pumpAndSettle();
    expect(find.text('3,924.72'), findsOneWidget);
    expect(find.text('36 股'), findsOneWidget);
    await tester.tap(singleHeader);
    await tester.pumpAndSettle();
    expect(find.text('3,924.72'), findsNothing);

    await tester.ensureVisible(multiGroup);
    await tester.tap(multiHeader);
    await tester.pumpAndSettle();
    expect(find.text('385 看涨'), findsOneWidget);
    expect(find.text('430 看涨'), findsOneWidget);
    expect(find.text('2027-03-19'), findsNWidgets(2));
    expect(find.text('4,286.40'), findsOneWidget);
    expect(find.text('1,865.00'), findsOneWidget);
    expect(find.text('2,040.00'), findsOneWidget);
    await tester.ensureVisible(find.text('385 看涨'));
    await tester.tap(find.text('385 看涨'));
    await tester.pumpAndSettle();
    expect(find.text('1865.0000'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  for (final onlyOptions in [false, true]) {
    testWidgets('正股与合约入口分开，${onlyOptions ? '仅期权持仓' : '混合持仓'}也能看正股', (
      tester,
    ) async {
      viewport(tester, const Size(390, 844));
      final fixture = _GroupedHistoryFixture(onlyOptions: onlyOptions);
      final dependencies = await fixture.dependencies();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
          ],
          child: MaterialApp(
            theme: LyraTheme.dark,
            home: const ReportScreen(view: LedgerView.positions),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('正股走势'), findsNothing);
      expect(find.byTooltip('合约走势'), findsNothing);
      expect(
        fixture.requests.where(
          (request) => request.path == '/v1/market/underlying-quote',
        ),
        isEmpty,
      );
      final group = find.byKey(
        PageStorageKey<String>('holding-group-AAPL-USD-${onlyOptions ? 2 : 3}'),
      );
      await tester.ensureVisible(group);
      await tester.tap(group);
      await tester.pumpAndSettle();
      expect(find.text('正股走势'), findsOneWidget);
      expect(find.byTooltip('合约走势'), findsNWidgets(2));
      expect(find.text('205.1250 USD'), findsOneWidget);
      expect(find.text('+2.56 %'), findsOneWidget);
      final quoteRequests = fixture.requests.where(
        (request) => request.path == '/v1/market/underlying-quote',
      );
      expect(quoteRequests, hasLength(1));
      expect(
        quoteRequests.single.queryParameters['scope'],
        LedgerFixture.account,
      );
      expect(
        quoteRequests.single.queryParameters['symbol_key'],
        onlyOptions ? 'AAPL-385-C' : 'AAPL',
      );
      expect(quoteRequests.single.queryParameters['ledger_version'], '12');

      await tester.ensureVisible(find.text('正股走势'));
      await tester.tap(find.text('正股走势'));
      await tester.pumpAndSettle();
      expect(
        find.descendant(
          of: find.byType(MarketHistorySheet),
          matching: find.text('正股走势'),
        ),
        findsOneWidget,
      );
      final stockQuery = fixture.requests
          .lastWhere((request) => request.path == '/v1/market/history')
          .queryParameters;
      expect(stockQuery['target'], 'underlying');
      expect(stockQuery['symbol_key'], onlyOptions ? 'AAPL-385-C' : 'AAPL');
      expect(stockQuery['scope'], LedgerFixture.account);
      expect(tester.takeException(), isNull);
      await tester.ensureVisible(find.byTooltip('关闭'));
      await tester.tap(find.byTooltip('关闭'));
      await tester.pumpAndSettle();

      for (final strike in ['385', '430']) {
        final symbol = 'AAPL-$strike-C';
        final row = find.byKey(
          PageStorageKey<String>(
            'holding-row-${LedgerFixture.account}-$symbol-2027-03-19-$strike-long',
          ),
        );
        final action = find.descendant(
          of: row,
          matching: find.byTooltip('合约走势'),
        );
        await tester.ensureVisible(action);
        await tester.tap(action);
        await tester.pumpAndSettle();
        expect(find.text('行情走势'), findsOneWidget);
        final query = fixture.requests
            .lastWhere((request) => request.path == '/v1/market/history')
            .queryParameters;
        expect(query['scope'], LedgerFixture.account);
        expect(query['symbol_key'], symbol);
        expect(query['direction'], 'long');
        expect(query['range'], '1m');
        expect(query['target'], 'position');
        expect(query['ledger_version'].toString(), '12');
        expect(query['settings_revision'].toString(), '0');
        expect(tester.takeException(), isNull);
        await tester.ensureVisible(find.byTooltip('关闭'));
        await tester.tap(find.byTooltip('关闭'));
        await tester.pumpAndSettle();
      }

      await tester.ensureVisible(find.text('385 看涨'));
      await tester.tap(find.text('385 看涨'));
      await tester.pumpAndSettle();
      expect(find.text('1865.0000'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('未知币种同标的单条分组的展开状态互不串用', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = _UnknownCurrencyDuplicateFixture();
    expect(
      () => LedgerRead.fromJson(fixture.response('positions', {})),
      returnsNormally,
    );
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: LyraTheme.dark,
          home: const ReportScreen(view: LedgerView.positions),
        ),
      ),
    );
    await tester.pumpAndSettle();

    const firstGroupKey = PageStorageKey<String>(
      'holding-group-AAPL-null-1-unknown-0',
    );
    const secondGroupKey = PageStorageKey<String>(
      'holding-group-AAPL-null-1-unknown-1',
    );
    expect(find.byKey(firstGroupKey), findsOneWidget);
    expect(find.byKey(secondGroupKey), findsOneWidget);
    expect(find.text('1 项持仓'), findsNWidgets(2));

    await tester.tap(
      find.descendant(
        of: find.byKey(firstGroupKey),
        matching: find.text('1 项持仓'),
      ),
    );
    await tester.pumpAndSettle();
    const firstRecordKey = PageStorageKey<String>(
      'holding-row-a2000000-0000-4000-8000-000000000001-AAPL-null-null-long',
    );
    expect(find.byKey(firstRecordKey), findsOneWidget);
    await tester.tap(find.byKey(firstRecordKey));
    await tester.pumpAndSettle();
    expect(find.text('首次开仓'), findsOneWidget);

    await tester.tap(
      find.descendant(
        of: find.byKey(firstGroupKey),
        matching: find.text('1 项持仓'),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(
      find.descendant(
        of: find.byKey(secondGroupKey),
        matching: find.text('1 项持仓'),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('首次开仓'), findsNothing);
    expect(find.text('5,000.00'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  for (final view in [LedgerView.positions, LedgerView.trades]) {
    for (final (size, scale) in [
      (const Size(390, 844), 1.0),
      (const Size(320, 740), 1.6),
    ]) {
      testWidgets('${view.command} 合约身份完整且不受金额挤压 ${size.width}/$scale', (
        tester,
      ) async {
        viewport(tester, size);
        tester.platformDispatcher.textScaleFactorTestValue = scale;
        addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
        final dependencies = await _ContractRowFixture().dependencies();
        await tester.pumpWidget(
          ProviderScope(
            overrides: [
              sessionDependenciesProvider.overrideWithValue(dependencies),
            ],
            child: MaterialApp(
              theme: LyraTheme.dark,
              home: ReportScreen(view: view),
            ),
          ),
        );
        await tester.pumpAndSettle();
        if (view == LedgerView.positions) {
          await tester.tap(
            find.byKey(
              const PageStorageKey<String>('holding-group-AAPL-USD-1'),
            ),
          );
          await tester.pumpAndSettle();
          final expiry = find.text('2027-03-19');
          expect(expiry, findsOneWidget);
          expect(find.text('385 看涨'), findsOneWidget);
          final label = tester.widget<Text>(expiry);
          expect(label.maxLines, isNull);
          expect(label.overflow, isNull);
          expect(find.text('3,810.00'), findsOneWidget);
          expect(find.text('38.10'), findsNothing);
        } else {
          final contract = find.text('2027-03-19 · 385 看涨');
          expect(contract, findsOneWidget);
          final label = tester.widget<Text>(contract);
          expect(label.maxLines, isNull);
          expect(label.overflow, isNull);
          expect(find.text('2027-06-18 · 430 看跌'), findsOneWidget);
        }
        expect(tester.takeException(), isNull);
      });
    }
  }

  for (final view in [LedgerView.expiring, LedgerView.risk]) {
    testWidgets('${view.command} 显式传递服务端要求的基准日', (tester) async {
      viewport(tester, const Size(390, 844));
      final fixture = LedgerFixture();
      final dependencies = await fixture.dependencies();
      final today = DateTime.now();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
          ],
          child: MaterialApp(
            theme: LyraTheme.dark,
            home: ReportScreen(view: view),
          ),
        ),
      );
      await tester.pumpAndSettle();
      final query = fixture.requests
          .lastWhere((request) => request.uri.path == view.path)
          .queryParameters;
      final asOf = DateTime.parse(query['as_of'] as String);
      expect(asOf, DateTime(today.year, today.month, today.day));
      expect(query.containsKey('asOf'), isFalse);
      expect(query['scope'], 'all');
      if (view == LedgerView.expiring) expect(query['days'], '30');
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('仓位配置切换资产类别时传递公开 group_by 而非被服务端忽略的字段', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('股票'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('仓位配置'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('筛选'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('按标的'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('按资产类别').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('应用筛选'));
    await tester.pumpAndSettle();
    final query = fixture.requests
        .lastWhere((request) => request.uri.path == LedgerView.allocation.path)
        .queryParameters;
    expect(query['group_by'], 'asset-type');
    expect(query.containsKey('groupBy'), isFalse);
    expect(LedgerView.attribution.parameters['group_by'], 'underlying');
    expect(tester.takeException(), isNull);
  });

  testWidgets('首页收益切换调用对应报告，失败可重试且不隐藏账户金额', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture()..failures.add('drawdown');
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();

    // 总览默认查询累计报告；嵌入图表的金额需先点选才会显示。
    expect(
      fixture.requests.any(
        (request) => request.uri.path == LedgerView.drawdown.path,
      ),
      isTrue,
    );
    expect(find.text('489.16'), findsNothing);
    expect(find.text('暂时无法读取数据，请重新加载。'), findsOneWidget);

    await Scrollable.ensureVisible(
      tester.element(find.text('按月')),
      alignment: .5,
    );
    await tester.tap(find.text('按月'));
    await tester.pumpAndSettle();
    final monthlyReads = fixture.requests
        .where((request) => request.uri.path == LedgerView.monthly.path)
        .toList();
    expect(monthlyReads, isNotEmpty);
    expect(monthlyReads.last.queryParameters['scope'], 'all');
    final plot = find.byKey(const ValueKey<String>('ledger-data-plot'));
    await tester.ensureVisible(plot);
    final bounds = tester.getRect(plot);
    await tester.tapAt(bounds.topLeft + Offset(bounds.width - 8, 80));
    await tester.pumpAndSettle();
    expect(find.textContaining('2026-09 / 当月损益'), findsOneWidget);
    expect(find.textContaining('489.16'), findsOneWidget);

    await Scrollable.ensureVisible(
      tester.element(find.text('累计')),
      alignment: .5,
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('累计'));
    await tester.pumpAndSettle();
    expect(
      fixture.requests
          .where((request) => request.uri.path == LedgerView.drawdown.path)
          .last
          .queryParameters['scope'],
      'all',
    );
    expect(find.text('+2,846.72'), findsOneWidget);
    expect(find.text('暂时无法读取数据，请重新加载。'), findsOneWidget);
    expect(find.textContaining('QUERY_FAILED'), findsNothing);
    fixture.failures.clear();
    await tester.ensureVisible(find.text('重新加载'));
    await tester.tap(find.text('重新加载'));
    await tester.pumpAndSettle();
    expect(find.text('暂时无法读取数据，请重新加载。'), findsNothing);
    final cumulativePlot = find.byKey(
      const ValueKey<String>('ledger-data-plot'),
    );
    await tester.ensureVisible(cumulativePlot);
    final cumulativeBounds = tester.getRect(cumulativePlot);
    await tester.tapAt(
      cumulativeBounds.topLeft + Offset(cumulativeBounds.width - 8, 80),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('/ 累计损益'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  for (final count in [0, 1]) {
    testWidgets('首页累计走势支持 $count 条记录', (tester) async {
      viewport(tester, const Size(390, 844));
      final dependencies = await _SparseTrendFixture(count).dependencies();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
          ],
          child: const LyraApp(),
        ),
      );
      await tester.pumpAndSettle();
      await Scrollable.ensureVisible(
        tester.element(find.text('累计')),
        alignment: .5,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('累计'));
      await tester.pumpAndSettle();
      expect(find.text('暂无收益记录'), count == 0 ? findsOneWidget : findsNothing);
      expect(
        find.byKey(const ValueKey<String>('ledger-data-plot')),
        count == 0 ? findsNothing : findsOneWidget,
      );
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('查询携带范围、标的筛选，失败显示错误而非空持仓', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('股票'));
    await tester.pumpAndSettle();
    expect(find.byType(TextField), findsNothing);
    await tester.tap(find.byTooltip('搜索证券'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'aapl');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pump();
    // A settled frame does not wait for Riverpod's auto-disposed HTTP future;
    // read the exact UI query so this assertion observes the real response.
    final filteredQuery = (
      path: LedgerView.positions.path,
      parameters: Uri(queryParameters: {'underlying': 'AAPL'}).query,
      scope: 'all',
    );
    final container = ProviderScope.containerOf(
      tester.element(find.byType(LyraApp)),
    );
    final filteredRead = ledgerReadProvider(filteredQuery);
    container.read(filteredRead);
    await _pumpUntilReady(
      tester,
      () => !container.read(filteredRead).isLoading,
      '股票筛选查询',
    );
    expect(container.read(filteredRead).hasValue, isTrue);
    await tester.pumpAndSettle();
    expect(
      fixture.requests
          .where((request) => request.uri.path == LedgerView.positions.path)
          .last
          .queryParameters['underlying'],
      'AAPL',
    );
    expect(find.byType(TextField), findsNothing);
    expect(find.text('标的 · AAPL'), findsOneWidget);
    expect(find.text('清除筛选'), findsNothing);
    // 收起编辑器保留已应用条件，未提交的输入不得改变查询。
    await tester.tap(find.byTooltip('搜索证券'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'NVDA');
    await tester.tap(find.byTooltip('搜索证券'));
    await tester.pumpAndSettle();
    expect(find.text('标的 · AAPL'), findsOneWidget);
    final positionReads = fixture.requests
        .where((request) => request.uri.path == LedgerView.positions.path)
        .toList();
    expect(positionReads.last.queryParameters['underlying'], 'AAPL');
    expect(positionReads.last.queryParameters['scope'], 'all');
    expect(find.text('NVDA'), findsNothing);

    fixture.failures.add('positions');
    await tester.tap(find.text('全部账户').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('长期投资'));
    await tester.pumpAndSettle();
    expect(
      fixture.requests
          .where((r) => r.uri.path.endsWith('/positions'))
          .last
          .queryParameters['scope'],
      LedgerFixture.account,
    );
    await tester.pumpAndSettle();
    expect(find.text('数据未能加载'), findsOneWidget);
    expect(find.text('暂时无法读取数据，请重新加载。'), findsOneWidget);
    expect(find.textContaining('QUERY_FAILED'), findsNothing);
    expect(find.text('暂无持仓记录'), findsNothing);
    fixture.failures.clear();
    await tester.ensureVisible(find.text('重新加载'));
    await tester.tap(find.text('重新加载'));
    await tester.pumpAndSettle();
    expect(find.text('AAPL'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('交易滚动自动使用服务端游标，筛选后从头读取', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: RepaintBoundary(key: _captureKey, child: const LyraApp()),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('交易'));
    await tester.pumpAndSettle();
    await _capture(tester, 'trades-390-fixture');
    // The short first page can auto-fill the viewport. Dragging the page also
    // verifies that loading is attached to its parent scroll position.
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(fixture.requests.last.queryParameters['cursor'], 'next-page');
    expect(find.text('MSFT'), findsOneWidget);
    expect(
      fixture.requests
          .where(
            (request) =>
                request.uri.path.endsWith('/trades') &&
                request.queryParameters['cursor'] != null,
          )
          .length,
      1,
    );
    expect(find.text('上一页'), findsNothing);
    expect(find.text('下一页'), findsNothing);
    expect(find.textContaining('第 '), findsNothing);
    final requestCount = fixture.requests.length;
    await tester.ensureVisible(find.byTooltip('搜索证券'));
    await tester.tap(find.byTooltip('搜索证券'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'AAPL');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pumpAndSettle();
    // 收起输入框会改变滚动布局；等待下一帧开始新查询后再核对请求。
    await tester.pump();
    await tester.pumpAndSettle();
    final filteredReads = fixture.requests.skip(requestCount).toList();
    expect(filteredReads, isNotEmpty);
    expect(filteredReads.first.queryParameters['symbol'], 'AAPL');
    expect(filteredReads.first.queryParameters.containsKey('cursor'), isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets('后续页失败保留已有记录，并可重试同一游标', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture()..failOnce.add('trades:next-page');
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('交易'));
    await tester.pumpAndSettle();
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(find.text('AAPL'), findsOneWidget);
    expect(find.text('数据未能加载'), findsOneWidget);
    expect(find.text('MSFT'), findsNothing);
    final failedCursor = fixture.requests.last;
    expect(failedCursor.queryParameters['cursor'], 'next-page');
    await tester.ensureVisible(find.text('重新加载'));
    await tester.tap(find.text('重新加载'));
    await tester.pumpAndSettle();
    expect(find.text('AAPL'), findsOneWidget);
    expect(find.text('MSFT'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('账本版本不一致时拒绝合并后续页', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture()..ledgerVersionsByCursor['next-page'] = 13;
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('交易'));
    await tester.pumpAndSettle();
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('账本版本已更新'), findsOneWidget);
    expect(find.text('AAPL'), findsOneWidget);
    expect(find.text('MSFT'), findsNothing);
    expect(find.text('从头刷新'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('重复游标停止续页并明确提示', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = _RepeatedCursorFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('交易'));
    await tester.pumpAndSettle();
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('重复分页游标'), findsOneWidget);
    expect(find.text('AAPL'), findsOneWidget);
    expect(find.text('MSFT'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('切换股票账户后交易列表从第一页重新读取', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('交易'));
    await tester.pumpAndSettle();
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(find.text('MSFT'), findsOneWidget);
    final previousCount = fixture.requests.length;
    await tester.tap(find.text('全部账户').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('长期投资'));
    await tester.pumpAndSettle();
    final scopedReads = fixture.requests
        .skip(previousCount)
        .where((request) => request.uri.path.endsWith('/trades'))
        .toList();
    expect(scopedReads, isNotEmpty);
    expect(scopedReads.first.queryParameters['scope'], LedgerFixture.account);
    expect(scopedReads.first.queryParameters.containsKey('cursor'), isFalse);
    expect(tester.takeException(), isNull);
  });

  testWidgets('延迟返回的旧筛选分页不会混入新列表', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture();
    final entered = Completer<void>();
    final filteredEntered = Completer<void>();
    final release = Completer<void>();
    addTearDown(() {
      if (!release.isCompleted) release.complete();
    });
    var held = false;
    fixture.beforeResponse = (options) async {
      if (!held &&
          options.uri.path.endsWith('/trades') &&
          options.queryParameters['cursor'] == 'next-page') {
        held = true;
        entered.complete();
        await release.future;
      }
      if (options.uri.path.endsWith('/trades') &&
          options.queryParameters['symbol'] == 'AAPL' &&
          !filteredEntered.isCompleted) {
        filteredEntered.complete();
      }
    };
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('交易'));
    // 延迟游标会让不确定进度指示器持续运行；有限推进帧后等待请求拦截，不能 settle 全部动画。
    await tester.pump(const Duration(milliseconds: 400));
    final container = ProviderScope.containerOf(
      tester.element(find.byType(LyraApp)),
    );
    final firstPageQuery = (
      path: LedgerView.trades.path,
      parameters: Uri(queryParameters: LedgerView.trades.parameters).query,
      scope: 'all',
    );
    final firstPageRead = ledgerReadProvider(firstPageQuery);
    container.read(firstPageRead);
    await _pumpUntilReady(
      tester,
      () => !container.read(firstPageRead).isLoading,
      '交易首屏查询',
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    await tester.pump();
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -1200),
    );
    await _pumpUntilReady(
      tester,
      () => fixture.requests.any(
        (request) =>
            request.uri.path.endsWith('/trades') &&
            request.queryParameters['cursor'] == 'next-page',
      ),
      '交易下一页请求',
    );
    expect(entered.isCompleted, isTrue);
    await tester.pump();
    final search = find.byTooltip('搜索证券');
    await Scrollable.ensureVisible(tester.element(search), alignment: 0);
    await tester.tap(search);
    await tester.pump();
    await tester.enterText(find.byType(TextField), 'AAPL');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pump();
    await _pumpUntilReady(
      tester,
      () => filteredEntered.isCompleted,
      'AAPL 筛选请求',
    );
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 40)),
    );
    await tester.pump();
    final filteredReads = fixture.requests
        .where(
          (request) =>
              request.uri.path.endsWith('/trades') &&
              request.queryParameters['symbol'] == 'AAPL',
        )
        .toList();
    expect(filteredReads.first.queryParameters.containsKey('cursor'), isFalse);
    expect(find.text('AAPL'), findsOneWidget);
    release.complete();
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 40)),
    );
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pumpAndSettle();
    expect(find.text('MSFT'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('不同币种交易分页分开展示', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = _MixedCurrencyFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: const LyraApp(),
      ),
    );
    await tester.pumpAndSettle();
    final container = ProviderScope.containerOf(
      tester.element(find.byType(LyraApp)),
    );
    await _pumpUntilReady(
      tester,
      () => !container.read(sessionProvider).isLoading,
      '会话恢复',
    );
    await tester.pump();
    container.read(accountScopeReadyProvider);
    await _pumpUntilReady(
      tester,
      () => !container.read(accountScopeReadyProvider).isLoading,
      '账户范围恢复',
    );
    await tester.pump();
    await tester.tap(find.text('交易'));
    await tester.pump();
    final firstPageQuery = (
      path: LedgerView.trades.path,
      parameters: Uri(queryParameters: LedgerView.trades.parameters).query,
      scope: 'all',
    );
    final firstPageRead = ledgerReadProvider(firstPageQuery);
    container.read(firstPageRead);
    await _pumpUntilReady(
      tester,
      () => !container.read(firstPageRead).isLoading,
      '多币种交易首屏查询',
    );
    final firstPage = container.read(firstPageRead);
    expect(
      firstPage.hasValue,
      isTrue,
      reason:
          '首屏查询错误：${firstPage.error}\n${firstPage.stackTrace}\n'
          'requests: ${fixture.requests.map((request) => request.uri).toList()}',
    );
    expect(firstPage.value!.data['nextCursor'], 'next-page');
    await tester.pumpAndSettle();
    expect(find.text('AAPL'), findsOneWidget);
    expect(find.text('数据未能加载'), findsNothing);
    await tester.drag(
      find.byKey(const PageStorageKey<String>('ledger-scroll')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(
      fixture.requests.any(
        (request) =>
            request.uri.path == LedgerView.trades.path &&
            request.queryParameters['cursor'] == 'next-page',
      ),
      isTrue,
      reason:
          'requests: ${fixture.requests.map((request) => request.uri).toList()}',
    );
    final nextPageQuery = (
      path: LedgerView.trades.path,
      parameters: Uri(
        queryParameters: {
          ...LedgerView.trades.parameters,
          'cursor': 'next-page',
        },
      ).query,
      scope: 'all',
    );
    final nextPageRead = ledgerReadProvider(nextPageQuery);
    container.read(nextPageRead);
    await _pumpUntilReady(
      tester,
      () => !container.read(nextPageRead).isLoading,
      '多币种交易下一页查询',
    );
    await tester.pump();
    await tester.pumpAndSettle();
    expect(
      fixture.requests.any(
        (request) => request.queryParameters['cursor'] == 'next-page',
      ),
      isTrue,
    );
    expect(find.text('EUR'), findsOneWidget);
    expect(find.text('MSFT'), findsOneWidget);
    await Scrollable.ensureVisible(
      tester.element(find.text('USD').first),
      alignment: 0,
    );
    expect(find.text('USD'), findsAtLeastNWidgets(1));
    expect(tester.takeException(), isNull);
  });

  for (final view in LedgerView.values) {
    testWidgets('${view.command} 按公开响应渲染，可展开且无溢出', (tester) async {
      viewport(tester, const Size(390, 844));
      final fixture = LedgerFixture();
      final dependencies = await fixture.dependencies();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
          ],
          child: RepaintBoundary(
            key: _captureKey,
            child: MaterialApp(
              debugShowCheckedModeBanner: false,
              theme: LyraTheme.dark,
              home: ReportScreen(view: view),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('数据未能加载'), findsNothing);
      expect(fixture.requests.any((r) => r.uri.path == view.path), isTrue);
      if (view == LedgerView.positions) {
        // 股票页将完整读取时点放在历史成本说明 tooltip 中。
        expect(
          find.byTooltip(RegExp('数据截至 2026年9月8日 08:00:00 UTC')),
          findsOneWidget,
        );
        expect(
          find.textContaining('数据截至 2026年9月8日 08:00:00 UTC'),
          findsNothing,
        );
      } else {
        expect(
          find.textContaining('数据截至 2026年9月8日 08:00:00 UTC'),
          findsOneWidget,
        );
      }
      expect(find.textContaining('账本 v'), findsNothing);
      expect(find.textContaining(LedgerFixture.timestamp), findsNothing);
      if (view == LedgerView.monthly || view == LedgerView.allocation) {
        await _capture(tester, '${view.command}-390-fixture');
      }
      if (view == LedgerView.monthly) {
        final plot = find.byKey(const ValueKey<String>('ledger-data-plot'));
        await tester.ensureVisible(plot);
        final bounds = tester.getRect(plot);
        await tester.tapAt(bounds.topLeft + const Offset(8, 70));
        await tester.pumpAndSettle();
        expect(find.text('2026-04 / 当月损益'), findsOneWidget);
      }
      final row = find.byType(ExpansionTile).first;
      await tester.ensureVisible(row);
      await tester.tap(row);
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }

  for (final paired in [false, true]) {
    testWidgets('小屏放大字体仍可阅读和导航：paired=$paired', (tester) async {
      viewport(tester, const Size(320, 740));
      tester.platformDispatcher.textScaleFactorTestValue = 1.6;
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final fixture = LedgerFixture();
      final dependencies = await fixture.dependencies(paired: paired);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
          ],
          child: RepaintBoundary(key: _captureKey, child: const LyraApp()),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      if (paired) {
        await tester.tap(find.text('股票'));
      } else {
        expect(find.byKey(const ValueKey('login-screen')), findsOneWidget);
        expect(find.byType(NavigationBar), findsNothing);
        await tester.ensureVisible(find.byKey(const ValueKey('login-google')));
      }
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('未登录首屏进入独立登录页并提供三种身份入口', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = LedgerFixture();
    final dependencies = await fixture.dependencies(paired: false);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: RepaintBoundary(key: _captureKey, child: const LyraApp()),
      ),
    );
    await tester.pumpAndSettle();
    await _capture(tester, 'welcome-390');
    expect(find.byKey(const ValueKey('login-screen')), findsOneWidget);
    expect(find.text('使用 Google 登录'), findsOneWidget);
    expect(find.text('使用 Linux.do 登录'), findsOneWidget);
    expect(find.text('使用 GitHub 登录'), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    expect(tester.takeException(), isNull);
  });

  test('畸形列表和非有限绘图值在数据边界被拒绝', () {
    expect(
      () => LedgerRead.fromJson({
        'metadata': <String, Object?>{},
        'data': {'rows': 'invalid'},
      }),
      throwsFormatException,
    );
    expect(
      () => LedgerRead.fromJson({
        'metadata': <String, Object?>{},
        'data': {
          'rows': [
            {'netPnl': 'NaN'},
          ],
        },
      }),
      throwsFormatException,
    );
  });

  test('多币种明细按 accountId 标注，分组不伪造总计', () {
    final read = LedgerRead.fromJson({
      'metadata': {
        'aggregateCurrency': null,
        'currencyGroups': [
          {
            'currency': 'USD',
            'accountIds': ['a'],
          },
          {
            'currency': 'EUR',
            'accountIds': ['b'],
          },
        ],
      },
      'data': {
        'groups': [
          {
            'currency': 'USD',
            'data': {'rows': []},
          },
          {
            'currency': 'EUR',
            'data': {'rows': []},
          },
        ],
      },
    });
    expect(read.currencyFor({'accountId': 'b'}), 'EUR');
    expect(read.sections.map((section) => section.currency), ['USD', 'EUR']);
  });

  test('金额与标识字段在数据边界校验，畸形取值不进入 build', () {
    for (final row in <Map<String, Object?>>[
      {
        'cost': <String, Object?>{'nested': true},
      },
      {'cost': 'abc'},
      {'price': 12.5},
      {'amount': '1e3'},
      {'fees': '1.2.3'},
      {'openPrice': ''},
      {'closePrice': '--'},
      {'strike': '1,000'},
      {'quantity': '很多'},
      {'underlying': 42},
      {'symbol': ''},
      {'symbolKey': '   '},
      {'check': 7},
      {'count': 'x'},
      {'count': 1.5},
      {'trade': 'invalid'},
      {'trade': null},
    ]) {
      expect(
        () => LedgerRead.fromJson({
          'metadata': <String, Object?>{},
          'data': {
            'rows': [row],
          },
        }),
        throwsFormatException,
        reason: '$row 必须在数据边界被拒绝',
      );
    }
  });

  test('extremes 嵌套 trade 记录同样在数据边界校验', () {
    expect(
      () => LedgerRead.fromJson({
        'metadata': <String, Object?>{},
        'data': {
          'rows': [
            {
              'trade': {'underlying': 'AAPL', 'pnl': 'oops'},
            },
          ],
        },
      }),
      throwsFormatException,
    );
  });

  testWidgets('畸形明细金额显示可重试错误态而不是红屏', (tester) async {
    viewport(tester, const Size(390, 844));
    final fixture = _MalformedPositionFixture();
    final dependencies = await fixture.dependencies();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(dependencies),
        ],
        child: RepaintBoundary(
          key: _captureKey,
          child: MaterialApp(
            debugShowCheckedModeBanner: false,
            theme: LyraTheme.dark,
            home: const ReportScreen(view: LedgerView.positions),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    // 解析错误在异步边界被捕获，页面进入设计的错误态并给出重试入口。
    expect(find.text('数据未能加载'), findsOneWidget);
    expect(find.text('暂时无法读取数据，请重新加载。'), findsOneWidget);
    expect(find.textContaining('账本字段 cost'), findsNothing);
    expect(find.text('重新加载'), findsOneWidget);
    expect(tester.takeException(), isNull);

    fixture.malformed = false;
    await tester.tap(find.text('重新加载'));
    await tester.pumpAndSettle();
    expect(find.text('AAPL'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
