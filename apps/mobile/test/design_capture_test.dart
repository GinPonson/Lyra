import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:dio/dio.dart';
import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_primary_icon.dart';
import 'package:lyra_mobile/core/preferences/app_preferences.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/import/import_sheet.dart';
import 'package:lyra_mobile/features/system/system_workspace.dart';

import 'session_flow_test.dart' show FakeLyraAdapter;
import 'support/ledger_fixture.dart';

// Opt-in review captures, independent of normal test runs and local SDK paths:
// flutter test test/design_capture_test.dart \
//   --dart-define=LYRA_REVIEW_DIR=/absolute/output/directory \
//   --dart-define=LYRA_CJK_FONT=/absolute/NotoSansCJKsc-Regular.otf \
//   --dart-define=LYRA_FLUTTER_ROOT=/absolute/flutter
// FLUTTER_ROOT can also be supplied by the environment that launches Flutter.
const _reviewDirectory = String.fromEnvironment('LYRA_REVIEW_DIR');
const _cjkFont = String.fromEnvironment('LYRA_CJK_FONT');
const _flutterRoot = String.fromEnvironment('LYRA_FLUTTER_ROOT');
const _standardFlutterRoot = String.fromEnvironment('FLUTTER_ROOT');
const _serverUrl = 'https://demo.lyra.example';
const _capturePrefix = String.fromEnvironment(
  'LYRA_CAPTURE_PREFIX',
  defaultValue: 'lyra-graphite',
);

/// Matches the approved mock's synthetic content for visual comparison.
/// All overrides stay inside this opt-in capture; production and shared business
/// fixtures never read these identities, dates, balances, or trade records.
class _DesignLedgerFixture extends LedgerFixture {
  @override
  Future<SessionDependencies> dependencies({bool paired = true}) async {
    final base = await super.dependencies(paired: paired);
    return SessionDependencies(
      credentialStore: base.credentialStore,
      clientFactory: (url) {
        final client = base.clientFactory!(url);
        // Synthetic import responses extend the same actual page flow; all
        // other requests still use LedgerFixture's in-memory HTTP adapter.
        client.interceptors.add(
          InterceptorsWrapper(
            onRequest: (request, handler) {
              final Object? body = switch (request.uri.path) {
                '/v1/imports/preview' => {
                  'importId': 'visual-import',
                  'confirmToken': 'synthetic-visual-confirmation',
                  'preview': {
                    'fileName': 'Schwab-transactions.csv',
                    'accountId': LedgerFixture.account,
                    'baseLedgerVersion': 12,
                    'totalRows': 3,
                    'insertableRows': 2,
                    'existingRows': 1,
                    'byAction': {'Buy': 2, 'Qualified Dividend': 1},
                  },
                },
                '/v1/imports/visual-import/commit' => {
                  'changed': true,
                  'ledgerVersion': 13,
                  'insertedRows': 2,
                  'skippedRows': 1,
                },
                _ => null,
              };
              if (body == null) {
                handler.next(request);
              } else {
                handler.resolve(
                  Response<Object?>(
                    requestOptions: request,
                    statusCode: 200,
                    data: body,
                  ),
                );
              }
            },
          ),
        );
        return client;
      },
    );
  }

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    final original = super.payload(command, query);
    if (command == 'accounts') {
      final accounts = (original['rows']! as List<Object?>)
          .cast<Map<String, Object?>>();
      return {
        ...original,
        'rows': [
          ...accounts,
          {
            ...accounts.first,
            'id': LedgerFixture.euroAccount,
            'displayName': '长期储备',
            'maskedSuffix': '••678',
          },
        ],
        'count': 2,
      };
    }
    if (command == 'trades') {
      final template =
          (original['rows']! as List<Object?>).first! as Map<String, Object?>;
      final examples = query['cursor'] == null
          ? [
              ('AAPL', 'Buy', '12', '-2143.32', '2026-10-16'),
              ('NVDA', 'Sell', '10', '1084.38', '2026-10-16'),
            ]
          : [
              ('MSFT', 'Buy', '4', '-1662.00', '2026-10-15'),
              ('VOO', 'Buy', '2', '-1027.38', '2026-10-15'),
              ('AAPL', 'Sell', '2', '121.06', '2026-10-12'),
              ('NVDA', 'Buy', '6', '-653.28', '2026-10-12'),
            ];
      return {
        ...original,
        'rows': [
          for (final item in examples)
            {
              ...template,
              'symbol': item.$1,
              'underlying': item.$1,
              'action': item.$2,
              'quantity': item.$3,
              'amount': item.$4,
              'transactionDate': item.$5,
              'recordDate': item.$5,
            },
        ],
        'count': examples.length,
      };
    }
    if (command == 'drawdown') {
      return {
        ...original,
        'groups': (original['groups']! as List<Object?>).map((entry) {
          final group = entry! as Map<String, Object?>;
          return {
            ...group,
            'data': {
              ...group['data']! as Map<String, Object?>,
              'maxDrawdown': '412.35',
              'rows': [
                for (final (index, item) in [
                  ('320.18', '320.18', '320.18', '0.00'),
                  ('836.38', '516.20', '836.38', '0.00'),
                  ('2039.55', '1203.17', '2039.55', '0.00'),
                  ('1627.20', '-412.35', '2039.55', '412.35'),
                  ('1973.16', '345.96', '2039.55', '66.39'),
                  ('2846.72', '873.56', '2846.72', '0.00'),
                ].indexed)
                  {
                    'date': '2026-${(index + 5).toString().padLeft(2, '0')}-01',
                    'dailyPnl': item.$2,
                    'cumulativePnl': item.$1,
                    'runningPeak': item.$3,
                    'drawdown': item.$4,
                  },
              ],
            },
          };
        }).toList(),
      };
    }
    if (command != 'summary') return original;
    return {
      ...original,
      'groups': (original['groups']! as List<Object?>).map((entry) {
        final group = entry! as Map<String, Object?>;
        final data = group['data']! as Map<String, Object?>;
        return {
          ...group,
          'data': {...data, 'cashBalance': '10539.64'},
        };
      }).toList(),
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

Dio _systemClient(String url) => Dio(BaseOptions(baseUrl: url))
  ..httpClientAdapter = FakeLyraAdapter((options) async {
    final response = switch (options.uri.path) {
      '/v1/system/info' => {
        'server_version': '0.1.0',
        'protocol_version': 3,
        'min_supported_protocol_version': 3,
        'max_supported_protocol_version': 3,
        'schema_version': 2,
      },
      '/v1/health' => {'status': 'healthy'},
      _ => throw StateError(
        'Unexpected visual fixture request: ${options.uri}',
      ),
    };
    return ResponseBody.fromString(
      jsonEncode(response),
      200,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  });

Future<void> _selectDestination(WidgetTester tester, String label) async {
  final destination = find.descendant(
    of: find.byType(NavigationBar),
    matching: find.text(label),
  );
  expect(destination, findsOneWidget);
  await tester.tap(destination);
  await tester.pumpAndSettle();
  expect(tester.takeException(), isNull);
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
        Directory(_reviewDirectory).uri.resolve('$_capturePrefix-$name.png'),
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
      'design captures (set LYRA_REVIEW_DIR and LYRA_CJK_FONT to enable)',
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
    await _loadFont('Geist', [
      for (final weight in [400, 500, 600, 700])
        rootBundle.load('assets/fonts/geist-$weight.ttf'),
    ]);
    await _loadFont('Geist Mono', [
      for (final weight in [400, 500, 600, 700])
        rootBundle.load('assets/fonts/geist-mono-$weight.ttf'),
    ]);
    final cjkData = await cjk.readAsBytes();
    for (final family in ['Noto Sans CJK SC', 'PingFang SC']) {
      await _loadFont(family, [Future.value(ByteData.sublistView(cjkData))]);
    }
    await _loadFont('MaterialIcons', [
      material.readAsBytes().then(ByteData.sublistView),
    ]);
  });

  for (final scenario in const [
    (name: '390', size: Size(390, 844), scale: 1.0, signedIn: true),
    (name: '320-large', size: Size(320, 740), scale: 1.6, signedIn: true),
    (name: '390-signed-out', size: Size(390, 844), scale: 1.0, signedIn: false),
    (name: '1440', size: Size(1440, 960), scale: 1.0, signedIn: true),
  ]) {
    testWidgets('capture design ${scenario.name}', (tester) async {
      tester.view.physicalSize = scenario.size;
      tester.view.devicePixelRatio = 1;
      tester.platformDispatcher.textScaleFactorTestValue = scenario.scale;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);

      final fixture = _DesignLedgerFixture();
      final dependencies = await fixture.dependencies(
        paired: scenario.signedIn,
      );
      if (scenario.signedIn) {
        await dependencies.credentialStore.write(
          const CredentialSnapshot(
            serverUrl: _serverUrl,
            deviceId: 'visual-qa-device',
            accessToken: 'synthetic-visual-token',
            refreshToken: 'synthetic-visual-refresh',
            userId: 'synthetic-visual-user',
            authMethod: 'google',
            userEmail: 'demo@example.com',
            googleSubject: 'synthetic-visual-user',
          ),
        );
      }
      final boundaryKey = GlobalKey();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            sessionDependenciesProvider.overrideWithValue(dependencies),
            importFilePickerProvider.overrideWithValue(
              (_) async => XFile.fromData(
                Uint8List.fromList(utf8.encode('Date,Action\n')),
                name: 'Schwab-transactions.csv',
                mimeType: 'text/csv',
              ),
            ),
            appPreferencesStoreProvider.overrideWithValue(
              MemoryAppPreferencesStore(
                initialValue: const AppPreferences(
                  rememberedServerUrl: _serverUrl,
                ),
              ),
            ),
            appVersionProvider.overrideWith((ref) async => '1.0.0 (3)'),
            systemDioFactoryProvider.overrideWithValue(_systemClient),
          ],
          child: RepaintBoundary(key: boundaryKey, child: const LyraApp()),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      // Confirm the scaling that the real MaterialApp sees, not an outer
      // MediaQuery that MaterialApp/View would replace during app construction.
      final appContext = tester.element(find.byType(Scaffold).first);
      expect(
        MediaQuery.textScalerOf(appContext).scale(16),
        closeTo(16 * scenario.scale, 0.001),
      );
      await _saveCapture(tester, boundaryKey, 'overview-${scenario.name}');

      if (scenario.signedIn && scenario.name != '1440') {
        await _selectDestination(tester, '股票');
        await _saveCapture(tester, boundaryKey, 'stocks-${scenario.name}');
        await _selectDestination(tester, '交易');
        await _saveCapture(tester, boundaryKey, 'ledger-${scenario.name}');
      }

      if (scenario.signedIn && scenario.name != '1440') {
        await _selectDestination(tester, '我的');
        await _saveCapture(tester, boundaryKey, 'profile-${scenario.name}');

        // 所有二级页都通过正式入口打开；只填写合成表单，不创建真实账户。
        final accounts = find.byKey(const ValueKey('profile-accounts'));
        await tester.ensureVisible(accounts);
        await tester.tap(accounts);
        await _saveCapture(tester, boundaryKey, 'accounts-${scenario.name}');
        await tester.tap(find.byKey(const ValueKey('profile-add-account')));
        await tester.pumpAndSettle();
        await tester.enterText(
          find.byKey(const ValueKey('account-name')),
          'Schwab 主账户',
        );
        await tester.enterText(
          find.byKey(const ValueKey('account-identifier')),
          'XXX000',
        );
        FocusManager.instance.primaryFocus?.unfocus();
        await _saveCapture(
          tester,
          boundaryKey,
          'create-account-${scenario.name}',
        );
        await tester.ensureVisible(find.byTooltip('关闭'));
        await tester.tap(find.byTooltip('关闭'));
        await tester.pumpAndSettle();
        await tester.pageBack();
        await tester.pumpAndSettle();

        for (final sheet in [
          (key: 'profile-startup-account', name: 'startup-account'),
          (key: 'profile-profit-colors', name: 'profit-colors'),
          (key: 'profile-market-data', name: 'market-data'),
        ]) {
          final row = find.byKey(ValueKey(sheet.key));
          await tester.ensureVisible(row);
          await tester.tap(row);
          await _saveCapture(
            tester,
            boundaryKey,
            '${sheet.name}-${scenario.name}',
          );
          await tester.ensureVisible(find.byTooltip('关闭'));
          await tester.tap(find.byTooltip('关闭'));
          await tester.pumpAndSettle();
          expect(tester.takeException(), isNull);
        }

        final about = find.byKey(const ValueKey('profile-about'));
        await tester.ensureVisible(about);
        await tester.tap(about);
        await _saveCapture(tester, boundaryKey, 'about-${scenario.name}');
        await tester.pageBack();
        await tester.pumpAndSettle();
      }

      if (scenario.name == '390') {
        final profileIcon = find.descendant(
          of: find.byType(NavigationBar),
          matching: find.byWidgetPredicate(
            (widget) =>
                widget is LyraPrimaryIcon &&
                widget.glyph == LyraPrimaryGlyph.profile,
          ),
        );
        // 保持按下与鼠标悬停，才能捕获静态截图看不到的选中底色和 ink 状态层叠加。
        final press = await tester.startGesture(tester.getCenter(profileIcon));
        await _saveCapture(tester, boundaryKey, 'navigation-pressed-390');
        await press.up();
        await tester.pumpAndSettle();
        final mouse = await tester.createGesture(
          kind: ui.PointerDeviceKind.mouse,
        );
        await mouse.addPointer(location: Offset.zero);
        await mouse.moveTo(tester.getCenter(profileIcon));
        await _saveCapture(tester, boundaryKey, 'navigation-hovered-390');
        await mouse.removePointer();
        await tester.pumpAndSettle();

        await _selectDestination(tester, '总览');
        await tester.tap(find.byKey(const ValueKey('toggle-amounts')));
        await _saveCapture(tester, boundaryKey, 'overview-hidden-390');
        await tester.tap(find.byKey(const ValueKey('toggle-amounts')));
        await tester.tap(find.byTooltip('导入文件'));
        await _saveCapture(tester, boundaryKey, 'import-390');
        await tester.tap(find.text('选择文件并预览'));
        await _saveCapture(tester, boundaryKey, 'import-preview-390');
        await tester.tap(find.text('确认导入'));
        await _saveCapture(tester, boundaryKey, 'import-complete-390');
        Navigator.of(tester.element(find.byType(ImportSheet))).pop();
        await tester.pumpAndSettle();
        await _selectDestination(tester, '交易');
        fixture.failures.add('trades');
        // 顶部不再常驻刷新按钮；使用正式页面的下拉刷新入口验证错误状态。
        await tester.drag(
          find.byKey(const PageStorageKey('ledger-scroll')),
          const Offset(0, 420),
        );
        await _saveCapture(tester, boundaryKey, 'load-error-390');
      }

      if (scenario.name == '320-large') {
        await _selectDestination(tester, '总览');
        await tester.tap(find.byTooltip('导入文件'));
        await _saveCapture(tester, boundaryKey, 'import-320-large');
        await tester.tap(find.text('选择文件并预览'));
        await _saveCapture(tester, boundaryKey, 'import-preview-320-large');
        final confirm = find.text('确认导入');
        await tester.ensureVisible(confirm);
        await tester.tap(confirm);
        await _saveCapture(tester, boundaryKey, 'import-complete-320-large');
        Navigator.of(tester.element(find.byType(ImportSheet))).pop();
        await tester.pumpAndSettle();
      }

      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }
}
