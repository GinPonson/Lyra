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
          },
        ...rows.skip(1),
      ];
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
      expect(find.text('4 个标的 · 展示前 3 个'), findsOneWidget);
      expect(
        tester.getTopLeft(preview).dy,
        lessThan(tester.getTopLeft(find.text('收益走势')).dy),
      );
      await _capture(tester, 'accounts-${size.width.toInt()}-fixture');
      await tester.ensureVisible(find.text('查看全部'));
      await tester.tap(find.text('查看全部'));
      await tester.pumpAndSettle();
      expect(find.text('AAPL'), findsOneWidget);
      expect(find.text('4,986.00'), findsOneWidget);
      await _capture(tester, 'stocks-${size.width.toInt()}-fixture');
      // 点击展开后，精确成本与数量保持服务端原字符串。
      await tester.tap(find.text('AAPL'));
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
    expect(find.text('18.65'), findsOneWidget);
    expect(find.text('20.40'), findsOneWidget);
    await Scrollable.ensureVisible(
      tester.element(find.text('385 看涨')),
      alignment: .4,
    );
    await tester.tap(find.text('385 看涨'));
    await tester.pumpAndSettle();
    expect(find.text('18.6500'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('首页收益切换调用对应报告，失败可重试且不隐藏账户金额', (tester) async {
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
    expect(find.text('489.16'), findsOneWidget);
    fixture.failures.add('drawdown');
    await Scrollable.ensureVisible(
      tester.element(find.text('累计')),
      alignment: .5,
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('累计'));
    await tester.pumpAndSettle();
    expect(fixture.requests.last.uri.path, LedgerView.drawdown.path);
    expect(fixture.requests.last.queryParameters['scope'], 'all');
    expect(find.text('+2,846.72'), findsOneWidget);
    expect(find.text('暂时无法读取数据，请重新加载。'), findsOneWidget);
    expect(find.textContaining('QUERY_FAILED'), findsNothing);
    fixture.failures.clear();
    await tester.ensureVisible(find.text('重新加载'));
    await tester.tap(find.text('重新加载'));
    await tester.pumpAndSettle();
    expect(find.text('暂时无法读取数据，请重新加载。'), findsNothing);
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
    await tester.pumpAndSettle();
    expect(fixture.requests.last.queryParameters['underlying'], 'AAPL');
    expect(find.byType(TextField), findsNothing);
    expect(find.text('标的 · AAPL'), findsOneWidget);
    expect(find.text('清除筛选'), findsNothing);
    // 收起编辑器保留已应用条件，未提交的输入不得改变查询。
    await tester.tap(find.byTooltip('搜索证券'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'NVDA');
    await tester.tap(find.byTooltip('收起搜索'));
    await tester.pumpAndSettle();
    expect(find.text('标的 · AAPL'), findsOneWidget);
    expect(fixture.requests.last.queryParameters['underlying'], 'AAPL');
    expect(fixture.requests.last.queryParameters['scope'], 'all');
    expect(find.text('NVDA'), findsNothing);

    await tester.tap(find.text('全部账号').last);
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
    fixture.failures.add('positions');
    await tester.tap(find.byTooltip('刷新数据'));
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

  testWidgets('交易分页使用服务端游标且筛选重置分页', (tester) async {
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
    await tester.ensureVisible(find.text('下一页'));
    await tester.tap(find.text('下一页'));
    await tester.pumpAndSettle();
    expect(fixture.requests.last.queryParameters['cursor'], 'next-page');
    expect(find.text('MSFT'), findsOneWidget);
    await tester.ensureVisible(find.byTooltip('搜索证券'));
    await tester.tap(find.byTooltip('搜索证券'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'AAPL');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pumpAndSettle();
    // 收起输入框会改变滚动布局；等待下一帧开始新查询后再核对请求。
    await tester.pump();
    await tester.pumpAndSettle();
    expect(
      fixture.requests.last.queryParameters.containsKey('cursor'),
      isFalse,
    );
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
      expect(
        find.textContaining('数据截至 2026年9月8日 08:00:00 UTC'),
        findsOneWidget,
      );
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
