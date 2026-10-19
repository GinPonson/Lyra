import '../../core/api/lyra_api_client.dart';
import '../../core/api/lyra_api_error.dart';

/// 将登录与注册失败代码映射为可执行的用户提示，不透传服务端原始错误。
String loginFailureMessage(LyraApiError error) => switch (error.code) {
  'LOGIN_BROWSER_UNAVAILABLE' => '无法打开浏览器，请重试。',
  'LOGIN_BROWSER_UNSUPPORTED' => '请更新或更换浏览器后重试。',
  'LOGIN_CALLBACK_INVALID' => '登录返回信息无效，请重新登录。',
  'LOGIN_EXPIRED' => '登录已超时，请重新开始。',
  'LOGIN_STATE_INVALID' || 'LOGIN_ALREADY_REDEEMED' => '此次登录已失效，请重新开始。',
  'AUTH_FORBIDDEN' => '此账号暂时无法登录 Lyra，请换一个账号。',
  'INVITE_CODE_REQUIRED' => '首次使用需要邀请码，请填写后重试。',
  'INVITE_CODE_INVALID' => '邀请码无效或已被使用，请确认后重试。',
  'LOGIN_DENIED' => '未能完成授权，请重试或换一个账号。',
  'AUTH_NOT_CONFIGURED' => '此登录方式暂不可用，请选择其他方式。',
  'LOGIN_RATE_LIMITED' => '尝试次数较多，请稍后重试。',
  'LOGIN_IN_PROGRESS' => '正在完成注册，请稍候。',
  'CLIENT_VERSION_INCOMPATIBLE' => '请更新 Lyra 后重试。',
  // 停用是终态：明确告知原因，不提供刷新或重试入口。
  kUserDisabledCode => '账号已停用，请联系管理员。',
  // 兑换阶段的通用失败（含自动注册事务失败）：明确失败并要求重新登录。
  'LOGIN_FAILED' => '登录未能完成，请重新登录。',
  _ when error.statusCode == 429 => '尝试次数较多，请稍后重试。',
  _ when error.statusCode == 0 => '暂时无法登录，请检查网络后重试。',
  // 服务端写入故障（含注册事务异常）：明确失败并允许重试，不假装成功。
  _ when error.statusCode >= 500 => '登录服务暂时不可用，请稍后重试。',
  _ => '登录未完成，请重试。',
};
