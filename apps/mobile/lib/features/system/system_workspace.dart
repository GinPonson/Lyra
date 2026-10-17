import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:package_info_plus/package_info_plus.dart';

import 'system_repository.dart';

typedef SystemDioFactory = Dio Function(String serverUrl);

/// 与带令牌的账本客户端分开；测试可以只替换公开服务检查。
final systemDioFactoryProvider = Provider<SystemDioFactory>(
  (ref) => SystemRepository.createDio,
);

final systemInfoProvider = FutureProvider.autoDispose
    .family<ServerConnectionCheck, String>((ref, rawUrl) async {
      final url = normalizeLyraServerUrl(rawUrl);
      final repository = SystemRepository(
        serverUrl: url,
        dio: ref.watch(systemDioFactoryProvider)(url),
      );
      final cancel = CancelToken();
      ref.onDispose(() {
        cancel.cancel('Connection check no longer visible');
        repository.close();
      });
      return repository.check(cancelToken: cancel);
    }, retry: (retryCount, error) => null);

/// 从实际安装包读取版本，随构建版本更新，不在页面硬编码。
final appVersionProvider = FutureProvider<String>((ref) async {
  final info = await PackageInfo.fromPlatform();
  if (info.version.isEmpty) throw const FormatException('应用版本信息不可用');
  return info.buildNumber.isEmpty
      ? info.version
      : '${info.version} (${info.buildNumber})';
}, retry: (retryCount, error) => null);
