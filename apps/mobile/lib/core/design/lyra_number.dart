/// 账本十进制字符串的展示格式；不参与计算，也不改写服务端原值。
abstract final class LyraNumber {
  /// 输入精确十进制字符串，输出千分位及指定小数位（默认两位）。
  /// 使用 BigInt 按绝对值四舍五入，避免浮点误差；非零微额明确显示阈值。
  /// `—` 是界面明确的缺失状态，非法数值则抛出 FormatException。
  static String amount(String value, {int decimals = 2}) {
    if (value == '—') return value;
    if (decimals < 0) throw ArgumentError.value(decimals, 'decimals');
    final parts = _parts(value);
    final negative = value.startsWith('-');
    final fraction = parts.$2.padRight(decimals + 1, '0');
    var scaled = BigInt.parse('${parts.$1}${fraction.substring(0, decimals)}');
    if (int.parse(fraction[decimals]) >= 5) scaled += BigInt.one;
    final nonzero = RegExp('[1-9]').hasMatch(value);
    if (scaled == BigInt.zero && nonzero && decimals > 0) {
      final threshold = '0.${'0' * (decimals - 1)}1';
      return negative ? '> −$threshold' : '< $threshold';
    }
    final digits = scaled.toString().padLeft(decimals + 1, '0');
    final split = digits.length - decimals;
    final whole = _group(digits.substring(0, split));
    final suffix = decimals == 0 ? '' : '.${digits.substring(split)}';
    return '${negative && scaled != BigInt.zero ? '-' : ''}$whole$suffix';
  }

  /// 数量不舍入，只去掉无意义的末尾零，保留碎股和合约原始精度。
  static String quantity(String value) {
    if (value == '—') return value;
    final parts = _parts(value);
    final whole = BigInt.parse(parts.$1).toString();
    final fraction = parts.$2.replaceFirst(RegExp(r'0+$'), '');
    final negative = value.startsWith('-') && RegExp('[1-9]').hasMatch(value);
    return '${negative ? '-' : ''}${_group(whole)}${fraction.isEmpty ? '' : '.$fraction'}';
  }

  /// 校验后拆分整数、小数绝对值；禁止将非法响应默认为零。
  static (String, String) _parts(String value) {
    final match = RegExp(r'^-?(\d+)(?:\.(\d+))?$').firstMatch(value);
    if (match == null) throw FormatException('无效账本数值', value);
    return (match.group(1)!, match.group(2) ?? '');
  }

  static String _group(String digits) =>
      digits.replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (_) => ',');
}
