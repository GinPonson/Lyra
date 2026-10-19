import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_error_message.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import '../../core/design/lyra_widgets.dart';
import '../accounts/account_sheet.dart';
import '../accounts/account_workspace.dart';
import '../ledger/ledger_workspace.dart';
import '../ledger/ledger_view.dart';
import 'import_repository.dart';

typedef ImportFilePicker = Future<XFile?> Function(
  List<XTypeGroup> acceptedTypes,
);

/// 系统文件选择器可在流程测试中替换，不影响真实文件读取与上传链路。
final importFilePickerProvider = Provider<ImportFilePicker>(
  (ref) =>
      (types) => openFile(acceptedTypeGroups: types),
);

/// 选择 Schwab 文件、确认证券账户、预览后提交，并在重试时保留所选文件。
class ImportSheet extends ConsumerStatefulWidget {
  const ImportSheet({super.key, this.openFileOverride});

  final Future<XFile?> Function(List<XTypeGroup> acceptedTypes)?
  openFileOverride;

  @override
  ConsumerState<ImportSheet> createState() => _ImportSheetState();
}

enum _ImportStage {
  idle,
  picking,
  uploading,
  previewReady,
  committing,
  committed,
}

class _ImportSheetState extends ConsumerState<ImportSheet> {
  LyraApiClient? _client;
  ImportRepository? _repository;
  _ImportStage _stage = _ImportStage.idle;
  ImportPreviewView? _preview;
  ImportCommitResult? _result;
  List<ImportAccountView>? _accountCandidates;
  String? _errorMessage;
  String? _accountsError;
  Uint8List? _pendingBytes;
  String? _pendingFileName;
  String? _pendingAccountId;
  int _generation = 0;
  bool _mappingRequired = false;
  bool _loadingAccounts = false;
  bool _identityChanged = false;

  bool get _busy =>
      _stage == _ImportStage.picking ||
      _stage == _ImportStage.uploading ||
      _stage == _ImportStage.committing ||
      _loadingAccounts;

  ImportRepository? _resolveRepository() {
    if (_identityChanged) return null;
    final client = ref.read(sessionProvider.notifier).client;
    if (client == null || (_client != null && !identical(client, _client))) {
      return null;
    }
    _client = client;
    return _repository ??= ImportRepository(client: client);
  }

  bool _active(int generation) =>
      mounted &&
      !_identityChanged &&
      generation == _generation &&
      identical(_client, ref.read(sessionProvider.notifier).client);

  Future<void> _pickFile() async {
    if (_busy || _identityChanged) return;
    if (_resolveRepository() == null) {
      setState(() => _errorMessage = '请先完成登录');
      return;
    }
    final generation = ++_generation;
    final previousStage = _stage;
    setState(() {
      _stage = _ImportStage.picking;
      _errorMessage = null;
    });
    try {
      const acceptedTypes = <XTypeGroup>[
        XTypeGroup(label: 'Schwab 导出', extensions: <String>['csv', 'xlsx']),
      ];
      final ImportFilePicker picker =
          widget.openFileOverride ??
          ref.read<ImportFilePicker>(importFilePickerProvider);
      final file = await picker(acceptedTypes);
      if (!_active(generation)) return;
      if (file == null) {
        setState(() => _stage = previousStage);
        return;
      }
      final bytes = await file.readAsBytes();
      if (!_active(generation)) return;
      setState(() {
        _pendingBytes = bytes;
        _pendingFileName = file.name;
        _pendingAccountId = null;
        _mappingRequired = false;
        _accountCandidates = null;
        _accountsError = null;
      });
      await _uploadPreview();
    } on Object {
      if (!_active(generation)) return;
      setState(() {
        _stage = previousStage;
        _errorMessage = '无法读取所选文件，请重试或选择其他文件。';
      });
    }
  }

  Future<void> _uploadPreview({String? accountId}) async {
    final repository = _resolveRepository();
    final bytes = _pendingBytes;
    final fileName = _pendingFileName;
    if (repository == null || bytes == null || fileName == null) return;
    final generation = ++_generation;
    if (accountId != null) _pendingAccountId = accountId;
    setState(() {
      _stage = _ImportStage.uploading;
      _errorMessage = null;
      _preview = null;
      _result = null;
    });
    try {
      final preview = await repository.uploadPreview(
        bytes: bytes,
        fileName: fileName,
        accountId: _pendingAccountId,
      );
      if (!_active(generation)) return;
      setState(() {
        _preview = preview;
        _stage = _ImportStage.previewReady;
        _mappingRequired = false;
        _accountCandidates = null;
        _accountsError = null;
      });
    } on Object catch (failure) {
      if (!_active(generation)) return;
      final error = LyraApiError.from(failure);
      _mappingRequired = error.code == 'ACCOUNT_MAPPING_REQUIRED';
      if (_mappingRequired) {
        await _loadAccountCandidates(repository, generation);
      }
      if (!_active(generation)) return;
      setState(() {
        _stage = _ImportStage.idle;
        _errorMessage = lyraErrorMessage(error, fallback: '暂时无法预览文件，请重试。');
      });
    }
  }

  Future<void> _loadAccountCandidates(
    ImportRepository repository,
    int generation,
  ) async {
    try {
      final candidates = (await repository.accounts()).rows;
      if (!_active(generation)) return;
      setState(() {
        _accountCandidates = candidates;
        _accountsError = null;
      });
    } on Object {
      if (!_active(generation)) return;
      setState(() => _accountsError = '证券账户列表加载失败，所选文件已保留。');
    }
  }

  Future<void> _refreshCandidates() async {
    final repository = _resolveRepository();
    if (repository == null || _busy) return;
    final generation = ++_generation;
    setState(() => _loadingAccounts = true);
    await _loadAccountCandidates(repository, generation);
    if (_active(generation)) setState(() => _loadingAccounts = false);
  }

  Future<void> _createAccount() async {
    if (_busy || _resolveRepository() == null) return;
    final generation = _generation;
    final account = await showCreateAccountSheet(
      context,
      sourceFileName: _pendingFileName,
    );
    if (!_active(generation)) return;
    if (account != null) {
      await _uploadPreview(accountId: account.id);
    } else {
      await _refreshCandidates();
    }
  }

  Future<void> _selectAccount(ImportAccountView account) async {
    if (_busy || _resolveRepository() == null) return;
    ref
        .read(accountScopeProvider.notifier)
        .select(account.id, account.displayName);
    await _uploadPreview(accountId: account.id);
  }

  Future<void> _commit() async {
    final repository = _resolveRepository();
    final preview = _preview;
    final client = _client;
    if (repository == null || preview == null || client == null || _busy) {
      return;
    }
    final generation = ++_generation;
    // 面板关闭后仍刷新当前身份账本；不同登录身份不得受旧请求影响。
    final container = ProviderScope.containerOf(context, listen: false);
    final controller = ref.read(sessionProvider.notifier);
    setState(() {
      _stage = _ImportStage.committing;
      _errorMessage = null;
    });
    try {
      final result = await repository.commit(
        importId: preview.importId,
        confirmToken: preview.confirmToken,
        idempotencyKey: 'commit-${preview.importId}',
      );
      if (result.changed && identical(controller.client, client)) {
        await refreshLedgerWorkspace(container: container, client: client);
      }
      if (!_active(generation)) return;
      setState(() {
        _result = result;
        _stage = _ImportStage.committed;
      });
    } on Object catch (failure) {
      if (!_active(generation)) return;
      final error = LyraApiError.from(failure);
      setState(() {
        final needsPreview = const {
          'IMPORT_PREVIEW_STALE',
          'IMPORT_STATE_CONFLICT',
          'LEDGER_VERSION_CONFLICT',
        }.contains(error.code);
        if (needsPreview) _preview = null;
        _stage = needsPreview ? _ImportStage.idle : _ImportStage.previewReady;
        _errorMessage = lyraErrorMessage(error, fallback: '暂时无法确认导入，请重试。');
      });
    }
  }

  /// 按文件、预览和结果阶段呈现导入流程，所有请求继续由状态机约束。
  @override
  Widget build(BuildContext context) {
    ref.listen(sessionProvider, (previous, next) {
      if (_client != null &&
          !identical(_client, ref.read(sessionProvider.notifier).client) &&
          !_identityChanged) {
        setState(() {
          ++_generation;
          _identityChanged = true;
          _stage = _ImportStage.idle;
          _loadingAccounts = false;
          _pendingBytes = null;
          _pendingFileName = null;
          _pendingAccountId = null;
          _preview = null;
          _result = null;
          _accountCandidates = null;
          _mappingRequired = false;
          _accountsError = null;
          _errorMessage = '登录状态已变化，请关闭导入面板后重新打开。';
        });
      }
    });
    final preview = _preview;
    final result = _result;
    final candidates = _accountCandidates ?? const <ImportAccountView>[];
    final chooseFile = _busy || _identityChanged ? null : _pickFile;
    final chooseLabel = Text(switch (_stage) {
      _ImportStage.picking => '打开文件中…',
      _ImportStage.uploading => '正在准备预览…',
      _ => preview == null ? '选择文件并预览' : '更换文件',
    });
    const chooseIcon = LyraPrimaryIcon(LyraPrimaryGlyph.importFile, size: 20);
    return Padding(
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          const LyraSheetHeader(title: '导入交易'),
          const SizedBox(height: 6),
          Text(
            '支持 CSV / Excel，预览后再导入。',
            style: Theme.of(context).textTheme.bodyMedium
                ?.copyWith(color: LyraColors.muted, height: 1.5),
          ),
          const SizedBox(height: 14),
          LyraCard(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                // 预览就绪后，确认是主要操作，更换文件降低为次级入口。
                if (preview == null)
                  FilledButton.icon(
                    onPressed: chooseFile,
                    icon: chooseIcon,
                    label: chooseLabel,
                  )
                else
                  OutlinedButton.icon(
                    onPressed: chooseFile,
                    icon: chooseIcon,
                    label: chooseLabel,
                  ),
                if (_pendingFileName != null && preview == null) ...[
                  const SizedBox(height: 8),
                  Text(
                    '已选择：$_pendingFileName',
                    style: Theme.of(context).textTheme.bodySmall
                        ?.copyWith(color: LyraColors.inkSecondary, height: 1.5),
                  ),
                ],
                if (_mappingRequired) ...[
                  const Divider(height: 24),
                  const _ImportSectionTitle(title: '选择账户'),
                  const SizedBox(height: 6),
                  Text(
                    candidates.isEmpty
                        ? _accountsError != null
                              ? '暂时无法读取证券账户，所选文件已保留。'
                              : '尚无可用的证券账户，请先创建。所选文件会保留。'
                        : '请选择该文件对应的账户后重新预览：',
                    style: Theme.of(context).textTheme.bodyMedium
                        ?.copyWith(color: LyraColors.inkSecondary, height: 1.5),
                  ),
                  if (candidates.isNotEmpty) const SizedBox(height: 8),
                  for (final account in candidates)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: OutlinedButton.icon(
                        onPressed: _busy ? null : () => _selectAccount(account),
                        icon: const LyraPrimaryIcon(
                          LyraPrimaryGlyph.brokerage,
                          size: 20,
                        ),
                        label: Text(
                          '${account.displayName}（${account.maskedSuffix}）',
                        ),
                      ),
                    ),
                  OutlinedButton.icon(
                    onPressed: _busy ? null : _createAccount,
                    icon: const Icon(LyraIcons.addAccount),
                    label: const Text('新增证券账户'),
                  ),
                  if (_accountsError != null) ...[
                    const SizedBox(height: 6),
                    Text(
                      _accountsError!,
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: LyraColors.inkSecondary,
                        height: 1.5,
                      ),
                    ),
                    TextButton.icon(
                      onPressed: _busy ? null : _refreshCandidates,
                      icon: const Icon(LyraIcons.sync, size: 18),
                      label: const Text('重新加载账户'),
                    ),
                  ],
                ],
                if (preview != null) ...[
                  const Divider(height: 24),
                  const _ImportSectionTitle(title: '预览交易'),
                  const SizedBox(height: 8),
                  _PreviewStats(preview: preview),
                  if (_stage != _ImportStage.committed) ...[
                    const Divider(height: 24),
                    FilledButton.icon(
                      onPressed: _busy ? null : _commit,
                      icon: const Icon(LyraIcons.confirm),
                      label: Text(
                        preview.insertableRows == 0 ? '确认无新增' : '确认导入',
                      ),
                    ),
                  ],
                ],
                if (result != null) ...[
                  const Divider(height: 24),
                  Text(
                    result.changed
                        ? '导入完成：新增 ${result.insertedRows} 条交易'
                              '${result.skippedRows > 0 ? '，已跳过 ${result.skippedRows} 条重复记录' : ''}。'
                        : result.skippedRows > 0
                        ? '这些交易已在账本中，无需重复导入。'
                        : '文件中没有可导入的交易。',
                    style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      color: LyraColors.ink,
                      fontWeight: FontWeight.w500,
                      height: 1.5,
                    ),
                  ),
                ],
                // 缺少账户是上方已说明的必选步骤，不再重复显示同一条错误提示。
                // 文件、网络或提交失败仍在这里保留原有错误与恢复入口。
                if (_errorMessage != null && !_mappingRequired) ...[
                  const Divider(height: 24),
                  Text(
                    _errorMessage!,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                      height: 1.5,
                    ),
                  ),
                  if (!_mappingRequired &&
                      preview == null &&
                      _pendingBytes != null &&
                      !_identityChanged)
                    TextButton.icon(
                      onPressed: _busy ? null : () => _uploadPreview(),
                      icon: const Icon(LyraIcons.sync, size: 18),
                      label: const Text('重试当前文件'),
                    ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// 以轻量文字标记导入流程中的一个阶段。
class _ImportSectionTitle extends StatelessWidget {
  const _ImportSectionTitle({required this.title});

  final String title;

  @override
  Widget build(BuildContext context) => Text(
    title,
    style: Theme.of(context).textTheme.labelMedium?.copyWith(
      color: LyraColors.muted,
      fontWeight: FontWeight.w500,
      letterSpacing: 0,
    ),
  );
}

/// 显示服务端生成的预览文件名、行数统计与按操作分类的记录数。
class _PreviewStats extends StatelessWidget {
  const _PreviewStats({required this.preview});

  final ImportPreviewView preview;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          preview.fileName,
          style: Theme.of(context).textTheme.titleSmall?.copyWith(
            color: LyraColors.ink,
            fontWeight: FontWeight.w500,
            height: 1.5,
          ),
        ),
        const SizedBox(height: 6),
        Text(
          '总行数 ${preview.totalRows} · 可新增 ${preview.insertableRows}'
          ' · 已存在 ${preview.existingRows}',
          style: const TextStyle(color: LyraColors.ink, height: 1.5),
        ),
        const SizedBox(height: 4),
        Text(
          preview.byAction.entries
              .map((entry) => '${ledgerText(entry.key)} × ${entry.value}')
              .join(' · '),
          style: Theme.of(context).textTheme.bodySmall
              ?.copyWith(color: LyraColors.inkSecondary, height: 1.5),
        ),
      ],
    );
  }
}
