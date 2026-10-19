import 'package:dio/dio.dart';
import 'package:flutter/services.dart';

import '../../core/api/lyra_api_error.dart';

const _channel = MethodChannel('lyra/auth');

/// 服务端验证完成后的固定回跳；不携带身份、OAuth code 或 Lyra 凭据。
const authorizationReturnUrl = 'com.lyrafinance.lyra://auth/callback';

/// 打开 Android 系统授权窗口，并等待窗口关闭、App 恢复后再允许兑换凭据。
///
/// [url] 是已由登录仓储核对来源的 HTTPS 授权地址；[cancelToken] 同时取消
/// 浏览器等待和后续兑换。有效回跳返回 true，用户关闭窗口抛出
/// `LOGIN_CANCELLED`，浏览器不可用或回跳不符时抛出明确的 [LyraApiError]。
/// 未预期的原生错误直接向上传递，不回落到普通浏览器或 WebView。
Future<bool> authorizeInBrowser(Uri url, CancelToken cancelToken) async {
  if (cancelToken.isCancelled) throw cancelToken.cancelError!;
  var completed = false;
  try {
    final callback = await Future.any<String?>([
      _channel.invokeMethod<String>('authenticate', {'url': url.toString()}),
      cancelToken.whenCancel.then((error) async {
        // 成功完成后的旧 CancelToken 不得关闭下一轮登录的授权窗口。
        if (completed) return null;
        await _channel.invokeMethod<void>('cancel');
        throw error;
      }),
    ]);
    if (cancelToken.isCancelled) throw cancelToken.cancelError!;
    if (callback == null) {
      throw const LyraApiError(
        code: 'LOGIN_CANCELLED',
        message: '已取消登录。',
        statusCode: 0,
        retryable: false,
      );
    }
    // 回跳只负责关闭窗口；身份是否通过仍以绑定 PKCE 的兑换响应为准。
    if (callback != authorizationReturnUrl) {
      throw const LyraApiError(
        code: 'LOGIN_CALLBACK_INVALID',
        message: '登录返回信息无效，请重新登录。',
        statusCode: 400,
        retryable: true,
      );
    }
    return true;
  } on PlatformException catch (error) {
    switch (error.code) {
      case 'AUTH_BROWSER_UNAVAILABLE':
        throw const LyraApiError(
          code: 'LOGIN_BROWSER_UNAVAILABLE',
          message: '无法打开浏览器，请重试。',
          statusCode: 0,
          retryable: true,
        );
      case 'AUTH_BROWSER_UNSUPPORTED':
        throw const LyraApiError(
          code: 'LOGIN_BROWSER_UNSUPPORTED',
          message: '请更新或更换浏览器后重试。',
          statusCode: 0,
          retryable: true,
        );
      case 'AUTH_CALLBACK_INVALID':
        throw const LyraApiError(
          code: 'LOGIN_CALLBACK_INVALID',
          message: '登录返回信息无效，请重新登录。',
          statusCode: 400,
          retryable: true,
        );
      default:
        rethrow;
    }
  } finally {
    completed = true;
  }
}
