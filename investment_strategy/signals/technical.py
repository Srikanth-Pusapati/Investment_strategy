"""Technical signals — price-action indicators (RSI, MACD, trend).

Inspired by the *Technical Analyst* role in TauricResearch/TradingAgents, which
reads MACD/RSI to gauge momentum. There each indicator is narrated by an LLM; we
instead fold them into one numeric Signal so the read costs ~zero extra decision
tokens and rides the same bundle -> prompt -> ledger path as every other signal.
The single Claude call still does the cross-signal weighing.

Self-contained like FundamentalsProvider: uses yfinance daily closes (free, no API
key), so the system stays runnable out of the box.
"""
from __future__ import annotations

from ..config import Config
from ..models import Signal, SignalKind
from ..symbols import yahoo_symbol
from .base import SignalProvider


class TechnicalProvider(SignalProvider):
    name = "technical"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def fetch(self, symbols: list[str]) -> list[Signal]:
        import yfinance as yf  # lazy import; optional dep

        signals: list[Signal] = []
        for symbol in symbols:
            # ~1y of daily closes so SMA200 and MACD have enough history.
            hist = yf.Ticker(yahoo_symbol(symbol)).history(period="1y", interval="1d")
            closes = [float(c) for c in hist["Close"].dropna().tolist()] if not hist.empty else []
            if len(closes) < 35:  # need enough for MACD(26)+signal(9)
                continue
            # High/Low ride along (same fetch) for the ATR; a feed without them
            # degrades to the close-to-close true range inside _atr.
            highs = [float(h) for h in hist["High"].dropna().tolist()] if "High" in hist else []
            lows = [float(l) for l in hist["Low"].dropna().tolist()] if "Low" in hist else []

            rsi = self._rsi(closes, 14)
            macd, macd_signal = self._macd(closes)
            hist_val = macd - macd_signal
            price = closes[-1]
            sma20 = self._sma(closes, 20)
            sma50 = self._sma(closes, 50)
            sma200 = self._sma(closes, 200)
            atr = self._atr(highs, lows, closes, 14)
            # Overextension inputs for the risk layer's anti-chasing gate: how
            # far price sits above its 20d mean, in % and in ATR multiples.
            ext_pct = ((price / sma20 - 1.0) * 100.0) if sma20 else None
            ext_atr = ((price - sma20) / atr) if (sma20 and atr) else None

            score = self._score(rsi, hist_val, price, sma50, sma200)
            summary = self._summary(rsi, macd, macd_signal, price, sma50, sma200)

            signals.append(Signal(
                kind=SignalKind.TECHNICAL,
                symbol=symbol,
                summary=summary,
                score=score,
                source="yfinance",
                data={
                    "rsi14": round(rsi, 1),
                    "macd": round(macd, 3),
                    "macd_signal": round(macd_signal, 3),
                    "macd_hist": round(hist_val, 3),
                    "price": round(price, 2),
                    "sma20": round(sma20, 2) if sma20 else None,
                    "sma50": round(sma50, 2) if sma50 else None,
                    "sma200": round(sma200, 2) if sma200 else None,
                    "atr14": round(atr, 3) if atr else None,
                    "ext_pct_sma20": round(ext_pct, 2) if ext_pct is not None else None,
                    "ext_atr": round(ext_atr, 2) if ext_atr is not None else None,
                },
            ))
        return signals

    # -- indicator math (pure, unit-testable) ------------------------------- #
    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> float:
        """Wilder's RSI in [0, 100]. 50 is neutral; <30 oversold, >70 overbought."""
        gains = losses = 0.0
        for i in range(1, period + 1):
            d = closes[i] - closes[i - 1]
            gains += max(d, 0.0)
            losses += max(-d, 0.0)
        avg_gain, avg_loss = gains / period, losses / period
        # Wilder smoothing over the remaining bars.
        for i in range(period + 1, len(closes)):
            d = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        k = 2.0 / (period + 1)
        ema = values[0]
        for v in values[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    @classmethod
    def _macd(cls, closes: list[float]) -> tuple[float, float]:
        """MACD line (EMA12-EMA26) and its 9-period signal EMA."""
        macd_series: list[float] = []
        # Build the MACD line over a trailing window so the signal EMA is meaningful.
        for end in range(26, len(closes) + 1):
            window = closes[:end]
            macd_series.append(cls._ema(window, 12) - cls._ema(window, 26))
        macd = macd_series[-1]
        signal = cls._ema(macd_series[-9:], 9) if len(macd_series) >= 9 else macd
        return macd, signal

    @staticmethod
    def _sma(closes: list[float], period: int) -> float | None:
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    @staticmethod
    def _atr(highs: list[float], lows: list[float], closes: list[float],
             period: int = 14) -> float | None:
        """Wilder ATR. Falls back to close-to-close true range when the feed
        lacks aligned High/Low columns (still a usable volatility yardstick)."""
        n = len(closes)
        if n < period + 1:
            return None
        aligned = len(highs) == n and len(lows) == n
        trs: list[float] = []
        for i in range(1, n):
            if aligned:
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            else:
                tr = abs(closes[i] - closes[i - 1])
            trs.append(tr)
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr if atr > 0 else None

    # -- scoring / summary -------------------------------------------------- #
    @staticmethod
    def _score(rsi: float, macd_hist: float, price: float,
               sma50: float | None, sma200: float | None) -> float:
        """Composite momentum lean in [-1, 1]: trend 0.4, MACD 0.4, RSI 0.2.

        Crude and tunable, like FundamentalsProvider._score — the numeric lean is
        a prior; Claude does the real cross-signal weighing. RSI is read as a
        mean-reversion caution (overbought trims, oversold adds), not momentum,
        so it counterbalances rather than double-counts the trend/MACD terms.
        """
        s = 0.0
        # Trend: above both SMAs is an uptrend; below both a downtrend.
        if sma50 and sma200:
            up = (price > sma50) + (price > sma200)   # 0, 1, or 2
            s += 0.4 * (up - 1)                        # -0.4 .. +0.4
        elif sma50:
            s += 0.4 if price > sma50 else -0.4
        # MACD histogram: sign of momentum, magnitude saturated near +-2% of price.
        if price > 0:
            s += 0.4 * max(-1.0, min(1.0, (macd_hist / price) / 0.02))
        # RSI mean-reversion caution: >70 overbought (trim), <30 oversold (add).
        if rsi >= 70:
            s -= 0.2 * min(1.0, (rsi - 70) / 30)
        elif rsi <= 30:
            s += 0.2 * min(1.0, (30 - rsi) / 30)
        return round(max(-1.0, min(1.0, s)), 3)

    @staticmethod
    def _summary(rsi: float, macd: float, macd_signal: float, price: float,
                 sma50: float | None, sma200: float | None) -> str:
        trend = "n/a"
        if sma50 and sma200:
            if price > sma50 > sma200:
                trend = "uptrend (px>50>200)"
            elif price < sma50 < sma200:
                trend = "downtrend (px<50<200)"
            else:
                trend = "mixed"
        macd_dir = "bull" if macd > macd_signal else "bear"
        rsi_tag = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
        return f"RSI {rsi:.0f} ({rsi_tag}), MACD {macd_dir}, {trend}"
