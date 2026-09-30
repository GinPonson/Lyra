import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/lyra_api_error.dart';
import '../../core/design/lyra_colors.dart';
import '../../core/design/lyra_icons.dart';
import '../../app/session.dart';

/// 未配对状态下的设备配对表单。
///
/// 配对码由服务端持有方（CLI 或已配对设备）生成；App 只提交服务端地址、
/// 配对码与设备名完成兑换。成功后凭据进入系统安全存储。
class PairingPanel extends ConsumerStatefulWidget {
  const PairingPanel({super.key});

  @override
  ConsumerState<PairingPanel> createState() => _PairingPanelState();
}

class _PairingPanelState extends ConsumerState<PairingPanel> {
  final TextEditingController _serverUrl = TextEditingController();
  final TextEditingController _pairingCode = TextEditingController();
  final TextEditingController _deviceName = TextEditingController(
    text: '我的设备',
  );
  bool _submitting = false;
  String? _errorMessage;

  @override
  void dispose() {
    _serverUrl.dispose();
    _pairingCode.dispose();
    _deviceName.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_submitting) {
      return;
    }
    final server = Uri.tryParse(_serverUrl.text.trim());
    if (server == null || !['http', 'https'].contains(server.scheme) || server.host.isEmpty || _pairingCode.text.trim().isEmpty || _deviceName.text.trim().isEmpty) {
      setState(() => _errorMessage = '请输入有效的服务端地址、配对码与设备名称');
      return;
    }
    setState(() {
      _submitting = true;
      _errorMessage = null;
    });
    try {
      await ref.read(sessionProvider.notifier).pair(
            serverUrl: _serverUrl.text.trim(),
            code: _pairingCode.text.trim(),
            displayName: _deviceName.text.trim(),
          );
    } on LyraApiError catch (error) {
      if (!mounted) return;
      setState(() {
        _errorMessage = '${error.message}（${error.code}）';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _errorMessage = '配对未完成：$error';
      });
    } finally {
      if (mounted) {
        setState(() {
          _submitting = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        // 纸白表单与主画布同一色系，字段用边界区分。
        color: LyraColors.surfaceSubtle,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: <Widget>[
          Row(
            children: <Widget>[
              const Icon(
                LyraIcons.security,
                size: 20,
                color: LyraColors.accent,
              ),
              const SizedBox(width: 10),
              Text('设备配对', style: Theme.of(context).textTheme.titleLarge),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '填入服务端地址与一次性配对码。',
            style: Theme.of(context).textTheme.bodyMedium
                ?.copyWith(color: LyraColors.muted),
          ),
          const SizedBox(height: 18),
          TextField(
            controller: _serverUrl,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: '服务端地址',
              hintText: 'https://lyra.example.com',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _pairingCode,
            autocorrect: false,
            textCapitalization: TextCapitalization.characters,
            decoration: const InputDecoration(
              labelText: '配对码',
              hintText: 'XXXXX-XXXXX',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _deviceName,
            decoration: const InputDecoration(
              labelText: '设备名称',
              border: OutlineInputBorder(),
            ),
          ),
          if (_errorMessage != null) ...<Widget>[
            const SizedBox(height: 12),
            Text(
              _errorMessage!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ],
          const SizedBox(height: 18),
          FilledButton.icon(
            onPressed: _submitting ? null : _submit,
            icon: _submitting
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(LyraIcons.security),
            label: const Text('完成配对'),
          ),
        ],
      ),
    );
  }
}
