import 'package:flutter/material.dart';

import 'lyra_colors.dart';
import 'lyra_icons.dart';

/// 本地证券品牌标识；输入标的代码，输出固定尺寸的头像。
/// 别名显式归并，期权由调用方传入 underlying，空代码表示现金类记录。
class StockAvatar extends StatelessWidget {
  const StockAvatar({super.key, required this.symbol});
  final String? symbol;

  static const assets = <String, String>{
    'AAOI': 'AAOI',
    'AAPL': 'AAPL',
    'ADBE': 'ADBE',
    'AMD': 'AMD',
    'AMZN': 'AMZN',
    'AVGO': 'AVGO',
    'AXTI': 'AXTI',
    'BOXX': 'BOXX',
    'BRK-B': 'BRK-B',
    'CRCL': 'CRCL',
    'CRWV': 'CRWV',
    'FIG': 'FIG',
    'GOOG': 'GOOG',
    'INTC': 'INTC',
    'JEPQ': 'JEPQ',
    'LLY': 'LLY',
    'LULU': 'LULU',
    'META': 'META',
    'MSFT': 'MSFT',
    'MU': 'MU',
    'NFLX': 'NFLX',
    'NOK': 'NOK',
    'NVDA': 'NVDA',
    'NVO': 'NVO',
    'ORCL': 'ORCL',
    'OKLO': 'OKLO',
    'PDD': 'PDD',
    'PG': 'PG',
    'QQQI': 'QQQI',
    'QS': 'QS',
    'RDDT': 'RDDT',
    'RKLB': 'RKLB',
    'SMR': 'SMR',
    'SPYI': 'SPYI',
    'TSLA': 'TSLA',
    'TSM': 'TSM',
    'TXN': 'TXN',
    'VOO': 'VOO',
    'WDC': 'WDC',
    'WMT': 'WMT',
    'GOOGL': 'GOOG',
    'BRKB': 'BRK-B',
    'BRK.B': 'BRK-B',
  };

  @override
  Widget build(BuildContext context) {
    final ticker = symbol?.trim().toUpperCase() ?? '';
    final asset = assets[ticker];
    // 以下源图为白色图形，深底保证原品牌图形的对比度。
    final dark = const {'AMZN', 'NOK', 'QS', 'AXTI'}.contains(asset);
    return Semantics(
      label: ticker.isEmpty ? '现金记录' : '$ticker 标识',
      image: true,
      child: Container(
        clipBehavior: Clip.antiAlias,
        width: 42,
        height: 42,
        // Apple 源图自带较宽留白，单独校正光学尺寸。
        padding: EdgeInsets.all(asset == 'AAPL' ? 2 : 8),
        decoration: BoxDecoration(
          color: dark ? LyraColors.ink : LyraColors.surface,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: LyraColors.hairline),
        ),
        child: asset != null
            ? Image.asset(
                'assets/stocks/$asset.png',
                fit: BoxFit.contain,
                excludeFromSemantics: true,
              )
            : ticker.isEmpty
            ? const Icon(LyraIcons.cash, size: 21, color: LyraColors.muted)
            : Center(
                child: Text(
                  ticker.substring(0, ticker.length.clamp(0, 2)),
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                    color: LyraColors.inkSecondary,
                  ),
                ),
              ),
      ),
    );
  }
}
