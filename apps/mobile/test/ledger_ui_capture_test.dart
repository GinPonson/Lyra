import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/ledger/ledger_screen.dart';
import 'package:lyra_mobile/features/ledger/ledger_view.dart';

import 'support/ledger_fixture.dart';

// Opt-in real Flutter captures; ordinary test runs do not write images.
// flutter test test/ledger_ui_capture_test.dart \
//   --dart-define=LYRA_REVIEW_DIR=/absolute/output/directory \
//   --dart-define=LYRA_CJK_FONT=/absolute/NotoSansCJKsc-Regular.otf \
//   --dart-define=LYRA_FLUTTER_ROOT=/absolute/flutter
const _reviewDirectory = String.fromEnvironment('LYRA_REVIEW_DIR');
const _cjkFont = String.fromEnvironment('LYRA_CJK_FONT');
const _flutterRoot = String.fromEnvironment('LYRA_FLUTTER_ROOT');
const _standardFlutterRoot = String.fromEnvironment('FLUTTER_ROOT');

/// A future status remains visibly unconfirmed even if the summary says ok.
/// All other account and report data comes from the shared synthetic fixture.
class _UnknownAuditFixture extends LedgerFixture {
  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    if (command != 'audit') return super.payload(command, query);
    return {
      'rows': [
        {
          'check': 'future_internal_check_v2',
          'status': {'new_status': 'internal_status_v2'},
          'count': 0,
        },
      ],
      'ok': true,
      'failed': 0,
    };
  }
}

Future<void> _loadFont(String family, Iterable<Future<ByteData>> files) async {
  final loader = FontLoader(family);
  for (final file in files) {
    loader.addFont(file);
  }
  await loader.load();
}

String _materialFontPath() {
  final root =
      [
        _flutterRoot,
        _standardFlutterRoot,
        Platform.environment['LYRA_FLUTTER_ROOT'] ?? '',
        Platform.environment['FLUTTER_ROOT'] ?? '',
      ].firstWhere(
        (value) => value.isNotEmpty,
        orElse: () => throw StateError(
          'Set LYRA_FLUTTER_ROOT or FLUTTER_ROOT to load MaterialIcons for captures.',
        ),
      );
  return Directory(root).uri
      .resolve('bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf')
      .toFilePath();
}

Future<void> _saveCapture(
  WidgetTester tester,
  GlobalKey boundaryKey,
  String name,
) async {
  await tester.runAsync(() async {
    for (final element in find.byType(Image).evaluate()) {
      await precacheImage((element.widget as Image).image, element);
    }
  });
  await tester.pumpAndSettle();
  expect(tester.takeException(), isNull, reason: 'Layout exception in $name');
  final boundary =
      boundaryKey.currentContext!.findRenderObject()! as RenderRepaintBoundary;
  await tester.runAsync(() async {
    final picture = await boundary.toImage(pixelRatio: 2);
    try {
      final data = await picture.toByteData(format: ui.ImageByteFormat.png);
      if (data == null) throw StateError('PNG encoding failed for $name');
      final output = File.fromUri(
        Directory(_reviewDirectory).uri
            .resolve('user-ui-20261011-ledger-$name.png'),
      );
      await output.writeAsBytes(
        data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes),
        flush: true,
      );
    } finally {
      picture.dispose();
    }
  });
  expect(tester.takeException(), isNull, reason: 'Capture exception in $name');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  if (_reviewDirectory.isEmpty || _cjkFont.isEmpty) {
    testWidgets(
      'ledger captures (set LYRA_REVIEW_DIR and LYRA_CJK_FONT to enable)',
      (_) async {},
      skip: true,
    );
    return;
  }

  setUpAll(() async {
    final cjk = File(_cjkFont);
    if (!await cjk.exists()) {
      throw StateError('LYRA_CJK_FONT does not exist: $_cjkFont');
    }
    final material = File(_materialFontPath());
    if (!await material.exists()) {
      throw StateError('MaterialIcons font does not exist: ${material.path}');
    }
    await Directory(_reviewDirectory).create(recursive: true);
    for (final (family, prefix) in [
      ('Geist', 'geist'),
      ('Geist Mono', 'geist-mono'),
    ]) {
      await _loadFont(family, [
        for (final weight in [400, 500, 600, 700])
          rootBundle.load('assets/fonts/$prefix-$weight.ttf'),
      ]);
    }
    final cjkData = await cjk.readAsBytes();
    for (final family in ['Noto Sans CJK SC', 'PingFang SC']) {
      await _loadFont(family, [Future.value(ByteData.sublistView(cjkData))]);
    }
    await _loadFont('MaterialIcons', [
      material.readAsBytes().then(ByteData.sublistView),
    ]);
  });

  for (final scenario in const [
    (
      name: 'audit-390',
      view: LedgerView.audit,
      size: Size(390, 844),
      scale: 1.0,
      unknownStatus: false,
    ),
    (
      name: 'holding-period-390',
      view: LedgerView.holdingPeriod,
      size: Size(390, 844),
      scale: 1.0,
      unknownStatus: false,
    ),
    (
      name: 'holding-period-320-large',
      view: LedgerView.holdingPeriod,
      size: Size(320, 740),
      scale: 1.6,
      unknownStatus: false,
    ),
    (
      name: 'audit-unknown-status-390',
      view: LedgerView.audit,
      size: Size(390, 844),
      scale: 1.0,
      unknownStatus: true,
    ),
  ]) {
    testWidgets('capture ledger UI ${scenario.name}', (tester) async {
      tester.view.physicalSize = scenario.size;
      tester.view.devicePixelRatio = 1;
      tester.platformDispatcher.textScaleFactorTestValue = scenario.scale;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);

      final fixture = scenario.unknownStatus
          ? _UnknownAuditFixture()
          : LedgerFixture();
      final dependencies = await fixture.dependencies();
      final boundaryKey = GlobalKey();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
            appPreferencesStoreProvider.overrideWithValue(
              MemoryAppPreferencesStore(),
            ),
          ],
          child: RepaintBoundary(
            key: boundaryKey,
            child: MaterialApp(
              debugShowCheckedModeBanner: false,
              theme: LyraTheme.dark,
              home: ReportScreen(view: scenario.view),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(
        MediaQuery.textScalerOf(tester.element(find.byType(Scaffold)))
            .scale(16),
        closeTo(16 * scenario.scale, 0.001),
      );
      final focus = find.text(
        scenario.view == LedgerView.holdingPeriod
            ? '8–30 天'
            : scenario.unknownStatus
            ? '其他账本检查'
            : '原始交易记录',
      );
      await tester.ensureVisible(focus);
      await tester.tap(focus);
      await tester.pumpAndSettle();
      if (scenario.scale > 1) {
        await Scrollable.ensureVisible(tester.element(focus), alignment: .15);
        await tester.pumpAndSettle();
      }
      expect(find.textContaining('账本 v'), findsNothing);
      expect(find.textContaining(LedgerFixture.timestamp), findsNothing);
      if (scenario.unknownStatus) {
        expect(find.text('部分检查结果待确认'), findsOneWidget);
        expect(find.text('账本检查通过'), findsNothing);
        expect(find.textContaining('internal_'), findsNothing);
      }
      await _saveCapture(tester, boundaryKey, scenario.name);

      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }
}
