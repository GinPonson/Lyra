/// 用可读日期展示数据时间；带时区的时间统一换算为 UTC。
/// 不把缺失时区、无效日期或原始错误文本当作可用时间。
String lyraDateTime(String? value) {
  if (value == null) return '时间未知';
  final match = _isoDateTime.firstMatch(value.trim());
  if (match == null) return '时间未知';

  final year = int.parse(match[1]!);
  final month = int.parse(match[2]!);
  final day = int.parse(match[3]!);
  final calendar = DateTime.utc(year, month, day);
  // DateTime 会把 2 月 30 日等溢出日期进位；展示层必须拒绝这类输入。
  if (calendar.year != year || calendar.month != month || calendar.day != day) {
    return '时间未知';
  }
  if (match[4] == null) return '$year年$month月$day日';

  final hour = int.parse(match[4]!);
  final minute = int.parse(match[5]!);
  final second = int.parse(match[6]!);
  if (hour > 23 || minute > 59 || second > 59) return '时间未知';
  final zone = match[7]!;
  if (zone != 'Z' && zone != 'z') {
    final offsetHour = int.parse(zone.substring(1, 3));
    final offsetMinute = int.parse(zone.substring(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return '时间未知';
  }

  final date = DateTime.tryParse(value.trim())?.toUtc();
  if (date == null) return '时间未知';
  String two(int part) => part.toString().padLeft(2, '0');
  return '${date.year}年${date.month}月${date.day}日 '
      '${two(date.hour)}:${two(date.minute)}:${two(date.second)} UTC';
}

final _isoDateTime = RegExp(
  r'^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([Zz]|[+-]\d{2}:\d{2}))?$',
);
