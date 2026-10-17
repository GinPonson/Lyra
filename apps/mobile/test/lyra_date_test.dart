import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/design/lyra_date.dart';

void main() {
  test('数据时间统一为 UTC，保留秒数并正确换日', () {
    expect(lyraDateTime('2026-09-08T08:00:03.250Z'), '2026年9月8日 08:00:03 UTC');
    expect(lyraDateTime('2026-09-08T01:30:03+08:00'), '2026年9月7日 17:30:03 UTC');
    expect(lyraDateTime('2026-09-08T23:30:03-07:00'), '2026年9月9日 06:30:03 UTC');
  });

  test('只有日期的记录不补造时分秒', () {
    expect(lyraDateTime('2024-02-29'), '2024年2月29日');
  });

  test('无效日期和不明确时区不显示原始值或规范化后的假日期', () {
    for (final value in [
      null,
      '',
      'FormatException: internal_timestamp',
      '2026-02-29T08:00:00Z',
      '2026-04-31T08:00:00Z',
      '2026-00-08T08:00:00Z',
      '2026-09-00T08:00:00Z',
      '2026-09-08T24:00:00Z',
      '2026-09-08T08:60:00Z',
      '2026-09-08T08:00:60Z',
      '2026-09-08T08:00:00+24:00',
      '2026-09-08T08:00:00+08:60',
      '2026-09-08T08:00:00',
    ]) {
      expect(lyraDateTime(value), '时间未知', reason: '$value');
    }
  });
}
