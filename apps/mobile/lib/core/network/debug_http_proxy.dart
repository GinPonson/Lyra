import 'dart:io';

import 'package:flutter/foundation.dart';

const String _debugHttpProxy = String.fromEnvironment('LYRA_DEBUG_HTTP_PROXY');

/// 为调试构建安装显式 HTTP 代理。
///
/// 仅当 [kDebugMode] 为 true 且通过 `LYRA_DEBUG_HTTP_PROXY` 提供了代理地址时
/// 才会修改全局 HTTP override。代理地址必须为不含协议、凭据或路径的 `host:port`，
/// 端口范围为 1–65535；非法地址抛出 [FormatException]。正式构建和未配置时不做修改。
void configureDebugHttpProxy() {
  if (!kDebugMode || _debugHttpProxy.isEmpty) return;

  final proxyAddress = validateDebugHttpProxyAddress(_debugHttpProxy);
  final existingOverrides = HttpOverrides.current;
  HttpOverrides.global = _DebugHttpProxyOverrides(
    proxyAddress: proxyAddress,
    parent: existingOverrides,
  );
  debugPrint('Lyra debug HTTP proxy enabled: $proxyAddress');
}

/// 验证代理地址仅包含主机和有效端口，并返回其规范化表示。
///
/// [address] 必须符合 `hostname:port`、`IPv4:port` 或 `[IPv6]:port` 格式，不能包含
/// scheme、用户凭据、路径、查询或片段。返回值保留输入主机名并将端口规范为十进制；
/// 格式或端口无效时抛出 [FormatException]。
String validateDebugHttpProxyAddress(String address) {
  final match = RegExp(r'^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]+)$')
      .firstMatch(address);
  if (match == null) {
    throw const FormatException(
      'LYRA_DEBUG_HTTP_PROXY must be a host:port address without a scheme or path.',
    );
  }

  final rawHost = match.group(1)!;
  final host = rawHost.startsWith('[')
      ? rawHost.substring(1, rawHost.length - 1)
      : rawHost;
  if (host.isEmpty ||
      (rawHost.startsWith('[') &&
          InternetAddress.tryParse(host)?.type != InternetAddressType.IPv6)) {
    throw const FormatException(
      'LYRA_DEBUG_HTTP_PROXY must contain a valid host and port.',
    );
  }

  final port = int.tryParse(match.group(2)!);
  if (port == null || port < 1 || port > 65535) {
    throw const FormatException(
      'LYRA_DEBUG_HTTP_PROXY port must be between 1 and 65535.',
    );
  }
  return '$rawHost:$port';
}

/// 为新建的 Dart HTTP 客户端指定调试代理，并委托已有 override 创建客户端。
class _DebugHttpProxyOverrides extends HttpOverrides {
  /// 保存代理地址与原有 override，供后续客户端创建时继续委托。
  ///
  /// [proxyAddress] 是已校验的 `host:port`；[parent] 是安装前的全局 HTTP override，
  /// 可以为空。
  _DebugHttpProxyOverrides({required this.proxyAddress, required this.parent});

  final String proxyAddress;
  final HttpOverrides? parent;

  /// 创建 HTTP 客户端并只设置其代理解析函数。
  ///
  /// [context] 是 TLS 安全上下文，会原样传给父 override（若存在）；返回的客户端
  /// 继承其余行为，包括证书校验策略。创建失败时异常向上传递。
  @override
  HttpClient createHttpClient(SecurityContext? context) {
    final client =
        parent?.createHttpClient(context) ?? super.createHttpClient(context);
    // 只替换代理选择函数，保留客户端默认或父 override 提供的 TLS 证书校验行为。
    client.findProxy = (_) => 'PROXY $proxyAddress';
    return client;
  }
}
