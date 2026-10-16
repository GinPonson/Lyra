import '../../core/api/lyra_api_client.dart';
import 'auth_provider.dart';

/// 账号状态；与服务端 `users.status` 的冻结取值一一对应。
enum LyraUserStatus { active, disabled }

/// 账号角色；与服务端 `users.role` 的冻结取值一一对应。
///
/// 普通用户只使用 `member`；首版没有 Flutter 管理后台，角色只用于判定是否
/// 允许展示管理员专用能力，不能由客户端自行提升。
enum LyraUserRole { admin, member }

/// 当前设备会话已验证的第三方身份。
///
/// 对应 P0 冻结契约 §3.9 中 `user.identity` 子对象：身份来自服务端按设备会话
/// 解析出的已验证关联，不接受请求参数指定，也不作为账本归属依据。
class LyraUserIdentity {
  const LyraUserIdentity({
    required this.provider,
    required this.subject,
    this.email,
  });

  final AuthProvider provider;
  final String subject;
  final String? email;
}

/// 登录响应与 `GET /v1/me` 共用的 `user` 对象。
///
/// [userId] 是唯一身份锚点：本机偏好、账户范围与缓存都按它隔离，邮箱与第三方
/// subject 都不参与归属判断。任一字段缺失或形态不符都抛出 [FormatException]，
/// 不做猜测性兜底。
class LyraUserProfile {
  const LyraUserProfile({
    required this.userId,
    required this.displayName,
    required this.status,
    required this.role,
    required this.identity,
  });

  final String userId;

  /// 用户在 Lyra 的显示名；服务端返回空串时按缺失处理，由界面回落到提供方名称。
  final String? displayName;
  final LyraUserStatus status;
  final LyraUserRole role;
  final LyraUserIdentity identity;

  factory LyraUserProfile.fromJson(Map<String, Object?> json) {
    final userId = _stableText(json['userId']);
    final status = _enumValue(LyraUserStatus.values, json['status']);
    final role = _enumValue(LyraUserRole.values, json['role']);
    final displayName = json['displayName'];
    final identity = json['identity'];
    if (userId == null ||
        status == null ||
        role == null ||
        displayName is! String ||
        identity is! Map<String, Object?>) {
      throw const FormatException('用户身份响应格式无效');
    }
    final providerId = identity['provider'];
    final provider = providerId is String
        ? AuthProvider.fromId(providerId)
        : null;
    final subject = _stableText(identity['subject']);
    final email = identity['email'];
    if (provider == null ||
        subject == null ||
        (email != null && email is! String)) {
      throw const FormatException('用户身份响应格式无效');
    }
    return LyraUserProfile(
      userId: userId,
      displayName: displayName.trim().isEmpty ? null : displayName,
      status: status,
      role: role,
      identity: LyraUserIdentity(
        provider: provider,
        subject: subject,
        email: email as String?,
      ),
    );
  }
}

/// 当前用户身份查询；与登录响应共用 [LyraUserProfile] 解析器。
///
/// 身份只由服务端按访问令牌与设备会话解析，客户端不传任何用户标识。
class UserProfileRepository {
  UserProfileRepository(this._apiClient);

  final LyraApiClient _apiClient;

  /// 读取服务端为当前设备会话解析出的用户身份（`GET /v1/me`）。
  ///
  /// 返回登录响应中的同一个 `user` 对象形状；401 由 [LyraApiClient] 统一刷新
  /// 或结束会话，响应缺少 `user` 对象时抛出 [FormatException]。
  Future<LyraUserProfile> me() async {
    final response = await _apiClient.get<Map<String, Object?>>(
      '/v1/me',
      const <String, String>{},
    );
    final body = response.data;
    final user = body is Map<String, Object?> ? body['user'] : null;
    if (user is! Map<String, Object?>) {
      throw const FormatException('/v1/me 响应缺少 user 对象');
    }
    return LyraUserProfile.fromJson(user);
  }
}

/// 稳定标识文本：非空、无首尾空白、长度受限且不含控制字符。
String? _stableText(Object? value) {
  if (value is! String ||
      value.isEmpty ||
      value.length > 128 ||
      value.trim() != value ||
      RegExp(r'[\x00-\x1f\x7f]').hasMatch(value)) {
    return null;
  }
  return value;
}

/// 按冻结的枚举名解析；未知取值返回 null 由调用方拒绝，不做近似匹配。
T? _enumValue<T extends Enum>(List<T> values, Object? raw) {
  for (final value in values) {
    if (value.name == raw) return value;
  }
  return null;
}
