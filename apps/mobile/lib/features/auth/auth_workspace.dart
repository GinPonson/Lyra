import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../app/session.dart';
import '../system/system_repository.dart';
import 'auth_provider.dart';
import 'auth_repository.dart';

/// 登录方式发现只发送公开请求；与携带设备令牌的账本客户端独立。
final authProvidersProvider = FutureProvider.autoDispose
    .family<List<AuthProviderAvailability>, String>((ref, rawUrl) async {
      final url = normalizeLyraServerUrl(rawUrl);
      final dependencies = ref.watch(sessionDependenciesProvider);
      final dio =
          dependencies.clientFactory?.call(url) ??
          SystemRepository.createDio(url);
      final cancel = CancelToken();
      ref.onDispose(() {
        cancel.cancel('Login provider selection closed');
        dio.close(force: true);
      });
      return AuthRepository(dio: dio).providers(cancelToken: cancel);
    }, retry: (retryCount, error) => null);
