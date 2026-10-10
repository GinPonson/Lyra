import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:file_selector/file_selector.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/api/lyra_api_client.dart';
import 'package:lyra_mobile/core/api/lyra_api_error.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';
import 'package:lyra_mobile/features/import/import_repository.dart';
import 'package:lyra_mobile/features/import/import_sheet.dart';

/// 按请求路径返回固定响应的假 HTTP 适配器；不触达真实网络。
class FakeImportAdapter implements HttpClientAdapter {
  FakeImportAdapter(this.handler);

  final Future<ResponseBody> Function(RequestOptions options) handler;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    return handler(options);
  }

  @override
  void close({bool force = false}) {}
}

const String _previewResponse = '''
{
  "importId": "i1000000-0000-4000-8000-000000000001",
  "confirmToken": "confirm-token-1",
  "preview": {
    "fileName": "Individual_TST276_Transactions_20260902-080000.csv",
    "accountId": "a2000000-0000-4000-8000-000000000001",
    "baseLedgerVersion": 1,
    "totalRows": 2,
    "insertableRows": 2,
    "existingRows": 0,
    "byAction": {"Buy": 1, "Qualified Dividend": 1}
  }
}
''';

const String _accountsResponse = '''
{
  "metadata": {"accountScope": {"kind": "all"}, "accountIds": [],
    "currencyGroups": [], "aggregateCurrency": null, "ledgerVersion": 1,
    "asOf": "2026-09-02T10:00:00.000Z", "fetchedAt": "2026-09-02T10:00:00.000Z"},
  "data": {
    "rows": [
      {"id": "a2000000-0000-4000-8000-000000000001",
       "displayName": "Individual", "broker": "schwab",
       "maskedSuffix": "TST276", "baseCurrency": "USD",
       "status": "active", "createdAt": "2026-09-01T08:00:00.000Z",
       "updatedAt": "2026-09-01T08:00:00.000Z"},
      {"id": "a3000000-0000-4000-8000-000000000003",
       "displayName": "Archived", "broker": "schwab",
       "maskedSuffix": "OLD999", "baseCurrency": "USD",
       "status": "archived", "createdAt": "2026-09-01T08:00:00.000Z",
       "updatedAt": "2026-09-01T08:00:00.000Z"}
    ],
    "count": 2,
    "currencyGroups": []
  }
}
''';

const String _commitResponse = '''
{"changed": true, "ledgerVersion": 2, "insertedRows": 2, "skippedRows": 0}
''';

String _mappingRequiredResponse() {
  return jsonEncode(<String, Object?>{
    'request_id': 'req-mapping-1',
    'code': 'ACCOUNT_MAPPING_REQUIRED',
    'message': '导入文件必须明确选择匹配的活跃账户',
    'retryable': false,
    'details': <String, Object?>{
      'candidate_account_ids': <String>['a2000000-0000-4000-8000-000000000001'],
    },
  });
}

ResponseBody _jsonResponse(String body, int status) {
  return ResponseBody.fromString(
    body,
    status,
    headers: <String, List<String>>{
      Headers.contentTypeHeader: <String>[Headers.jsonContentType],
    },
  );
}

const CredentialSnapshot _snapshot = CredentialSnapshot(
  serverUrl: 'http://127.0.0.1:3141',
  deviceId: 'd1000000-0000-4000-8000-000000000001',
  accessToken: 'access-token-1',
  refreshToken: 'refresh-token-1',
  authMethod: 'google',
);

Future<ResponseBody> _routeImportRequest(RequestOptions options) async {
  final path = options.uri.path;
  if (path == '/v1/ledger/accounts') {
    expect(options.uri.queryParameters['scope'], 'all');
    return _jsonResponse(_accountsResponse, 200);
  }
  if (path == '/v1/ledger/summary') {
    return _jsonResponse(
      '{"metadata":{"accountScope":{"kind":"all"},'
      '"accountIds":[],"currencyGroups":[],'
      '"aggregateCurrency":null,"ledgerVersion":1,'
      '"asOf":"2026-09-02T10:00:00.000Z",'
      '"fetchedAt":"2026-09-02T10:00:00.000Z"},'
      '"data":{"aggregateCurrency":null,"groups":[]}}',
      200,
    );
  }
  if (path == '/v1/imports/preview') {
    if (options.uri.queryParameters['accountId'] != null) {
      return _jsonResponse(_previewResponse, 201);
    }
    return _jsonResponse(_mappingRequiredResponse(), 409);
  }
  if (path.endsWith('/commit')) {
    // 提交必须携带设备幂等键，供服务端网络超时重放持久化结果。
    expect(
      options.headers['idempotency-key'],
      'commit-i1000000-0000-4000-8000-000000000001',
    );
    return _jsonResponse(_commitResponse, 200);
  }
  return _jsonResponse('unexpected', 404);
}

/// 固定返回注入的 CSV 文件；测试不触达平台文件选择器。
Future<XFile?> _openPresetCsvFile(List<XTypeGroup> acceptedTypes) async {
  return XFile.fromData(
    Uint8List.fromList(utf8.encode('Date,Action\n')),
    path: 'Individual_TST276_Transactions_20260902-080000.csv',
    name: 'Individual_TST276_Transactions_20260902-080000.csv',
    mimeType: 'text/csv',
  );
}

/// 会话状态探针：watch sessionProvider，驱动 AsyncNotifier 在
/// FakeAsync 时钟内完成凭据恢复与账本汇总加载（与生产 OverviewScreen 一致）。
class _SessionProbe extends ConsumerWidget {
  const _SessionProbe();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(sessionProvider);
    return Text(
      state.when(
        loading: () => 'session:loading',
        error: (Object error, StackTrace stackTrace) => 'session:error',
        data: (SessionState value) => value is SessionAuthenticated
            ? 'session:paired'
            : 'session:unpaired',
      ),
    );
  }
}

void main() {
  test('ImportRepository 读取账户列表并解析上传预览与确认提交', () async {
    final requestedPaths = <String>[];
    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    final dio = Dio(BaseOptions(baseUrl: 'http://127.0.0.1:3141'))
      ..httpClientAdapter = FakeImportAdapter((RequestOptions options) async {
        requestedPaths.add(options.uri.path);
        return _routeImportRequest(options);
      });
    final repository = ImportRepository(
      client: LyraApiClient(
        serverUrl: 'http://127.0.0.1:3141',
        deviceId: _snapshot.deviceId,
        credentialStore: store,
        tokenRefresher: () async => null,
        dio: dio,
      ),
    );

    final accounts = await repository.accounts();
    // 归档账户 OLD999 在客户端边界被显式过滤，不作为导入候选。
    expect(accounts.rows, hasLength(1));
    expect(accounts.rows.first.displayName, 'Individual');
    expect(accounts.rows.first.maskedSuffix, 'TST276');
    expect(accounts.rows.first.isActive, isTrue);

    // 未显式选择账户且无法唯一精确匹配时，服务端要求映射；错误模型
    // 保留可操作的候选账户 details，正文不含文件账户键或底层细节。
    await expectLater(
      repository.uploadPreview(
        bytes: Uint8List.fromList(utf8.encode('Date,Action\n')),
        fileName: 'Individual_TST276_Transactions_20260902-080000.csv',
      ),
      throwsA(
        isA<LyraApiError>()
            .having(
              (LyraApiError error) => error.code,
              'code',
              'ACCOUNT_MAPPING_REQUIRED',
            )
            .having(
              (LyraApiError error) => error.details?['candidate_account_ids'],
              'candidates',
              <String>['a2000000-0000-4000-8000-000000000001'],
            ),
      ),
    );

    final preview = await repository.uploadPreview(
      bytes: Uint8List.fromList(utf8.encode('Date,Action\n')),
      fileName: 'Individual_TST276_Transactions_20260902-080000.csv',
      accountId: 'a2000000-0000-4000-8000-000000000001',
    );
    expect(preview.importId, 'i1000000-0000-4000-8000-000000000001');
    expect(preview.totalRows, 2);
    expect(preview.insertableRows, 2);
    expect(preview.byAction['Buy'], 1);

    final commit = await repository.commit(
      importId: preview.importId,
      confirmToken: preview.confirmToken,
      idempotencyKey: 'commit-${preview.importId}',
    );
    expect(commit.changed, true);
    expect(commit.ledgerVersion, 2);
    expect(requestedPaths, <String>[
      '/v1/ledger/accounts',
      '/v1/imports/preview',
      '/v1/imports/preview',
      '/v1/imports/i1000000-0000-4000-8000-000000000001/commit',
    ]);
  });

  testWidgets('导入面板完成选择文件、预览统计与确认提交', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final store = MemoryCredentialStore();
    await store.write(_snapshot);
    var committed = false;
    var summaryReads = 0;

    // 用可变服务端版本验证真实缓存失效行为，避免仅检查成功提示而漏掉旧汇总。
    Future<ResponseBody> routeAfterCommit(RequestOptions options) async {
      if (options.uri.path == '/v1/ledger/summary') {
        summaryReads++;
        final body = jsonDecode(_accountsResponse) as Map<String, dynamic>;
        (body['metadata'] as Map<String, dynamic>)['ledgerVersion'] = committed
            ? 2
            : 1;
        body['data'] = {'aggregateCurrency': null, 'groups': <Object?>[]};
        return _jsonResponse(jsonEncode(body), 200);
      }
      if (options.uri.path.endsWith('/commit')) committed = true;
      return _routeImportRequest(options);
    }

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          sessionDependenciesProvider.overrideWithValue(
            SessionDependencies(
              credentialStore: store,
              clientFactory: (String serverUrl) {
                return Dio(BaseOptions(baseUrl: serverUrl))
                  ..httpClientAdapter = FakeImportAdapter(routeAfterCommit);
              },
            ),
          ),
        ],
        child: MaterialApp(
          home: Scaffold(
            body: Column(
              children: <Widget>[
                // 会话探针：与生产一致地 watch 状态，驱动 AsyncNotifier
                // 在 FakeAsync 时钟内完成凭据恢复与汇总加载。
                const _SessionProbe(),
                const Expanded(
                  child: ImportSheet(openFileOverride: _openPresetCsvFile),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('session:paired'), findsOneWidget);

    // 选择文件：注入固定 CSV，不触达平台文件选择器。
    await tester.tap(find.text('选择文件并预览'));
    await tester.pumpAndSettle();

    // 服务端要求显式账户映射后，面板展示候选账户按钮。
    expect(find.textContaining('ACCOUNT_MAPPING_REQUIRED'), findsOneWidget);
    expect(find.textContaining('请选择该文件对应的账户'), findsOneWidget);
    expect(find.text('Individual（TST276）'), findsOneWidget);
    // 归档账户不进入候选。
    expect(find.textContaining('OLD999'), findsNothing);

    // 选择账户后携带 accountId 重新预览成功。
    await tester.tap(find.text('Individual（TST276）'));
    await tester.pumpAndSettle();

    expect(find.text('总行数 2 · 可新增 2 · 已存在 0'), findsOneWidget);
    expect(find.textContaining('Buy × 1'), findsOneWidget);

    await tester.tap(find.textContaining('确认写入账本'));
    await tester.pumpAndSettle();

    expect(find.textContaining('已写入账本'), findsOneWidget);
    expect(find.textContaining('账本版本 2'), findsOneWidget);
    expect(summaryReads, 2);
    final state =
        ProviderScope.containerOf(tester.element(find.byType(ImportSheet)))
                .read(sessionProvider)
                .requireValue
            as SessionAuthenticated;
    expect(state.summary!.metadata.ledgerVersion, 2);
  });

  testWidgets('未登录时导入面板提示先登录', (WidgetTester tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    await tester.pumpWidget(
      ProviderScope(
        child: MaterialApp(
          home: Scaffold(
            body: ImportSheet(
              openFileOverride: (List<XTypeGroup> acceptedTypes) async => null,
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('选择文件并预览'));
    await tester.pumpAndSettle();

    expect(find.text('请先完成登录'), findsOneWidget);
  });
}
