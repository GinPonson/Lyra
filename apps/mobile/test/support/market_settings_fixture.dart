import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:lyra_mobile/app/session.dart';
import 'package:lyra_mobile/core/security/credential_store.dart';

import '../session_flow_test.dart' show FakeLyraAdapter;
import 'market_fixture.dart';

/// A local, synthetic HTTP boundary. No real provider connection or key is used.
class MarketSettingsFixture extends MarketFixture {
  MarketSettingsFixture({
    this.emptyPositions = false,
    Map<String, Object?>? initialSettings,
  }) : settings = initialSettings ?? settingsPayload();

  final bool emptyPositions;
  Map<String, Object?> settings;
  bool failRead = false;
  int readFailureStatus = 503;
  ({String code, int status})? saveFailure;
  Completer<void>? nextReadGate;
  Completer<void>? nextPositionGate;
  Completer<void>? saveResponseGate;
  int? positionRevisionOverride;

  static Map<String, Object?> settingsPayload({
    int revision = 0,
    List<String> stock = const ['first-feed'],
    List<String> option = const ['second-feed'],
    bool secondConfigured = true,
  }) => {
    'revision': revision,
    'routes': {'stock': stock, 'option': option},
    'effectiveRoutes': {'stock': stock, 'option': option},
    'providers': [
      {
        'id': 'first-feed',
        'displayName': 'First Quotes',
        'assetTypes': ['stock', 'option'],
        'configured': true,
        'timing': {'kind': 'delayed', 'delayMinutes': 16},
        'cacheTtlSeconds': 900,
      },
      {
        'id': 'second-feed',
        'displayName': 'Second Data',
        'assetTypes': ['stock', 'option'],
        'configured': secondConfigured,
        'timing': {'kind': 'end_of_day', 'delayMinutes': null},
        'cacheTtlSeconds': 86400,
      },
    ],
    'updatedAt': revision == 0 ? null : '2026-10-11T04:00:00.000Z',
  };

  @override
  Future<SessionDependencies> dependencies({bool paired = true}) async {
    final store = MemoryCredentialStore();
    if (paired) {
      await store.write(
        const CredentialSnapshot(
          serverUrl: 'https://fixture.invalid',
          deviceId: 'test-device',
          accessToken: 'test-only-token',
          refreshToken: 'test-only-refresh',
          authMethod: 'google',
        ),
      );
    }
    return SessionDependencies(
      credentialStore: store,
      clientFactory: (url) => Dio(BaseOptions(baseUrl: url))
        ..httpClientAdapter = FakeLyraAdapter((options) async {
          requests.add(options);
          if (options.uri.path == '/v1/market/settings') {
            if (options.method == 'PUT') {
              final failure = saveFailure;
              if (failure != null) return _error(failure.code, failure.status);
              final data = (options.data as Map).cast<String, Object?>();
              if (data['expectedRevision'] != settings['revision']) {
                return _error('MARKET_SETTINGS_CHANGED', 409);
              }
              settings = {
                ...settings,
                'revision': (settings['revision']! as int) + 1,
                'routes': data['routes'],
                'effectiveRoutes': data['routes'],
                'updatedAt': '2026-10-11T04:00:00.000Z',
              };
              final saved = _clone(settings);
              await saveResponseGate?.future;
              return _body(saved);
            }
            if (failRead) return _error('QUERY_FAILED', readFailureStatus);
            final captured = _clone(settings);
            final gate = nextReadGate;
            nextReadGate = null;
            await gate?.future;
            return _body(captured);
          }
          if (options.uri.path == '/v1/market/positions') {
            final captured = marketResponse(options.queryParameters);
            final gate = nextPositionGate;
            nextPositionGate = null;
            await gate?.future;
            return _body(captured);
          }
          if (options.uri.path.endsWith('/revoke')) return _body({'ok': true});
          final command = options.uri.path.split('/').last;
          if (failures.contains(command)) return _error('QUERY_FAILED', 503);
          return _body(response(command, options.queryParameters));
        }),
    );
  }

  @override
  Map<String, Object?> payload(String command, Map<String, dynamic> query) {
    if (emptyPositions && command == 'positions') {
      return {'rows': <Object?>[], 'count': 0};
    }
    return super.payload(command, query);
  }

  @override
  Map<String, Object?> marketResponse(Map<String, dynamic> query) {
    final result = super.marketResponse(query);
    final data = result['data']! as Map<String, Object?>;
    final market = data['marketData']! as Map<String, Object?>;
    market['settingsRevision'] =
        positionRevisionOverride ?? settings['revision'];
    final routes = settings['routes']! as Map<String, Object?>;
    market['routes'] = routes;
    market['enabled'] =
        (routes['stock']! as List).isNotEmpty ||
        (routes['option']! as List).isNotEmpty;
    for (final raw in data['rows']! as List) {
      final row = raw as Map<String, Object?>;
      final quote = row['quote']! as Map<String, Object?>;
      final selected = routes[row['assetType']]! as List;
      final snapshot = quote['quote'] as Map<String, Object?>?;
      if (snapshot != null && selected.isNotEmpty) {
        snapshot['providerId'] = selected.first;
      }
    }
    return result;
  }

  Map<String, Object?> _clone(Map<String, Object?> value) =>
      (jsonDecode(jsonEncode(value)) as Map).cast<String, Object?>();

  ResponseBody _error(String code, int status) => _body({
    'code': code,
    'message': 'Synthetic fixture error',
    'retryable': true,
  }, status);

  ResponseBody _body(Map<String, Object?> value, [int status = 200]) =>
      ResponseBody.fromString(
        jsonEncode(value),
        status,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );
}
