import 'package:flutter/material.dart';

import 'lyra_colors.dart';
import 'lyra_icons.dart';

/// 本地证券品牌标识；输入标的代码，输出固定尺寸的头像。
/// 别名显式归并，期权由调用方传入 underlying，空代码表示现金类记录。
class StockAvatar extends StatelessWidget {
  const StockAvatar({super.key, required this.symbol, this.size = 42});
  final String? symbol;
  final double size;

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
    // 原图中的深色和彩色图形保持原色，浅色底托只用于保证其对比度。
    // 自带白底的 PNG 同样使用白色底托，避免出现内外两层不同的方框。
    final lightPlate = const {
      'AAOI',
      'BOXX',
      'CRCL',
      'CRWV',
      'INTC',
      'NVO',
      'OKLO',
      'PG',
      'QQQI',
      'RDDT',
      'RKLB',
      'SMR',
      'SPYI',
      'TSM',
      'TXN',
      'VOO',
      'WDC',
    }.contains(asset);
    Widget? brandImage;
    if (asset != null) {
      brandImage = Image.asset(
        'assets/stocks/$asset.png',
        fit: BoxFit.contain,
        excludeFromSemantics: true,
      );
      if (asset == 'AAPL') {
        // AAPL 源图已确认是全不透明的灰阶标识。仅在渲染时去除白底，
        // 保留灰阶抗锯齿作为 alpha，不修改 PNG 或重绘商标轮廓。
        brandImage = ColorFiltered(
          colorFilter: ColorFilter.matrix([
            0,
            0,
            0,
            0,
            LyraColors.ink.r * 255,
            0,
            0,
            0,
            0,
            LyraColors.ink.g * 255,
            0,
            0,
            0,
            0,
            LyraColors.ink.b * 255,
            -.2126,
            -.7152,
            -.0722,
            1,
            0,
          ]),
          child: brandImage,
        );
      }
    }
    return Semantics(
      label: ticker.isEmpty ? '现金记录' : '$ticker 标识',
      image: true,
      child: Container(
        clipBehavior: Clip.antiAlias,
        width: size,
        height: size,
        // Apple 源图自带较宽留白，单独校正光学尺寸。
        padding: EdgeInsets.all(size * (asset == 'AAPL' ? 2 / 42 : 8 / 42)),
        decoration: BoxDecoration(
          color: lightPlate ? const Color(0xFFFFFFFF) : LyraColors.surface,
          borderRadius: BorderRadius.circular(size * 12 / 42),
          border: Border.all(color: LyraColors.hairlineStrong),
        ),
        child:
            brandImage ??
            (ticker.isEmpty
                ? Icon(LyraIcons.cash, size: size / 2, color: LyraColors.muted)
                : Center(
                    child: FittedBox(
                      fit: BoxFit.scaleDown,
                      child: Text(
                        ticker.substring(0, ticker.length.clamp(0, 2)),
                        style: TextStyle(
                          fontSize: size * 12 / 42,
                          fontWeight: FontWeight.w500,
                          color: LyraColors.inkSecondary,
                        ),
                      ),
                    ),
                  )),
      ),
    );
  }
}
