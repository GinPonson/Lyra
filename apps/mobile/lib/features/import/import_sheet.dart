import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../import/import_repository.dart';

/// 导入流程面板：选择 Schwab 文件 → 上传预览 → 确认提交。
///
/// 文件选择器可通过 [openFileOverride] 注入；测试不触达平台通道。
class ImportSheet extends ConsumerStatefulWidget {
  const ImportSheet({super.key, this.openFileOverride});

  final Future<XFile?> Function(List<XTypeGroup> acceptedTypes)?
  openFileOverride;

  @override
  ConsumerState<ImportSheet> createState() => _ImportSheetState();
}

enum _ImportStage { idle, uploading, previewReady, committing, committed }

class _ImportSheetState extends ConsumerState<ImportSheet> {
  ImportRepository? _repository;
  _ImportStage _stage = _ImportStage.idle;
  ImportPreviewView? _preview;
  ImportCommitResult? _result;
  List<ImportAccountView>? _accountCandidates;
  String? _errorMessage;
  Uint8List? _pendingBytes;
  String? _pendingFileName;

  bool get _busy =>
      _stage == _ImportStage.uploading || _stage == _ImportStage.committing;

  ImportRepository? _resolveRepository() {
    if (_repository != null) {
      return _repository;
    }
    final client = ref.read(sessionProvider.notifier).client;
    if (client == null) {
      return null;
    }
    return _repository = ImportRepository(client: client);
  }

  Future<void> _pickAndPreview({String? accountId}) async {
    final repository = _resolveRepository();
    if (repository == null) {
      setState(() => _errorMessage = '请先完成登录');
      return;
    }
    // 账户选择重试不重新弹文件选择器，直接复用已暂存的字节。
    if (accountId == null) {
      const acceptedTypes = <XTypeGroup>[
        XTypeGroup(label: 'Schwab 导出', extensions: <String>['csv', 'xlsx']),
      ];
      final XFile? file = widget.openFileOverride != null
          ? await widget.openFileOverride!(acceptedTypes)
          : await openFile(acceptedTypeGroups: acceptedTypes);
      if (file == null) {
        return;
      }
      _pendingBytes = await file.readAsBytes();
      _pendingFileName = file.name;
    }
    final bytes = _pendingBytes;
    final fileName = _pendingFileName;
    if (bytes == null || fileName == null) {
      return;
    }
    setState(() {
      _stage = _ImportStage.uploading;
      _errorMessage = null;
      _preview = null;
      _result = null;
      if (accountId == null) {
        _accountCandidates = null;
      }
    });
    try {
      final preview = await repository.uploadPreview(
        bytes: bytes,
        fileName: fileName,
        accountId: accountId,
      );
      setState(() {
        _preview = preview;
        _stage = _ImportStage.previewReady;
        _accountCandidates = null;
      });
    } on LyraApiError catch (error) {
      // 服务端明确要求显式账户映射时，先读取候选账户列表再统一刷新界面。
      final candidates = error.code == 'ACCOUNT_MAPPING_REQUIRED'
          ? await _loadAccountCandidates(repository)
          : null;
      setState(() {
        _stage = _ImportStage.idle;
        _errorMessage = '${error.message}（${error.code}）';
        _accountCandidates = candidates;
      });
    }
  }

  Future<List<ImportAccountView>?> _loadAccountCandidates(
    ImportRepository repository,
  ) async {
    try {
      return (await repository.accounts()).rows;
    } on LyraApiError {
      return null;
    }
  }

  Future<void> _commit() async {
    final repository = _resolveRepository();
    final preview = _preview;
    if (repository == null || preview == null || _busy) {
      return;
    }
    // 保存应用级容器：即使上传面板先关闭，成功提交仍需刷新账本读取缓存。
    final container = ProviderScope.containerOf(context, listen: false);
    setState(() => _stage = _ImportStage.committing);
    try {
      final result = await repository.commit(
        importId: preview.importId,
        confirmToken: preview.confirmToken,
        // 同一预览的重试共用一个幂等键：网络超时重放返回持久化结果，
        // 换新预览则生成新键。键值只含安全字符，明文不进入任何存储。
        idempotencyKey: 'commit-${preview.importId}',
      );
      if (result.changed) {
        // 会话持有权威汇总；依赖它的账户、持仓和报告 Provider 会一起重读。
        // 无新增的重复导入不改变版本，也不触发不必要的网络请求。
        container.invalidate(sessionProvider);
      }
      if (!mounted) return;
      setState(() {
        _result = result;
        _stage = _ImportStage.committed;
      });
    } on LyraApiError catch (error) {
      if (!mounted) return;
      setState(() {
        _stage = _ImportStage.previewReady;
        _errorMessage = '${error.message}（${error.code}）';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final preview = _preview;
    final result = _result;
    final candidates = _accountCandidates;
    return Padding(
      padding: const EdgeInsets.all(22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Row(
            children: <Widget>[
              const Icon(
                LyraIcons.importFile,
                size: 20,
                color: LyraColors.accent,
              ),
              const SizedBox(width: 10),
              Text(
                '导入 Schwab 文件',
                style: Theme.of(context).textTheme.titleLarge,
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '支持官方导出的 .csv 与 .xlsx；全部行校验通过后才会原子写入账本。',
            style: Theme.of(context).textTheme.bodyMedium
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 18),
          FilledButton.icon(
            onPressed: _busy ? null : () => _pickAndPreview(),
            icon: const Icon(LyraIcons.importFile),
            label: Text(
              _stage == _ImportStage.uploading ? '上传解析中…' : '选择文件并预览',
            ),
          ),
          if (candidates != null && candidates.isNotEmpty) ...<Widget>[
            const SizedBox(height: 14),
            Text(
              '请选择该文件对应的账户后重新预览：',
              style: Theme.of(context).textTheme.bodyMedium,
            ),
            const SizedBox(height: 8),
            for (final account in candidates)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: OutlinedButton.icon(
                  onPressed: _busy
                      ? null
                      : () => _pickAndPreview(accountId: account.id),
                  icon: const Icon(LyraIcons.ledger),
                  label: Text(
                    '${account.displayName}（${account.maskedSuffix}）',
                  ),
                ),
              ),
          ],
          if (preview != null) ...<Widget>[
            const SizedBox(height: 18),
            _PreviewStats(preview: preview),
            const SizedBox(height: 14),
            FilledButton.icon(
              onPressed: _stage == _ImportStage.committing ? null : _commit,
              icon: const Icon(LyraIcons.confirm),
              label: Text(
                preview.insertableRows == 0
                    ? '无新增交易，仍确认提交'
                    : '确认写入账本（${preview.insertableRows} 行）',
              ),
            ),
          ],
          if (result != null) ...<Widget>[
            const SizedBox(height: 14),
            Text(
              result.changed
                  ? '已写入账本：新增 ${result.insertedRows} 行，账本版本 ${result.ledgerVersion}'
                  : '无新增交易；账本版本保持 ${result.ledgerVersion}',
              style: Theme.of(context).textTheme.bodyLarge
                  ?.copyWith(fontWeight: FontWeight.w700),
            ),
          ],
          if (_errorMessage != null) ...<Widget>[
            const SizedBox(height: 12),
            Text(
              _errorMessage!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
        ],
      ),
    );
  }
}

class _PreviewStats extends StatelessWidget {
  const _PreviewStats({required this.preview});

  final ImportPreviewView preview;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: LyraColors.surfaceSubtle,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: LyraColors.hairlineStrong),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(
            preview.fileName,
            style: Theme.of(context).textTheme.titleMedium
                ?.copyWith(color: LyraColors.ink, fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 10),
          Text(
            '总行数 ${preview.totalRows} · 可新增 ${preview.insertableRows}'
            ' · 已存在 ${preview.existingRows}',
            style: const TextStyle(color: LyraColors.ink),
          ),
          const SizedBox(height: 6),
          Text(
            preview.byAction.entries
                .map((entry) => '${entry.key} × ${entry.value}')
                .join(' · '),
            style: Theme.of(context).textTheme.labelSmall
                ?.copyWith(color: LyraColors.ink.withValues(alpha: 0.72)),
          ),
        ],
      ),
    );
  }
}
