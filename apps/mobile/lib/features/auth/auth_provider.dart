/// 登录身份提供方；服务端 ID 与客户端允许的浏览器授权入口成对固定。
enum AuthProvider {
  google('google', 'Google', 'accounts.google.com', '/o/oauth2/v2/auth'),
  linuxdo('linuxdo', 'Linux.do', 'connect.linux.do', '/oauth2/authorize'),
  github('github', 'GitHub', 'github.com', '/login/oauth/authorize');

  const AuthProvider(
    this.id,
    this.label,
    this.authorizationHost,
    this.authorizationPath,
  );

  final String id;
  final String label;
  final String authorizationHost;
  final String authorizationPath;

  static AuthProvider? fromId(String? id) {
    for (final provider in values) {
      if (provider.id == id) return provider;
    }
    return null;
  }

  bool acceptsAuthorizationUrl(Uri url) =>
      url.scheme == 'https' &&
      url.host == authorizationHost &&
      url.port == 443 &&
      url.path == authorizationPath &&
      url.userInfo.isEmpty &&
      !url.hasFragment;
}

class AuthProviderAvailability {
  const AuthProviderAvailability({
    required this.provider,
    required this.available,
  });

  final AuthProvider provider;
  final bool available;
}

/// 登录方式目录；`registrationRequiresInvite` 决定登录页是否展示邀请码输入框。
class AuthProviderCatalog {
  const AuthProviderCatalog({
    required this.providers,
    required this.registrationRequiresInvite,
  });

  final List<AuthProviderAvailability> providers;

  /// 旧服务端不带该字段：缺省按不需要邀请码处理，否则旧版本会无法注册。
  final bool registrationRequiresInvite;

  factory AuthProviderCatalog.parse(Map<String, Object?> json) {
    final entries = json['providers'];
    if (entries is! List || entries.length != AuthProvider.values.length) {
      throw const FormatException('登录方式响应格式无效');
    }
    final requiresInvite = json['registrationRequiresInvite'];
    // 字段存在但类型不对时按契约失败，不猜测其含义。
    if (requiresInvite != null && requiresInvite is! bool) {
      throw const FormatException('登录方式响应格式无效');
    }
    final result = <AuthProvider, AuthProviderAvailability>{};
    for (final entry in entries) {
      if (entry is! Map ||
          entry['id'] is! String ||
          entry['name'] is! String ||
          entry['available'] is! bool) {
        throw const FormatException('登录方式响应格式无效');
      }
      final provider = AuthProvider.fromId(entry['id'] as String);
      if (provider == null || result.containsKey(provider)) {
        throw const FormatException('登录方式响应格式无效');
      }
      result[provider] = AuthProviderAvailability(
        provider: provider,
        available: entry['available'] as bool,
      );
    }
    return AuthProviderCatalog(
      providers: List.unmodifiable(
        AuthProvider.values.map((provider) => result[provider]!),
      ),
      registrationRequiresInvite: requiresInvite == true,
    );
  }
}
