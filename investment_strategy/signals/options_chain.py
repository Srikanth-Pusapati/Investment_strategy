"""Options-chain positioning signal (C.4) — the options DATA Claude was missing.

Per candidate name: ATM implied volatility, put-call IV skew, and the put/call
open-interest lean, read from Alpaca's option chain snapshots (the free
'indicative' feed the execution path already pulls quotes from — no extra key
or vendor). Heavy put OI + bid put skew reads bearish; the prompt teaches
Claude to express a corroborated bearish thesis as a long_put/bear_put_spread
(the bot cannot short stock) and to prefer spreads when ATM IV is rich.

Two batched REST calls per name (chain snapshots + contract metadata for OI),
capped at OPTIONS_CHAIN_MAX_SYMBOLS per cycle. Names without a liquid listed
chain (most small-cap screener hits) are skipped quietly.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

from ..config import Config
from ..execution.options import parse_occ
from ..models import Signal, SignalKind
from .base import SignalProvider

log = logging.getLogger("signals")

# Chain window: matches the entry gate (MIN/MAX_OPTION_DTE defaults) so the IV
# we read is the IV the bot would actually trade.
_DTE_LO, _DTE_HI = 7, 45
_STRIKE_BAND = 0.15          # +/-15% of spot
_ATM_BAND = 0.02             # ATM = within 2% of spot
_OTM_LO, _OTM_HI = 0.05, 0.15  # skew band: 5-15% out of the money
_MIN_QUOTED = 10             # fewer quoted contracts -> no meaningful chain


class OptionsChainProvider(SignalProvider):
    name = "options_chain"

    def __init__(self, cfg: Config, data=None, trading=None, stock=None):
        self.cfg = cfg
        # Injectable for tests; lazily built from Alpaca creds otherwise.
        self._data = data
        self._trading = trading
        self._stock = stock

    @property
    def enabled(self) -> bool:
        return bool(
            getattr(self.cfg, "options_chain_signal", False)
            and self.cfg.alpaca_api_key
        )

    # -- lazy clients -------------------------------------------------------- #
    @property
    def data(self):
        if self._data is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            self._data = OptionHistoricalDataClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key,
            )
        return self._data

    @property
    def trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient
            self._trading = TradingClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key,
                paper=not self.cfg.is_live,
            )
        return self._trading

    @property
    def stock(self):
        if self._stock is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._stock = StockHistoricalDataClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key,
            )
        return self._stock

    # -- fetch ---------------------------------------------------------------- #
    def fetch(self, symbols: list[str]) -> list[Signal]:
        cap = int(getattr(self.cfg, "options_chain_max_symbols", 25))
        if cap > 0 and len(symbols) > cap:
            log.info(
                "options_chain: reading %d of %d symbols this cycle (cap).",
                cap, len(symbols),
            )
            symbols = symbols[:cap]
        out: list[Signal] = []
        for symbol in symbols:
            try:
                sig = self._one(symbol)
            except Exception as e:  # never let one name break the sweep
                log.debug("options_chain(%s) failed: %s", symbol, e)
                continue
            if sig is not None:
                out.append(sig)
        return out

    def _one(self, symbol: str) -> Signal | None:
        spot = self._spot(symbol)
        if spot <= 0:
            return None
        chain = self._chain(symbol, spot)
        if len(chain) < _MIN_QUOTED:
            return None  # thin/no listed chain — normal for small caps

        atm_ivs: list[float] = []
        otm_put_ivs: list[float] = []
        otm_call_ivs: list[float] = []
        for occ_sym, iv in chain:
            occ = parse_occ(occ_sym)
            if occ is None or iv is None or iv <= 0:
                continue
            _, _, right, strike = occ
            money = strike / spot
            if abs(money - 1.0) <= _ATM_BAND:
                atm_ivs.append(iv)
            elif right == "P" and (1.0 - _OTM_HI) <= money <= (1.0 - _OTM_LO):
                otm_put_ivs.append(iv)
            elif right == "C" and (1.0 + _OTM_LO) <= money <= (1.0 + _OTM_HI):
                otm_call_ivs.append(iv)

        atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
        skew_pts = None
        if otm_put_ivs and otm_call_ivs:
            skew_pts = (
                sum(otm_put_ivs) / len(otm_put_ivs)
                - sum(otm_call_ivs) / len(otm_call_ivs)
            ) * 100.0

        call_oi, put_oi = self._open_interest(symbol)

        # Deterministic positioning score in [-1, 1]: call-heavy OI is bullish,
        # bid put skew is bearish. tanh(ln(ratio)) maps 1:1 OI to 0 and squashes
        # extremes; 10 IV points of skew saturates its half.
        parts: list[float] = []
        if call_oi > 0 and put_oi > 0:
            parts.append(0.5 * math.tanh(math.log(call_oi / put_oi)))
        if skew_pts is not None:
            parts.append(-0.5 * max(-1.0, min(1.0, skew_pts / 10.0)))
        if not parts and atm_iv is None:
            return None  # nothing informative came back
        score = round(max(-1.0, min(1.0, sum(parts))), 3)

        lean = "bearish" if score <= -0.15 else "bullish" if score >= 0.15 else "neutral"
        bits = []
        if atm_iv is not None:
            bits.append(f"ATM IV {atm_iv * 100.0:.0f}%")
        if skew_pts is not None:
            bits.append(
                f"put-call IV skew {skew_pts:+.1f}pts"
                + (" (puts bid)" if skew_pts > 0 else "")
            )
        if call_oi > 0 or put_oi > 0:
            ratio = (put_oi / call_oi) if call_oi > 0 else float("inf")
            bits.append(f"put/call OI {ratio:.2f}")
        return Signal(
            kind=SignalKind.OPTIONS_CHAIN,
            symbol=symbol,
            summary=(
                f"Options chain: {', '.join(bits)} -> {lean} positioning "
                f"({score:+.2f})."
            ),
            score=score,
            source="alpaca-options",
            data={
                "atm_iv": atm_iv, "skew_pts": skew_pts,
                "call_oi": call_oi, "put_oi": put_oi,
                "contracts_quoted": len(chain), "spot": spot,
            },
        )

    # -- data reads ------------------------------------------------------------ #
    def _spot(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest
        try:
            t = self.stock.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )[symbol]
            return float(t.price or 0)
        except Exception as e:
            log.debug("options_chain spot(%s) failed: %s", symbol, e)
            return 0.0

    def _chain(self, symbol: str, spot: float) -> list[tuple[str, float | None]]:
        """[(occ_symbol, implied_vol)] for near-the-money contracts in the
        tradable expiry window."""
        from alpaca.data.requests import OptionChainRequest
        today = date.today()
        snaps = self.data.get_option_chain(OptionChainRequest(
            underlying_symbol=symbol,
            expiration_date_gte=today + timedelta(days=_DTE_LO),
            expiration_date_lte=today + timedelta(days=_DTE_HI),
            strike_price_gte=round(spot * (1.0 - _STRIKE_BAND), 2),
            strike_price_lte=round(spot * (1.0 + _STRIKE_BAND), 2),
        ))
        out: list[tuple[str, float | None]] = []
        for occ_sym, snap in (snaps or {}).items():
            iv = getattr(snap, "implied_volatility", None)
            out.append((occ_sym, float(iv) if iv else None))
        return out

    def _open_interest(self, symbol: str) -> tuple[float, float]:
        """(call_oi, put_oi) summed over the same expiry window. (0, 0) when the
        trading API has nothing — the score then rests on skew alone."""
        from alpaca.trading.requests import GetOptionContractsRequest
        today = date.today()
        call_oi = put_oi = 0.0
        try:
            resp = self.trading.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[symbol],
                expiration_date_gte=today + timedelta(days=_DTE_LO),
                expiration_date_lte=today + timedelta(days=_DTE_HI),
                limit=500,
            ))
            for c in (getattr(resp, "option_contracts", None) or []):
                oi = getattr(c, "open_interest", None)
                if oi is None:
                    continue
                ctype = str(getattr(c.type, "value", c.type) or "")
                if ctype == "call":
                    call_oi += float(oi)
                elif ctype == "put":
                    put_oi += float(oi)
        except Exception as e:
            log.debug("options_chain OI(%s) failed: %s", symbol, e)
        return call_oi, put_oi
