import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/app/lyra_app.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/design/lyra_theme.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/accounts/account_repository.dart';
import 'package:lyra_mobile/features/accounts/account_sheet.dart';
import 'package:lyra_mobile/features/agent/agent_session_store.dart';
import 'package:lyra_mobile/features/import/import_sheet.dart';
import 'package:lyra_mobile/features/ledger/ledger_workspace.dart';

import 'support/ledger_fixture.dart';

const _fileName = 'Individual_XXX276_Transactions_20261011.csv';
final _fileBytes = Uint8List.fromList(
  utf8.encode('Date,Action\n10/10/2026,Buy\n'),
);
const _snapshot = CredentialSnapshot(
  serverUrl: 'https://fixture.invalid',
  deviceId: 'test-device',
  accessToken: 'test-access',
  refreshToken: 'test-refresh',
  userId: 'account-user',
  authMethod: 'google',
);

ResponseBody _json(Object body, [int status = 200]) => ResponseBody.fromString(
  jsonEncode(body),
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);

class _AccountAdapter implements HttpClientAdapter {
  _AccountAdapter(this.fixture);
  final _FirstUseFixture fixture;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (options.uri.path == '/v1/imports/preview' && requestStream != null) {
      fixture.uploads.add(
        await requestStream.expand((bytes) => bytes).toList(),
      );
    }
    return fixture.handle(options);
  }

  @override
  void close({bool force = false}) {}
}

/// 模拟空账本到第一笔导入的真实 HTTP 契约，不在生产路径提供演示数据。
class _FirstUseFixture extends LedgerFixture {
  final store = MemoryCredentialStore();
  bool created = false;
  bool committed = false;
  bool loseFirstCreateResponse = false;
  bool duplicateOnCreate = false;
  bool failFirstMappedPreview = false;
  Completer<ResponseBody>? delayedCreate;
  Completer<ResponseBody>? delayedAccounts;
  String accountName = 'Schwab 主账户';
  int filePicks = 0;
  final createKeys = <String>[];
  final createBodies = <Map<String, Object?>>[];
  final previewAccounts = <String?>[];
  final uploads = <List<int>>[];

  Map<String, Object?> get account => {
    'id': LedgerFixture.account,
    'displayName': accountName,
    'broker': 'schwab',
    'maskedSuffix': '276',
    'baseCurrency': 'USD',
    'status': 'active',
    'createdAt': LedgerFixture.timestamp,
    'updatedAt': LedgerFixture.timestamp,
  };

  Future<ResponseBody> handle(RequestOptions options) async {
    requests.add(options);
    // 适配器回调可能与测试的 pump 同时执行，使用同步断言保持协议校验。
    expectSync(options.headers['x-lyra-protocol-version'], '3');
    final path = options.uri.path;
    if (path == '/v1/auth/logout') return ResponseBody.fromString('', 204);
    if (path == '/v1/ledger/accounts' && options.method == 'POST') {
      createKeys.add(options.headers['idempotency-key'] as String);
      final body = options.data is Map
          ? (options.data as Map).cast<String, Object?>()
          : (jsonDecode(options.data as String) as Map).cast<String, Object?>();
      createBodies.add(body);
      accountName = body['displayName'] as String;
      if (delayedCreate != null) return delayedCreate!.future;
      created = true;
      if (duplicateOnCreate) {
        return _json({
          'code': 'ACCOUNT_ALREADY_EXISTS',
          'message': '证券账户已存在',
          'retryable': false,
        }, 409);
      }
      if (loseFirstCreateResponse && createKeys.length == 1) {
        // 服务端已经创建，客户端没有收到响应；重试须复用同一键。
        throw DioException(
          requestOptions: options,
          type: DioExceptionType.receiveTimeout,
        );
      }
      return _json({'account': account}, 201);
    }
    if (path == '/v1/ledger/accounts' && delayedAccounts != null) {
      return delayedAccounts!.future;
    }
    if (path == '/v1/imports/preview') {
      expectSync(options.headers['x-lyra-file-name'], _fileName);
      previewAccounts.add(options.uri.queryParameters['accountId']);
      if (!created) {
        return _json({
          'code': 'ACCOUNT_MAPPING_REQUIRED',
          'message': '请选择该文件对应的证券账户',
          'retryable': false,
          'details': {'candidate_account_ids': <String>[]},
        }, 409);
      }
      if (failFirstMappedPreview &&
          options.uri.queryParameters['accountId'] != null) {
        failFirstMappedPreview = false;
        return _json({
          'code': 'SERVER_BUSY',
          'message': '暂时无法解析文件',
          'retryable': true,
        }, 503);
      }
      return _json({
        'importId': 'i1000000-0000-4000-8000-000000000001',
        'confirmToken': 'test-confirmation',
        'preview': {
          'fileName': _fileName,
          'accountId': LedgerFixture.account,
          'baseLedgerVersion': 0,
          'totalRows': 1,
          'insertableRows': 1,
          'existingRows': 0,
          'byAction': {'Buy': 1},
        },
      }, 201);
    }
    if (path.endsWith('/commit')) {
      committed = true;
      return _json({
        'changed': true,
        'ledgerVersion': 1,
        'insertedRows': 1,
        'skippedRows': 0,
      });
    }
    return _json(response(path.split('/').last, options.queryParameters));
  }

  @override
  Map<String, Object?> response(String command, Map<String, dynamic> query) {
    final body = super.response(command, query);
    (body['metadata'] as Map<String, Object?>)['ledgerVersion'] = committed
        ? 1
        : 0;
    return body;
  }

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    if (command == 'accounts') {
      return {
        'rows': created ? [account] : <Object?>[],
        'count': created ? 1 : 0,
      };
    }
    if (command == 'summary' && !committed) {
      return {'aggregateCurrency': null, 'groups': <Object?>[]};
    }
    return super.payload(command, query);
  }

  @override
  Future<SessionDependencies> dependencies({bool paired = true}) async {
    if (paired) await store.write(_snapshot);
    return SessionDependencies(
      credentialStore: store,
      agentSessionStore: MemoryAgentSessionStore(),
      clientFactory: (url) =>
          Dio(BaseOptions(baseUrl: url))
            ..httpClientAdapter = _AccountAdapter(this),
    );
  }

  Future<XFile?> pickFile(List<XTypeGroup> types) async {
    filePicks++;
    return XFile.fromData(
      _fileBytes,
      path: _fileName,
      name: _fileName,
      mimeType: 'text/csv',
    );
  }
}

class _ImportHarness extends ConsumerWidget {
  const _ImportHarness();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    final scope = ref.watch(accountScopeProvider);
    return Scaffold(
      body: SingleChildScrollView(
        child: Column(
          children: [
            Text(session.value is SessionAuthenticated ? 'Google 已登录' : '未登录'),
            Text('scope:${scope.id}', key: const ValueKey('scope-probe')),
            const ImportSheet(),
          ],
        ),
      ),
    );
  }
}

Future<void> _pumpFixture(
  WidgetTester tester,
  _FirstUseFixture fixture, {
  bool fullApp = false,
  ImportFilePicker? picker,
}) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dependencies = await fixture.dependencies();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionDependenciesProvider.overrideWithValue(dependencies),
        importFilePickerProvider.overrideWithValue(picker ?? fixture.pickFile),
      ],
      child: fullApp
          ? const LyraApp()
          : MaterialApp(theme: LyraTheme.dark, home: const _ImportHarness()),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _tap(WidgetTester tester, Finder target) async {
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pumpAndSettle();
}

Future<void> _fillAccount(WidgetTester tester, {bool fromFile = false}) async {
  await tester.enterText(
    find.byKey(const ValueKey('account-name')),
    'Schwab 主账户',
  );
  if (fromFile) {
    expect(
      tester
          .widget<TextFormField>(
            find.byKey(const ValueKey('account-identifier')),
          )
          .controller!
          .text,
      'XXX276',
    );
  } else {
    await tester.enterText(
      find.byKey(const ValueKey('account-identifier')),
      'xxx276',
    );
  }
}

Future<void> _openImportCreation(WidgetTester tester) async {
  await _tap(tester, find.text('选择文件并预览'));
  expect(find.textContaining('所选文件会保留'), findsOneWidget);
  await _tap(tester, find.text('新增证券账户'));
  await _fillAccount(tester, fromFile: true);
}

Future<void> _replaceIdentity(
  WidgetTester tester,
  _FirstUseFixture fixture,
  ProviderContainer container,
) async {
  final logout = expectLater(
    container.read(sessionProvider.notifier).signOut(),
    completes,
  );
  await tester.pumpAndSettle();
  await logout;
  await fixture.store.write(
    const CredentialSnapshot(
      serverUrl: 'https://fixture.invalid',
      deviceId: 'new-device',
      accessToken: 'new-access',
      refreshToken: 'new-refresh',
      userId: 'other-user',
      authMethod: 'google',
    ),
  );
  container.invalidate(sessionProvider);
  await tester.pumpAndSettle();
}

void main() {
  test('账户请求规范化且同一逻辑重试保留幂等键，修改表单生成新请求', () {
    final first = AccountCreationRequest(
      displayName: ' 主账户 ',
      accountIdentifier: ' xxx276 ',
      baseCurrency: ' usd ',
    );
    expect(first.body, {
      'displayName': '主账户',
      'accountIdentifier': 'XXX276',
      'baseCurrency': 'USD',
    });
    expect(first.idempotencyKey, matches(RegExp(r'^[A-Za-z0-9._:-]{16,200}$')));
    expect(
      first.matches(
        displayName: '主账户',
        accountIdentifier: 'xxx276',
        baseCurrency: 'USD',
      ),
      isTrue,
    );
    expect(
      first.matches(
        displayName: '另一账户',
        accountIdentifier: 'xxx276',
        baseCurrency: 'USD',
      ),
      isFalse,
    );
    expect(
      AccountCreationRequest(
        displayName: '另一账户',
        accountIdentifier: 'XXX276',
      ).idempotencyKey,
      isNot(first.idempotencyKey),
    );
    expect(accountIdentifierFromSchwabFile(_fileName), 'XXX276');
    expect(
      accountIdentifierFromSchwabFile('Individual_bad/name_File.csv'),
      isNull,
    );
    expect(isValidAccountIdentifier('276'), isFalse);
    expect(isValidAccountIdentifier('XXX12'), isFalse);
  });

  testWidgets('空账本首页创建证券账户、选中、导入预览并确认第一笔记录', (tester) async {
    final fixture = _FirstUseFixture();
    await _pumpFixture(tester, fixture, fullApp: true);
    expect(find.text('先创建一个证券账户'), findsOneWidget);
    expect(fixture.createKeys, isEmpty);
    await _tap(tester, find.text('创建第一个证券账户'));
    expect(find.textContaining('登录账号用于访问 Lyra'), findsOneWidget);
    await _fillAccount(tester);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(find.byType(AccountSheet), findsNothing);
    expect(find.byType(ImportSheet), findsOneWidget);
    final container = ProviderScope.containerOf(
      tester.element(find.byType(ImportSheet)),
    );
    expect(container.read(accountScopeProvider).id, LedgerFixture.account);
    expect(fixture.createBodies.single['accountIdentifier'], 'XXX276');
    await _tap(tester, find.text('选择文件并预览'));
    expect(find.text('总行数 1 · 可新增 1 · 已存在 0'), findsOneWidget);
    await _tap(tester, find.text('确认写入账本（1 行）'));
    expect(find.text('导入完成：新增 1 条交易。'), findsOneWidget);
    expect(find.textContaining('账本版本'), findsNothing);
    expect(container.read(accountScopeProvider).id, LedgerFixture.account);
    expect(
      (container.read(sessionProvider).value as SessionAuthenticated)
          .summary!
          .metadata
          .ledgerVersion,
      1,
    );
    expect(fixture.filePicks, 1);
    expect(fixture.uploads.single, orderedEquals(_fileBytes));
    expect(tester.takeException(), isNull);
  });

  testWidgets('已有账户的范围选择器保留新增入口并选中新建账户', (tester) async {
    final fixture = _FirstUseFixture()..created = true;
    await _pumpFixture(tester, fixture, fullApp: true);
    await _tap(tester, find.text('全部账号'));
    expect(find.text('选择证券账户'), findsOneWidget);
    await _tap(tester, find.text('新增证券账户'));
    await _fillAccount(tester);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(find.text('选择证券账户'), findsNothing);
    expect(find.text('Schwab 主账户'), findsOneWidget);
    expect(fixture.createKeys, hasLength(1));
    expect(tester.takeException(), isNull);
  });

  testWidgets('导入先选文件后新建账户，预填需确认且保持文件字节继续预览', (tester) async {
    final fixture = _FirstUseFixture();
    await _pumpFixture(tester, fixture);
    await _openImportCreation(tester);
    expect(fixture.createKeys, isEmpty);
    expect(find.textContaining('请核对后创建'), findsOneWidget);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(fixture.createKeys, hasLength(1));
    expect(fixture.previewAccounts, [null, LedgerFixture.account]);
    expect(fixture.filePicks, 1);
    expect(fixture.uploads, [
      orderedEquals(_fileBytes),
      orderedEquals(_fileBytes),
    ]);
    expect(find.text('总行数 1 · 可新增 1 · 已存在 0'), findsOneWidget);
    expect(find.text('scope:${LedgerFixture.account}'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('创建响应丢失后重试复用幂等键和表单，不重新选择文件', (tester) async {
    final fixture = _FirstUseFixture()..loseFirstCreateResponse = true;
    await _pumpFixture(tester, fixture);
    await _openImportCreation(tester);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(find.byType(AccountSheet), findsOneWidget);
    expect(find.text('暂时无法创建证券账户，请重试。'), findsOneWidget);
    expect(find.textContaining('CLIENT_REQUEST_FAILED'), findsNothing);
    expect(fixture.previewAccounts, [null]);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(fixture.createKeys, hasLength(2));
    expect(fixture.createKeys[0], fixture.createKeys[1]);
    expect(fixture.createBodies[0], fixture.createBodies[1]);
    expect(fixture.filePicks, 1);
    expect(find.text('总行数 1 · 可新增 1 · 已存在 0'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('重复账户返回现有账户选择，须明确选择后继续同一文件', (tester) async {
    final fixture = _FirstUseFixture()..duplicateOnCreate = true;
    await _pumpFixture(tester, fixture);
    await _openImportCreation(tester);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(find.text('该证券账户已存在，请返回并选择已有账户。'), findsOneWidget);
    await _tap(tester, find.text('返回选择已有账户'));
    expect(find.text('Schwab 主账户（276）'), findsOneWidget);
    expect(fixture.previewAccounts, [null]);
    await _tap(tester, find.text('Schwab 主账户（276）'));
    expect(fixture.previewAccounts, [null, LedgerFixture.account]);
    expect(fixture.createKeys, hasLength(1));
    expect(fixture.filePicks, 1);
    expect(find.text('总行数 1 · 可新增 1 · 已存在 0'), findsOneWidget);
  });

  testWidgets('新建账户后预览暂时失败，可带同一账户重试原文件', (tester) async {
    final fixture = _FirstUseFixture()..failFirstMappedPreview = true;
    await _pumpFixture(tester, fixture);
    await _openImportCreation(tester);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(find.byType(AccountSheet), findsNothing);
    expect(find.text('暂时无法预览文件，请重试。'), findsOneWidget);
    expect(find.textContaining('SERVER_BUSY'), findsNothing);
    expect(find.textContaining('尚无可用的证券账户'), findsNothing);
    await _tap(tester, find.text('重试当前文件'));
    expect(fixture.previewAccounts, [
      null,
      LedgerFixture.account,
      LedgerFixture.account,
    ]);
    expect(fixture.createKeys, hasLength(1));
    expect(fixture.filePicks, 1);
    for (final upload in fixture.uploads) {
      expect(upload, orderedEquals(_fileBytes));
    }
    expect(find.text('总行数 1 · 可新增 1 · 已存在 0'), findsOneWidget);
  });

  testWidgets('取消创建不写入账户，已选择文件仍可继续创建', (tester) async {
    final fixture = _FirstUseFixture();
    await _pumpFixture(tester, fixture);
    await _openImportCreation(tester);
    await _tap(tester, find.text('取消'));
    expect(fixture.createKeys, isEmpty);
    expect(find.textContaining('已选择：$_fileName'), findsOneWidget);
    await _tap(tester, find.text('新增证券账户'));
    await _fillAccount(tester, fromFile: true);
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(fixture.filePicks, 1);
    expect(fixture.previewAccounts, [null, LedgerFixture.account]);
  });

  testWidgets('切换登录后迟到创建响应不能选择旧账户或继续导入', (tester) async {
    final fixture = _FirstUseFixture()
      ..delayedCreate = Completer<ResponseBody>();
    await _pumpFixture(tester, fixture);
    await _openImportCreation(tester);
    final container = ProviderScope.containerOf(
      tester.element(find.byType(ImportSheet)),
    );
    await _tap(tester, find.byKey(const ValueKey('create-account-submit')));
    expect(fixture.createKeys, hasLength(1));
    await _replaceIdentity(tester, fixture, container);
    fixture.delayedCreate!.complete(_json({'account': fixture.account}, 201));
    await tester.pumpAndSettle();
    expect(find.text('登录状态已变化，请关闭后重新打开。'), findsOneWidget);
    expect(container.read(accountScopeProvider).id, 'all');
    expect(fixture.previewAccounts, [null]);
    expect(find.textContaining('已选择：$_fileName'), findsNothing);
    expect(
      tester
          .widget<TextFormField>(find.byKey(const ValueKey('account-name')))
          .controller!
          .text,
      isEmpty,
    );
    expect(
      (container.read(sessionProvider).value as SessionAuthenticated)
          .snapshot
          .deviceId,
      'new-device',
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('旧身份候选列表迟到后不泄露账户或恢复所选文件', (tester) async {
    final fixture = _FirstUseFixture()
      ..delayedAccounts = Completer<ResponseBody>();
    await _pumpFixture(tester, fixture);
    final container = ProviderScope.containerOf(
      tester.element(find.byType(ImportSheet)),
    );
    await _tap(tester, find.text('选择文件并预览'));
    await _replaceIdentity(tester, fixture, container);
    fixture.delayedAccounts!.complete(
      _json({
        'data': {
          'rows': [fixture.account],
        },
      }),
    );
    await tester.pumpAndSettle();
    expect(find.text('Schwab 主账户（276）'), findsNothing);
    expect(find.textContaining('已选择：$_fileName'), findsNothing);
    expect(find.text('登录状态已变化，请关闭导入面板后重新打开。'), findsOneWidget);
    expect(fixture.previewAccounts, [null]);
    expect(tester.takeException(), isNull);
  });

  testWidgets('文件选择器迟到返回旧身份文件时不上传', (tester) async {
    final fixture = _FirstUseFixture();
    final picked = Completer<XFile?>();
    await _pumpFixture(tester, fixture, picker: (_) => picked.future);
    final container = ProviderScope.containerOf(
      tester.element(find.byType(ImportSheet)),
    );
    await _tap(tester, find.text('选择文件并预览'));
    await _replaceIdentity(tester, fixture, container);
    picked.complete(XFile.fromData(_fileBytes, name: _fileName));
    await tester.pumpAndSettle();
    expect(fixture.previewAccounts, isEmpty);
    expect(find.textContaining('已选择：$_fileName'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('导入面板卸载后迟到文件选择结果不触发 setState 或上传', (tester) async {
    final fixture = _FirstUseFixture();
    final picked = Completer<XFile?>();
    await _pumpFixture(tester, fixture, picker: (_) => picked.future);
    await _tap(tester, find.text('选择文件并预览'));
    await tester.pumpWidget(const SizedBox.shrink());
    picked.complete(XFile.fromData(_fileBytes, name: _fileName));
    await tester.pumpAndSettle();
    expect(fixture.previewAccounts, isEmpty);
    expect(tester.takeException(), isNull);
  });
}
