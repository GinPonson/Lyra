import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../core/design/lyra_primary_icon.dart';
import 'account_repository.dart';
import 'account_workspace.dart';

Future<BrokerageAccount?> showCreateAccountSheet(
  BuildContext context, {
  String? sourceFileName,
}) => showModalBottomSheet<BrokerageAccount>(
  context: context,
  isScrollControlled: true,
  isDismissible: false,
  enableDrag: false,
  useSafeArea: true,
  shape: const RoundedRectangleBorder(
    borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
  ),
  builder: (context) => Padding(
    padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
    child: SingleChildScrollView(
      child: AccountSheet(sourceFileName: sourceFileName),
    ),
  ),
);

/// 证券账户与登录身份分开；只在用户确认后提交创建。
class AccountSheet extends ConsumerStatefulWidget {
  const AccountSheet({super.key, this.sourceFileName});

  final String? sourceFileName;

  @override
  ConsumerState<AccountSheet> createState() => _AccountSheetState();
}

class _AccountSheetState extends ConsumerState<AccountSheet> {
  final _formKey = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _identifier = TextEditingController();
  final _currency = TextEditingController(text: 'USD');
  LyraApiClient? _client;
  AccountCreationRequest? _submittedRequest;
  LyraApiError? _error;
  bool _busy = false;
  bool _identityChanged = false;

  @override
  void initState() {
    super.initState();
    _client = ref.read(sessionProvider.notifier).client;
    _identifier.text =
        accountIdentifierFromSchwabFile(widget.sourceFileName) ?? '';
  }

  @override
  void dispose() {
    _name.dispose();
    _identifier.dispose();
    _currency.dispose();
    super.dispose();
  }

  bool _active(LyraApiClient client) =>
      mounted &&
      !_identityChanged &&
      identical(ref.read(sessionProvider.notifier).client, client);

  Future<void> _create() async {
    if (_busy || _identityChanged || !_formKey.currentState!.validate()) return;
    final client = _client ??= ref.read(sessionProvider.notifier).client;
    if (client == null) {
      setState(() {
        _error = const LyraApiError(
          code: 'AUTH_REQUIRED',
          message: '请先登录 Lyra',
          statusCode: 401,
          retryable: false,
        );
      });
      return;
    }
    if (!_active(client)) return;
    final previous = _submittedRequest;
    final request =
        previous != null &&
            previous.matches(
              displayName: _name.text,
              accountIdentifier: _identifier.text,
              baseCurrency: _currency.text,
            )
        ? previous
        : AccountCreationRequest(
            displayName: _name.text,
            accountIdentifier: _identifier.text,
            baseCurrency: _currency.text,
          );
    _submittedRequest = request;
    final container = ProviderScope.containerOf(context, listen: false);
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final account = await AccountRepository(client: client).create(request);
      if (!_active(client)) return;
      await refreshLedgerWorkspace(
        container: container,
        client: client,
        selectAccount: account,
      );
      if (!mounted || !_active(client)) return;
      setState(() => _busy = false);
      Navigator.pop(context, account);
    } on Object catch (error) {
      if (!_active(client)) return;
      setState(() {
        _busy = false;
        _error = LyraApiError.from(error);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(sessionProvider, (previous, next) {
      if (_client != null &&
          !identical(_client, ref.read(sessionProvider.notifier).client) &&
          !_identityChanged) {
        setState(() {
          _identityChanged = true;
          _busy = false;
          _submittedRequest = null;
          _error = null;
          _name.clear();
          _identifier.clear();
        });
      }
    });
    final disabled = _busy || _identityChanged;
    final error = _error;
    return PopScope(
      canPop: !_busy,
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Form(
          key: _formKey,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  const LyraPrimaryIcon(
                    LyraPrimaryGlyph.brokerage,
                    color: LyraColors.accent,
                    size: 22,
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      '新增证券账户',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  IconButton(
                    tooltip: '关闭',
                    onPressed: _busy ? null : () => Navigator.pop(context),
                    icon: const Icon(LyraIcons.dismiss),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              const Text(
                '登录账号用于访问 Lyra；证券账户用于归集你导入的交易记录。',
                style: TextStyle(color: LyraColors.inkSecondary, height: 1.6),
              ),
              const SizedBox(height: 20),
              TextFormField(
                key: const ValueKey('account-name'),
                controller: _name,
                enabled: !disabled,
                maxLength: 64,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(
                  labelText: '账户名称',
                  hintText: '例如：Schwab 主账户',
                  counterText: '',
                ),
                validator: (value) {
                  final name = value?.trim() ?? '';
                  if (name.isEmpty) return '请填写便于识别的账户名称';
                  if (name.length > 64 ||
                      RegExp(r'[\x00-\x1F\x7F]').hasMatch(name)) {
                    return '账户名称须为 1–64 个有效字符';
                  }
                  return null;
                },
              ),
              const SizedBox(height: 16),
              TextFormField(
                key: const ValueKey('account-identifier'),
                controller: _identifier,
                enabled: !disabled,
                autocorrect: false,
                textCapitalization: TextCapitalization.characters,
                textInputAction: TextInputAction.next,
                maxLength: 32,
                decoration: const InputDecoration(
                  labelText: 'Schwab 文件账户标识',
                  hintText: '例如：XXX276',
                  helperText: '使用文件名 Individual_XXX276_… 中的 XXX276，无需完整账号。',
                  helperMaxLines: 3,
                  counterText: '',
                ),
                validator: (value) => isValidAccountIdentifier(value ?? '')
                    ? null
                    : '填写 4–32 位字母、数字或连字符，末尾至少 3 位数字',
              ),
              if (accountIdentifierFromSchwabFile(widget.sourceFileName) !=
                  null)
                const Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Text(
                    '已从所选文件名填写标识，请核对后创建。',
                    style: TextStyle(
                      fontSize: 13,
                      height: 1.5,
                      color: LyraColors.accent,
                    ),
                  ),
                ),
              const SizedBox(height: 16),
              TextFormField(
                key: const ValueKey('account-currency'),
                controller: _currency,
                enabled: !disabled,
                autocorrect: false,
                textCapitalization: TextCapitalization.characters,
                textInputAction: TextInputAction.done,
                maxLength: 3,
                decoration: const InputDecoration(
                  labelText: '记账币种',
                  helperText: '与券商文件一致，例如 USD。不同币种分别展示。',
                  helperMaxLines: 2,
                  counterText: '',
                ),
                validator: (value) =>
                    RegExp(r'^[A-Z]{3}$')
                        .hasMatch(value?.trim().toUpperCase() ?? '')
                    ? null
                    : '请输入三位币种代码，例如 USD',
                onFieldSubmitted: (_) => _create(),
              ),
              if (_identityChanged) ...[
                const SizedBox(height: 16),
                const Text('登录状态已变化，请关闭后重新打开。'),
              ],
              if (error != null) ...[
                const SizedBox(height: 16),
                Text(
                  error.code == 'ACCOUNT_ALREADY_EXISTS'
                      ? '该证券账户已存在，请返回并选择已有账户。'
                      : '${error.message}（${error.code}）',
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
                if (error.code == 'ACCOUNT_ALREADY_EXISTS') ...[
                  const SizedBox(height: 8),
                  OutlinedButton.icon(
                    onPressed: () {
                      ref.invalidate(brokerageAccountsProvider);
                      Navigator.pop(context);
                    },
                    icon: const Icon(LyraIcons.accountScope, size: 18),
                    label: const Text('返回选择已有账户'),
                  ),
                ],
              ],
              const SizedBox(height: 24),
              FilledButton.icon(
                key: const ValueKey('create-account-submit'),
                onPressed: disabled ? null : _create,
                icon: const Icon(LyraIcons.confirm, size: 18),
                label: Text(_busy ? '创建中…' : '创建证券账户'),
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: _busy ? null : () => Navigator.pop(context),
                child: Text(_identityChanged ? '关闭' : '取消'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
