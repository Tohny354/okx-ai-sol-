"""
基于 K 线收盘价序列的技术指标（纯标准库，无 numpy）。
用于配合 bot.py 将摘要传给 AI。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def parse_okx_candle_rows(rows: List[List[Any]]) -> List[Dict[str, Any]]:
    """
    OKX K 线每行: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    接口返回通常为「最新在前」，此处解析为字典列表后由调用方决定顺序。
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not r or len(r) < 6:
            continue
        out.append(
            {
                "ts": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
        )
    return out


def newest_first_to_oldest_first(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """转为时间升序（最旧 → 最新），便于计算指标。"""
    return list(reversed(candles))



def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """Wilder/标准 EMA：前 period-1 位为 None，第 period-1 根用 SMA 种子。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period or period <= 0:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, n):
        prev = out[i - 1]
        if prev is None:
            break
        out[i] = values[i] * k + prev * (1.0 - k)
    return out


def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """相对强弱指标 RSI（Wilder 平滑）。"""
    if len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """
    返回每根K线对应的RSI值列表（前 period 个为 None）
    """
    n = len(closes)
    if n < period + 1:
        return [None] * n
    rsi_vals = [None] * n
    gains = []
    losses = []
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    # 第一个有效RSI从索引 period 开始
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_vals[period] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss != 0 else 100.0

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            rsi_vals[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_vals[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi_vals

def macd_last(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, Optional[float]]:
    """返回最后一根的 MACD / Signal / Histogram（不足数据则部分为 None）。"""
    ema_f = ema_series(closes, fast)
    ema_s = ema_series(closes, slow)
    macd_line: List[Optional[float]] = []
    for i in range(len(closes)):
        a, b = ema_f[i], ema_s[i]
        macd_line.append((a - b) if a is not None and b is not None else None)
    first = next((i for i, m in enumerate(macd_line) if m is not None), None)
    if first is None:
        return {"macd": None, "signal": None, "histogram": None}
    sub: List[float] = []
    for m in macd_line[first:]:
        if m is None:
            break
        sub.append(float(m))
    if len(sub) < signal:
        return {"macd": macd_line[-1], "signal": None, "histogram": None}
    sig_series = ema_series(sub, signal)
    last_m = macd_line[-1]
    last_s = sig_series[-1] if sig_series and sig_series[-1] is not None else None
    hist = (last_m - last_s) if (last_m is not None and last_s is not None) else None
    return {"macd": last_m, "signal": last_s, "histogram": hist}


def compute_indicator_bundle(
    candles_oldest_first: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    从已排序 K 线（旧→新）生成传给 AI 的摘要。
    """
    if not candles_oldest_first:
        return {"error": "no_candles", "bar_count": 0}

    closes = [c["close"] for c in candles_oldest_first]
    highs = [c["high"] for c in candles_oldest_first]
    lows = [c["low"] for c in candles_oldest_first]

    last = closes[-1]
    n = len(closes)

    look20 = min(20, n)
    look50 = min(50, n)
    high_20 = max(highs[-look20:]) if look20 else None
    low_20 = min(lows[-look20:]) if look20 else None
    high_50 = max(highs[-look50:]) if look50 else None
    low_50 = min(lows[-look50:]) if look50 else None

    macd = macd_last(closes)

    # 使用 rsi_series 获取 RSI 序列
    rsi_vals = rsi_series(closes, 14)
    rsi_last = rsi_vals[-1] if rsi_vals else None
    rsi_prev = rsi_vals[-2] if len(rsi_vals) >= 2 else None

    bundle: Dict[str, Any] = {
        "bar_count": n,
        "first_ts": candles_oldest_first[0]["ts"],
        "last_ts": candles_oldest_first[-1]["ts"],
        "last_close": last,
        "sma_5": sma(closes, 5),
        "sma_10": sma(closes, 10),
        "sma_20": sma(closes, 20),
        "sma_50": sma(closes, 50),
        "sma_100": sma(closes, 100) if n >= 100 else None,
        "sma_200": sma(closes, 200) if n >= 200 else None,
        "rsi_14": rsi_last,  # 当前RSI
        "rsi_14_prev": rsi_prev,  # 前一棒RSI
        "macd": macd,
        "recent_high_low": {
            "high_last_20_bars": high_20,
            "low_last_20_bars": low_20,
            "high_last_50_bars": high_50,
            "low_last_50_bars": low_50,
        },
        "price_vs_sma20_pct": ((last - sma(closes, 20)) / sma(closes, 20) * 100.0)
        if sma(closes, 20)
        else None,
    }

    # 简短趋势描述（给 AI 自然语言辅助）
    sma20 = bundle["sma_20"]
    sma50 = bundle["sma_50"]
    trend_bits = []
    if sma20 and last > sma20:
        trend_bits.append("收盘在 SMA20 上方")
    elif sma20:
        trend_bits.append("收盘在 SMA20 下方")
    if sma50 and sma20:
        if sma20 > sma50:
            trend_bits.append("SMA20 在 SMA50 上方(短期偏强)")
        elif sma20 < sma50:
            trend_bits.append("SMA20 在 SMA50 下方(短期偏弱)")
    bundle["trend_hints"] = trend_bits

    return bundle


def _sma_at_index(closes: List[float], period: int, end_idx: int) -> Optional[float]:
    """closes[end_idx] 为最后一根时，返回该位置的 SMA(period)。"""
    if end_idx < period - 1 or end_idx >= len(closes):
        return None
    start = end_idx - period + 1
    return sum(closes[start : end_idx + 1]) / period


def daily_ma30_strategy_context(closes_1d: List[float]) -> Dict[str, Any]:
    """
    对应「30根日线 + 30日均线 + 斜率」类规则：
    - 上升：收盘 > SMA30 且 SMA30 日斜率 > 0
    - 下降：收盘 < SMA30 且 SMA30 日斜率 < 0
    - 横盘：收盘在 SMA30 ±3% 内 且 |SMA30 日斜率| < 0.5%
    """
    n = len(closes_1d)
    if n < 36:
        return {
            "error": "daily_bars_insufficient",
            "bar_count": n,
            "hint": "至少需要约 36 根日线以计算 SMA30、日斜率与 5 日斜率",
        }
    last = closes_1d[-1]
    s_now = _sma_at_index(closes_1d, 30, n - 1)
    s_prev = _sma_at_index(closes_1d, 30, n - 2)
    s_6 = _sma_at_index(closes_1d, 30, n - 7) if n >= 37 else None

    slope_1d_pct: Optional[float] = None
    if s_now is not None and s_prev is not None and s_prev != 0:
        slope_1d_pct = (s_now - s_prev) / s_prev * 100.0

    slope_5d_pct: Optional[float] = None
    if s_now is not None and s_6 is not None and s_6 != 0:
        slope_5d_pct = (s_now - s_6) / s_6 * 100.0

    price_vs_sma30_pct: Optional[float] = None
    if s_now is not None and s_now != 0:
        price_vs_sma30_pct = (last - s_now) / s_now * 100.0

    in_band = (
        price_vs_sma30_pct is not None and abs(price_vs_sma30_pct) <= 3.0
    )
    slope_flat = slope_1d_pct is not None and abs(slope_1d_pct) < 0.5

    trend_class = "未分类/过渡"
    if (
        s_now is not None
        and price_vs_sma30_pct is not None
        and slope_1d_pct is not None
    ):
        if last > s_now and slope_1d_pct > 0:
            trend_class = "上升趋势"
        elif last < s_now and slope_1d_pct < 0:
            trend_class = "下降趋势"
        elif in_band and slope_flat:
            trend_class = "横盘趋势"

    return {
        "bar_count": n,
        "last_close": last,
        "sma_30": s_now,
        "sma30_slope_1d_pct": slope_1d_pct,
        "sma30_slope_5d_pct": slope_5d_pct,
        "price_vs_sma30_pct": price_vs_sma30_pct,
        "in_ma30_plus_minus_3pct_band": in_band,
        "sma30_slope_abs_lt_0_5pct": slope_flat,
        "trend_class": trend_class,
    }


def range_48h_from_1h(candles_1h_oldest_first: List[Dict[str, Any]]) -> Dict[str, Any]:
    """最近 48 根 1H K 线（约 48 小时）的最高/最低与区间百分比。"""
    if not candles_1h_oldest_first:
        return {"error": "no_1h_candles"}
    chunk = candles_1h_oldest_first[-48:] if len(candles_1h_oldest_first) >= 48 else candles_1h_oldest_first
    highs = [c["high"] for c in chunk]
    lows = [c["low"] for c in chunk]
    hi = max(highs)
    lo = min(lows)
    mid = (hi + lo) / 2.0 if hi and lo else None
    range_pct = ((hi - lo) / mid * 100.0) if mid and mid != 0 else None
    return {
        "bars_used": len(chunk),
        "high_48h": hi,
        "low_48h": lo,
        "range_pct_of_mid": range_pct,
    }


def pullback_metrics_vs_48h(
    last_price: float,
    h48: Dict[str, Any],
) -> Dict[str, Any]:
    """相对 48h 高点的回撤比例、相对低点的反弹等（给「回调 30%~60%」等规则用）。"""
    hi = h48.get("high_48h")
    lo = h48.get("low_48h")
    out: Dict[str, Any] = {}
    if isinstance(hi, (int, float)) and hi and last_price <= hi:
        out["retracement_from_48h_high_pct"] = (hi - last_price) / hi * 100.0
    if isinstance(hi, (int, float)) and isinstance(lo, (int, float)) and hi and lo and hi > lo:
        rng = hi - lo
        out["position_in_48h_range_0_to_1"] = (last_price - lo) / rng
    return out


def amplitude_pct_recent(
    candles_oldest_first: List[Dict[str, Any]],
    lookback_bars: int,
) -> Optional[float]:
    """(最高-最低)/中间价，衡量近期振幅%，用于「横盘振幅<5%」的参考。"""
    if not candles_oldest_first:
        return None
    n = min(lookback_bars, len(candles_oldest_first))
    seg = candles_oldest_first[-n:]
    hi = max(c["high"] for c in seg)
    lo = min(c["low"] for c in seg)
    mid = (hi + lo) / 2.0
    if not mid:
        return None
    return (hi - lo) / mid * 100.0


def round_floats(obj: Any, nd: int = 6) -> Any:
    """JSON 友好：递归 round 浮点，减少 token。"""
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(x, nd) for x in obj]
    return obj
# indicators.py 追加以下函数

def ema_last(closes: List[float], period: int) -> Optional[float]:
    """返回最后一根 K 线的 EMA(period) 值，不足数据返回 None"""
    ema_vals = ema_series(closes, period)
    if not ema_vals:
        return None
    return ema_vals[-1] if ema_vals[-1] is not None else None

def is_ema21_pullback(candles: List[Dict[str, Any]], ema21_series: List[Optional[float]]) -> bool:
    """
    判断最后一根 K 线是否“回踩 EMA21 企稳”。
    - 回踩定义：最低价触及或略低于 EMA21，收盘价收在 EMA21 上方（多头）或下方（空头）。
    - 阳线：close > open。
    """
    if len(candles) < 2 or len(ema21_series) < 2:
        return False
    last = candles[-1]
    ema_val = ema21_series[-1]
    if ema_val is None:
        return False
    # 价格低点与 EMA21 的距离在 0.5% 以内视为“触及”
    low_touch = abs(last["low"] - ema_val) / ema_val <= 0.005
    # 阳线条件（做多）或阴线（做空）可以根据调用时传入 side
    return low_touch and last["close"] > last["open"]