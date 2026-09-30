import 'package:flutter_test/flutter_test.dart';
import 'package:lyra_mobile/core/design/lyra_number.dart';

void main() {
  test('金额四舍五入、进位和超出浮点安全范围的整数均精确显示', () {
    expect(LyraNumber.amount('25307.3227'), '25,307.32');
    expect(LyraNumber.amount('70615.0000'), '70,615.00');
    expect(LyraNumber.amount('1.005'), '1.01');
    expect(LyraNumber.amount('-1.005'), '-1.01');
    expect(LyraNumber.amount('999.995'), '1,000.00');
    expect(
      LyraNumber.amount('9007199254740993.995'),
      '9,007,199,254,740,994.00',
    );
    expect(LyraNumber.amount('-0.0000'), '0.00');
    expect(LyraNumber.amount('0.0001'), '< 0.01');
    expect(LyraNumber.amount('-0.0001'), '> −0.01');
    expect(LyraNumber.amount('519', decimals: 0), '519');
  });

  test('碎股保持完整精度，仅移除尾零；缺失状态与非法数据明确区分', () {
    expect(LyraNumber.quantity('1000.0000'), '1,000');
    expect(LyraNumber.quantity('0.000100'), '0.0001');
    expect(LyraNumber.quantity('-01.2500'), '-1.25');
    expect(LyraNumber.quantity('-0.00'), '0');
    expect(LyraNumber.amount('—'), '—');
    expect(() => LyraNumber.amount('invalid'), throwsFormatException);
    expect(() => LyraNumber.quantity('NaN'), throwsFormatException);
    expect(() => LyraNumber.amount('1', decimals: -1), throwsArgumentError);
  });
}
