/// Sources are supplied by the server catalog. The App never translates
/// provider-specific symbols, stores vendor credentials, or chooses a fallback.
class MarketSource {
  MarketSource.fromJson(Map<String, Object?> json)
    : id = _text(json['id']),
      displayName = _text(json['displayName']),
      assetTypes = _strings(json['assetTypes']),
      configured = _boolean(json['configured']),
      timingKind = _text(_object(json['timing'])['kind']),
      delayMinutes = _object(json['timing'])['delayMinutes'] == null
          ? null
          : _integer(_object(json['timing'])['delayMinutes']),
      cacheTtlSeconds = _integer(json['cacheTtlSeconds']);

  final String id;
  final String displayName;
  final List<String> assetTypes;
  final bool configured;
  final String timingKind;
  final int? delayMinutes;
  final int cacheTtlSeconds;

  bool supports(String assetType) => assetTypes.contains(assetType);

  String get timingLabel => switch (timingKind) {
    'end_of_day' => '历史行情',
    'delayed' => delayMinutes == null ? '延迟行情' : '延迟 $delayMinutes 分钟',
    _ => '以报价时点为准',
  };
}

class MarketRoutes {
  MarketRoutes({
    required Iterable<String> stock,
    required Iterable<String> option,
  }) : stock = List.unmodifiable(stock),
       option = List.unmodifiable(option) {
    for (final route in [this.stock, this.option]) {
      if (route.length > 2 ||
          route.any((id) => id.isEmpty) ||
          route.toSet().length != route.length) {
        throw const FormatException('行情来源顺序无效');
      }
    }
  }

  factory MarketRoutes.fromJson(Object? value) {
    final json = _object(value);
    return MarketRoutes(
      stock: _strings(json['stock']),
      option: _strings(json['option']),
    );
  }

  final List<String> stock;
  final List<String> option;

  List<String> forAsset(String assetType) =>
      assetType == 'stock' ? stock : option;

  MarketRoutes replace(String assetType, Iterable<String> route) =>
      MarketRoutes(
        stock: assetType == 'stock' ? route : stock,
        option: assetType == 'option' ? route : option,
      );

  Map<String, Object?> toJson() => {'stock': stock, 'option': option};

  @override
  bool operator ==(Object other) =>
      other is MarketRoutes &&
      _same(stock, other.stock) &&
      _same(option, other.option);

  @override
  int get hashCode =>
      Object.hash(Object.hashAll(stock), Object.hashAll(option));
}

class MarketSettings {
  MarketSettings.fromJson(Map<String, Object?> json)
    : revision = _integer(json['revision']),
      routes = MarketRoutes.fromJson(json['routes']),
      effectiveRoutes = MarketRoutes.fromJson(json['effectiveRoutes']),
      providers = List.unmodifiable(
        _array(json['providers'])
            .map((value) => MarketSource.fromJson(_object(value))),
      ),
      updatedAt = json['updatedAt'] == null ? null : _text(json['updatedAt']) {
    if (providers.map((source) => source.id).toSet().length !=
        providers.length) {
      throw const FormatException('行情来源列表包含重复项');
    }
    if (updatedAt != null && DateTime.tryParse(updatedAt!) == null) {
      throw const FormatException('行情设置时间无效');
    }
  }

  final int revision;
  final MarketRoutes routes;
  final MarketRoutes effectiveRoutes;
  final List<MarketSource> providers;
  final String? updatedAt;

  MarketSource? source(String id) {
    for (final source in providers) {
      if (source.id == id) return source;
    }
    return null;
  }

  bool canSelect(String assetType, String id) {
    final provider = source(id);
    return provider != null &&
        provider.configured &&
        provider.supports(assetType);
  }

  bool accepts(MarketRoutes selected) => ['stock', 'option'].every(
    (asset) => selected.forAsset(asset).every((id) => canSelect(asset, id)),
  );
}

bool _same(List<String> left, List<String> right) =>
    left.length == right.length &&
    Iterable<int>.generate(left.length)
        .every((index) => left[index] == right[index]);

Map<String, Object?> _object(Object? value) {
  if (value is! Map<String, Object?>) throw const FormatException('行情设置格式无效');
  return value;
}

List<Object?> _array(Object? value) {
  if (value is! List<Object?>) throw const FormatException('行情设置列表无效');
  return value;
}

List<String> _strings(Object? value) =>
    List.unmodifiable(_array(value).map(_text));

String _text(Object? value) {
  if (value is! String || value.isEmpty) {
    throw const FormatException('行情设置字段无效');
  }
  return value;
}

int _integer(Object? value) {
  if (value is! int || value < 0) throw const FormatException('行情设置版本或数量无效');
  return value;
}

bool _boolean(Object? value) {
  if (value is! bool) throw const FormatException('行情来源配置状态无效');
  return value;
}
