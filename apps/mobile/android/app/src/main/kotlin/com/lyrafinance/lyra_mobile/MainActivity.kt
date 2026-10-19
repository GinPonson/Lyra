package com.lyrafinance.lyra_mobile

import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResultLauncher
import androidx.browser.auth.AuthTabIntent
import androidx.browser.customtabs.CustomTabsClient
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.UUID

/**
 * 承载 Flutter 页面，并通过 AndroidX Auth Tab 处理浏览器登录。
 *
 * 每次授权使用独立的 Activity Result registry key，避免已取消的旧页面迟到回调完成新登录。
 */
class MainActivity : FlutterFragmentActivity() {
    private var authChannel: MethodChannel? = null
    private var activeAuthAttempt: AuthAttempt? = null

    /**
     * Flutter engine 可用后安装原生认证通道。
     *
     * @param flutterEngine 用于接收 Dart 认证调用的 Flutter 引擎。
     */
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        authChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, AUTH_CHANNEL).also { channel ->
            channel.setMethodCallHandler(::handleAuthMethodCall)
        }
    }

    /**
     * 将 Dart 认证方法分发给原生 Auth Tab 实现。
     *
     * @param call 从 Dart 收到的方法名与参数。
     * @param result 完成此次 Dart 方法调用的回调。
     */
    private fun handleAuthMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            METHOD_AUTHENTICATE -> startAuthentication(call, result)
            METHOD_CANCEL -> cancelAuthentication(result)
            else -> result.notImplemented()
        }
    }

    /**
     * 校验 HTTPS 授权地址与浏览器能力，并启动 Auth Tab。
     *
     * @param call 包含必需 `url` 字符串参数的方法调用。
     * @param result 在 Auth Tab 成功或取消后完成的回调。
     */
    private fun startAuthentication(call: MethodCall, result: MethodChannel.Result) {
        if (activeAuthAttempt != null) {
            result.error(AUTH_ALREADY_ACTIVE, "An authentication attempt is already active.", null)
            return
        }

        val urlString = call.argument<String>(ARGUMENT_URL)
        val authUri = parseAuthorizationUri(urlString)
        if (authUri == null) {
            result.error(AUTH_INVALID_URL, "The authorization URL must be a valid HTTPS URL.", null)
            return
        }

        val browserPackage = CustomTabsClient.getPackageName(applicationContext, null)
        if (browserPackage == null) {
            result.error(AUTH_BROWSER_UNAVAILABLE, "No Custom Tabs browser is available.", null)
            return
        }
        if (!CustomTabsClient.isAuthTabSupported(applicationContext, browserPackage)) {
            result.error(AUTH_BROWSER_UNSUPPORTED, "The selected browser does not support Auth Tab.", null)
            return
        }

        val attempt = AuthAttempt(UUID.randomUUID().toString(), result)
        activeAuthAttempt = attempt

        try {
            // 1.10.0 提供公开结果合约，允许每次尝试使用独立 key 隔离迟到结果。
            attempt.launcher = activityResultRegistry.register(
                "${AUTH_REGISTRY_KEY_PREFIX}${attempt.id}",
                AuthTabIntent.AuthenticateUserResultContract(),
            ) { authResult ->
                handleAuthResult(attempt, authResult)
            }
            val authTabIntent = AuthTabIntent.Builder().build()
            // 固定到刚才完成能力检查的浏览器，避免启动时系统改选另一个 provider。
            authTabIntent.intent.setPackage(browserPackage)
            authTabIntent.launch(attempt.launcher!!, authUri, AUTH_CALLBACK_SCHEME)
        } catch (error: ActivityNotFoundException) {
            completeAuthError(attempt, AUTH_BROWSER_UNAVAILABLE, "The Auth Tab activity could not be opened.", error)
        } catch (error: Exception) {
            completeAuthError(attempt, AUTH_PLATFORM_ERROR, "The Auth Tab could not be started.", error)
        }
    }

    /**
     * Auth Tab 返回结果后完成对应的 Dart 请求。
     *
     * @param attempt registry 回调捕获的登录尝试，可能已经过期。
     * @param authResult AndroidX Browser 返回的结果码与可选回调 URI。
     */
    private fun handleAuthResult(attempt: AuthAttempt, authResult: AuthTabIntent.AuthResult) {
        if (activeAuthAttempt !== attempt) {
            unregisterLauncher(attempt)
            return
        }

        when (authResult.resultCode) {
            AuthTabIntent.RESULT_CANCELED -> completeAuthSuccess(attempt, null)
            AuthTabIntent.RESULT_OK -> {
                if (isExpectedCallback(authResult.resultUri)) {
                    // 仅返回固定回跳地址，不把 Auth Tab 返回数据当作登录令牌。
                    completeAuthSuccess(attempt, AUTH_CALLBACK_URI)
                } else {
                    completeAuthError(
                        attempt,
                        AUTH_CALLBACK_INVALID,
                        "Auth Tab returned an unexpected callback URI.",
                        null,
                    )
                }
            }
            AuthTabIntent.RESULT_VERIFICATION_FAILED -> completeAuthError(
                attempt,
                AUTH_VERIFICATION_FAILED,
                "The browser could not verify the authentication redirect.",
                null,
            )
            AuthTabIntent.RESULT_VERIFICATION_TIMED_OUT -> completeAuthError(
                attempt,
                AUTH_VERIFICATION_TIMED_OUT,
                "The browser timed out while verifying the authentication redirect.",
                null,
            )
            else -> completeAuthError(
                attempt,
                AUTH_PLATFORM_ERROR,
                "Auth Tab returned an unknown result code: ${authResult.resultCode}.",
                null,
            )
        }
    }

    /**
     * 取消当前 Dart 请求，并将应用 Activity 带回前台。
     *
     * @param result 单独的 Dart `cancel` 方法调用的完成回调。
     */
    private fun cancelAuthentication(result: MethodChannel.Result) {
        val attempt = activeAuthAttempt
        if (attempt == null) {
            result.success(null)
            return
        }

        // 先注销本次回调，避免返回应用后旧浏览器结果完成当前或后续登录。
        activeAuthAttempt = null
        unregisterLauncher(attempt)

        try {
            // 浏览器页处于调用方任务栈时，CLEAR_TOP 会移除其上方页面并回到 MainActivity。
            startActivity(
                Intent(this, MainActivity::class.java).addFlags(
                    Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP,
                ),
            )
            attempt.methodResult.success(null)
            result.success(null)
        } catch (error: Exception) {
            attempt.methodResult.error(AUTH_PLATFORM_ERROR, "The authentication attempt was canceled, but the app could not be resumed.", error.toString())
            result.error(AUTH_PLATFORM_ERROR, "The app could not be resumed after canceling authentication.", error.toString())
        }
    }

    /**
     * 以字符串结果或取消时的 null 完成一次登录尝试。
     *
     * @param attempt 持有待完成 Dart 回调的登录尝试。
     * @param value 成功时的固定回跳 URI；用户取消时为 null。
     */
    private fun completeAuthSuccess(attempt: AuthAttempt, value: String?) {
        if (activeAuthAttempt !== attempt) return
        activeAuthAttempt = null
        unregisterLauncher(attempt)
        attempt.methodResult.success(value)
    }

    /**
     * 以明确的平台错误完成一次登录尝试，并向 Dart 暴露底层异常。
     *
     * @param attempt 持有待完成 Dart 回调的登录尝试。
     * @param code 提供给 Dart 的稳定错误码。
     * @param message 不含敏感数据的错误说明。
     * @param error 可选的底层异常。
     */
    private fun completeAuthError(
        attempt: AuthAttempt,
        code: String,
        message: String,
        error: Exception?,
    ) {
        if (activeAuthAttempt !== attempt) {
            unregisterLauncher(attempt)
            return
        }

        activeAuthAttempt = null
        unregisterLauncher(attempt)
        attempt.methodResult.error(code, message, error?.toString())
    }

    /**
     * 登录成功、取消或启动失败后释放 registry 回调。
     *
     * @param attempt 需要注销其 launcher 的登录尝试；未创建 launcher 时不执行操作。
     */
    private fun unregisterLauncher(attempt: AuthAttempt) {
        attempt.launcher?.unregister()
        attempt.launcher = null
    }

    /**
     * 将 Dart 传入的授权地址解析为 HTTPS URI，并在交给浏览器前进行校验。
     *
     * @param value 从 Dart 收到的可空 URL 字符串。
     * @return 带主机名的分层 HTTPS URI；输入无效时返回 null。
     */
    private fun parseAuthorizationUri(value: String?): Uri? {
        if (value.isNullOrBlank()) return null
        val uri = Uri.parse(value)
        if (!uri.isHierarchical || !uri.scheme.equals(HTTPS_SCHEME, ignoreCase = true)) return null
        if (uri.host.isNullOrBlank() || uri.userInfo != null) return null
        return uri
    }

    /**
     * 校验浏览器结果是否与应用固定回跳地址完全一致且不含任何附加数据。
     *
     * @param uri Auth Tab 返回的可空 URI。
     * @return 仅当 URI 精确为 `com.lyrafinance.lyra://auth/callback` 时返回 true。
     */
    private fun isExpectedCallback(uri: Uri?): Boolean {
        return uri != null && uri.toString() == AUTH_CALLBACK_URI
    }

    /** 保存一次登录尝试的结果回调和独立 launcher。 */
    private class AuthAttempt(
        val id: String,
        val methodResult: MethodChannel.Result,
        var launcher: ActivityResultLauncher<Intent>? = null,
    )

    private companion object {
        const val AUTH_CHANNEL = "lyra/auth"
        const val METHOD_AUTHENTICATE = "authenticate"
        const val METHOD_CANCEL = "cancel"
        const val ARGUMENT_URL = "url"
        const val HTTPS_SCHEME = "https"
        const val AUTH_CALLBACK_SCHEME = "com.lyrafinance.lyra"
        const val AUTH_CALLBACK_URI = "com.lyrafinance.lyra://auth/callback"
        const val AUTH_REGISTRY_KEY_PREFIX = "lyra-auth-"

        const val AUTH_INVALID_URL = "AUTH_INVALID_URL"
        const val AUTH_BROWSER_UNAVAILABLE = "AUTH_BROWSER_UNAVAILABLE"
        const val AUTH_BROWSER_UNSUPPORTED = "AUTH_BROWSER_UNSUPPORTED"
        const val AUTH_ALREADY_ACTIVE = "AUTH_ALREADY_ACTIVE"
        const val AUTH_CALLBACK_INVALID = "AUTH_CALLBACK_INVALID"
        const val AUTH_VERIFICATION_FAILED = "AUTH_VERIFICATION_FAILED"
        const val AUTH_VERIFICATION_TIMED_OUT = "AUTH_VERIFICATION_TIMED_OUT"
        const val AUTH_PLATFORM_ERROR = "AUTH_PLATFORM_ERROR"
    }
}
