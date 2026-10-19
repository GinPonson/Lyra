import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app/lyra_app.dart';
import 'core/network/debug_http_proxy.dart';
import 'core/preferences/app_preferences.dart';

/// Lyra Android 应用入口。
///
/// 首屏使用 edge-to-edge 系统栏；每个页面仍通过 SafeArea 保护可交互内容，
/// 避免状态栏、手势导航区和挖孔屏遮挡。
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  configureDebugHttpProxy();
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.transparent,
      statusBarIconBrightness: Brightness.dark,
      systemNavigationBarColor: Colors.transparent,
      systemNavigationBarIconBrightness: Brightness.dark,
      systemNavigationBarContrastEnforced: false,
    ),
  );
  runApp(
    ProviderScope(
      overrides: [
        appPreferencesStoreProvider.overrideWithValue(
          const SecureAppPreferencesStore(),
        ),
      ],
      child: const LyraApp(),
    ),
  );
}
