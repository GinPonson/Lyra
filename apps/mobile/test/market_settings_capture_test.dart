import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_colors.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;
import 'support/market_settings_fixture.dart';

// Opt-in actual Flutter captures. Provider names below are display-only fixture
// labels; HTTP is entirely synthetic and no live market request is performed.
// flutter test test/market_settings_capture_test.dart \
//   --dart-define=LYRA_REVIEW_DIR=/absolute/output \
//   --dart-define=LYRA_CJK_FONT=/absolute/NotoSansCJKsc-Regular.otf \
//   --dart-define=LYRA_FLUTTER_ROOT=/absolute/flutter
const _directory = String.fromEnvironment('LYRA_REVIEW_DIR');
const _cjkFont = String.fromEnvironment('LYRA_CJK_FONT');
const _flutterRoot = String.fromEnvironment('LYRA_FLUTTER_ROOT');

Future<void> _font(String family, Iterable<Future<ByteData>> files) async {
  final loader = FontLoader(family);
  for (final file in files) {
    loader.addFont(file);
  }
  await loader.load();
}

Dio _system(String url) =>
    Dio(BaseOptions(baseUrl: url))
      ..httpClientAdapter = FakeLyraAdapter(
        (options) async => ResponseBody.fromString(
          jsonEncode(
            options.uri.path == '/v1/health'
                ? {'status': 'healthy'}
                : {
                    'server_version': '0.1.0',
                    'protocol_version': 2,
                    'min_supported_protocol_version': 2,
                    'max_supported_protocol_version': 2,
                    'schema_version': 14,
                  },
          ),
          200,
          headers: {
            Headers.contentTypeHeader: [Headers.jsonContentType],
          },
        ),
      );

Finder _field(String asset, {bool backup = false}) => find.byWidgetPredicate(
  (widget) =>
      widget is DropdownButtonFormField<String> &&
      widget.key is ValueKey<String> &&
      (widget.key! as ValueKey<String>).value.startsWith(
        'market-source-$asset-${backup ? 'backup' : 'primary'}-',
      ),
);

Future<void> _choose(
  WidgetTester tester,
  String asset,
  String value, {
  bool backup = false,
}) async {
  await tester.ensureVisible(_field(asset, backup: backup));
  await tester.tap(_field(asset, backup: backup));
  await tester.pumpAndSettle();
  final item = find.byWidgetPredicate(
    (widget) => widget is DropdownMenuItem<String> && widget.value == value,
  );
  await tester.tap(find.descendant(of: item, matching: find.byType(Text)));
  await tester.pumpAndSettle();
}

Future<void> _capture(WidgetTester tester, GlobalKey key, String name) async {
  await tester.pumpAndSettle();
  expect(tester.takeException(), isNull, reason: name);
  final boundary =
      key.currentContext!.findRenderObject()! as RenderRepaintBoundary;
  await tester.runAsync(() async {
    final image = await boundary.toImage(pixelRatio: 2);
    try {
      final data = await image.toByteData(format: ui.ImageByteFormat.png);
      await File('$_directory/market-settings-20261011-$name.png').writeAsBytes(
        data!.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes),
        flush: true,
      );
    } finally {
      image.dispose();
    }
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  if (_directory.isEmpty || _cjkFont.isEmpty || _flutterRoot.isEmpty) {
    testWidgets(
      'market settings captures (opt in with LYRA_REVIEW_DIR, LYRA_CJK_FONT, LYRA_FLUTTER_ROOT)',
      (_) async {},
      skip: true,
    );
    return;
  }
  setUpAll(() async {
    await Directory(_directory).create(recursive: true);
    for (final (family, prefix) in [
      ('Geist', 'geist'),
      ('Geist Mono', 'geist-mono'),
    ]) {
      await _font(family, [
        for (final weight in [400, 500, 600, 700])
          rootBundle.load('assets/fonts/$prefix-$weight.ttf'),
      ]);
    }
    final cjk = await File(_cjkFont).readAsBytes();
    for (final family in ['Noto Sans CJK SC', 'PingFang SC']) {
      await _font(family, [Future.value(ByteData.sublistView(cjk))]);
    }
    await _font('MaterialIcons', [
      File(
        '$_flutterRoot/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf',
      ).readAsBytes().then(ByteData.sublistView),
    ]);
  });
  for (final scenario in const [
    (name: '390', size: Size(390, 844), scale: 1.0),
    (name: '320-large', size: Size(320, 740), scale: 1.6),
  ]) {
    testWidgets('capture actual market settings ${scenario.name}', (
      tester,
    ) async {
      tester.view.physicalSize = scenario.size;
      tester.view.devicePixelRatio = 1;
      tester.platformDispatcher.textScaleFactorTestValue = scenario.scale;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final settings = MarketSettingsFixture.settingsPayload();
      final providers = settings['providers']! as List;
      (providers[0] as Map)['displayName'] = 'Alpaca';
      (providers[1] as Map)['displayName'] = 'MarketData';
      final fixture = MarketSettingsFixture(initialSettings: settings);
      final dependencies = await fixture.dependencies();
      final boundaryKey = GlobalKey();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
            appPreferencesStoreProvider.overrideWithValue(
              MemoryAppPreferencesStore(),
            ),
            appVersionProvider.overrideWith((ref) async => '1.0.0 (3)'),
            systemDioFactoryProvider.overrideWithValue(_system),
          ],
          child: RepaintBoundary(
            key: boundaryKey,
            child: Directionality(
              textDirection: TextDirection.ltr,
              child: Banner(
                message: '演示配置',
                location: BannerLocation.topEnd,
                color: LyraColors.accentTint,
                textStyle: const TextStyle(
                  fontFamily: 'Geist',
                  fontFamilyFallback: ['Noto Sans CJK SC'],
                  color: LyraColors.ink,
                  fontSize: 10,
                ),
                child: const LyraApp(),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(
        find.descendant(
          of: find.byType(NavigationBar),
          matching: find.text('我的'),
        ),
      );
      await tester.pumpAndSettle();
      final entry = find.byKey(const ValueKey('profile-market-data'));
      await tester.ensureVisible(entry);
      await tester.tap(entry);
      await tester.pumpAndSettle();
      await _choose(tester, 'stock', 'second-feed', backup: true);
      if (scenario.name == '390') {
        await tester.ensureVisible(find.byTooltip('关闭'));
        await _capture(tester, boundaryKey, scenario.name);
        await tester.tap(_field('stock'));
        await tester.pumpAndSettle();
        await _capture(tester, boundaryKey, '390-picker');
      } else {
        await Scrollable.ensureVisible(
          tester.element(find.byKey(const ValueKey('market-settings-save'))),
          alignment: 1,
        );
        await _capture(tester, boundaryKey, scenario.name);
      }
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }
}
