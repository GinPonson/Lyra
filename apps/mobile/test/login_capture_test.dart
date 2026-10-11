import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/features/auth/auth_provider.dart';

import 'support/login_fixture.dart';

const _directory = String.fromEnvironment('LYRA_REVIEW_DIR');
const _font = String.fromEnvironment('LYRA_CJK_FONT');
const _flutter = String.fromEnvironment('LYRA_FLUTTER_ROOT');

Future<void> _loadFont(String name, List<Future<ByteData>> data) async {
  final loader = FontLoader(name);
  for (final bytes in data) {
    loader.addFont(bytes);
  }
  await loader.load();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  if (_directory.isEmpty || _font.isEmpty || _flutter.isEmpty) {
    testWidgets(
      'login captures (set LYRA_REVIEW_DIR, LYRA_CJK_FONT and LYRA_FLUTTER_ROOT)',
      (_) async {},
      skip: true,
    );
    return;
  }
  setUpAll(() async {
    final fontData = ByteData.sublistView(await File(_font).readAsBytes());
    for (final family in ['Noto Sans CJK SC', 'PingFang SC']) {
      await _loadFont(family, [Future.value(fontData)]);
    }
    await _loadFont('Geist', [
      for (final weight in [400, 500, 600, 700])
        rootBundle.load('assets/fonts/geist-$weight.ttf'),
    ]);
    await _loadFont('MaterialIcons', [
      File(
        '$_flutter/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf',
      ).readAsBytes().then(ByteData.sublistView),
    ]);
    await Directory(_directory).create(recursive: true);
  });
  for (final scenario in [
    (name: '390', size: const Size(390, 844), scale: 1.0, unavailable: false),
    (
      name: '320-large',
      size: const Size(320, 740),
      scale: 1.6,
      unavailable: false,
    ),
    (
      name: '390-unavailable',
      size: const Size(390, 844),
      scale: 1.0,
      unavailable: true,
    ),
  ]) {
    testWidgets('capture dedicated login ${scenario.name}', (tester) async {
      tester.view.physicalSize = scenario.size;
      tester.view.devicePixelRatio = 1;
      tester.platformDispatcher.textScaleFactorTestValue = scenario.scale;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      final fixture = LoginFixture();
      if (scenario.unavailable) fixture.available = {AuthProvider.github};
      final key = GlobalKey();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(
              await fixture.dependencies(),
            ),
            appPreferencesStoreProvider.overrideWithValue(
              MemoryAppPreferencesStore(
                initialValue: const AppPreferences(
                  rememberedServerUrl: loginFixtureServer,
                ),
              ),
            ),
          ],
          child: RepaintBoundary(key: key, child: const LyraApp()),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.runAsync(() async {
        for (final element in find.byType(Image).evaluate()) {
          await precacheImage((element.widget as Image).image, element);
        }
      });
      await tester.pumpAndSettle();
      final boundary =
          key.currentContext!.findRenderObject()! as RenderRepaintBoundary;
      await tester.runAsync(() async {
        final image = await boundary.toImage(pixelRatio: 2);
        try {
          final data = (await image.toByteData(
            format: ui.ImageByteFormat.png,
          ))!;
          await File('$_directory/login-20261011-${scenario.name}.png')
              .writeAsBytes(
                data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes),
                flush: true,
              );
        } finally {
          image.dispose();
        }
      });
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }
}
