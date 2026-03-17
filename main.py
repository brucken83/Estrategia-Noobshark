import os
import math
import json
import time
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, List, Tuple

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG
# =========================
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.005"))         # 0.5%
TP1_PCT = float(os.getenv("TP1_PCT", "0.40"))            # 40%
TP2_PCT = float(os.getenv("TP2_PCT", "0.25"))            # 25%
TP3_PCT = float(os.getenv("TP3_PCT", "0.25"))            # 25%
RUNNER_PCT = float(os.getenv("RUNNER_PCT", "0.10"))      # 10%
INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "100000"))
MAX_BARS = int(os.getenv("MAX_BARS", "500"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
COINGLASS_BASE_URL = os.getenv("COINGLASS_BASE_URL", "https://open-api-v4.coinglass.com/api")
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")
ATR_MULT = float(os.getenv("ATR_MULT", "1.0"))
VWAP_WHALE_MULT = float(os.getenv("VWAP_WHALE_MULT", "2.0"))    # volume x avg volume => whale candle
POC_BINS = int(os.getenv("POC_BINS", "40"))
ENTRY_OFFSET_BPS = float(os.getenv("ENTRY_OFFSET_BPS", "0"))    # for paper trading
ENABLE_SENTIMENT = os.getenv("ENABLE_SENTIMENT", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# =========================
# MODELS
# =========================
@dataclass
class Position:
    symbol: str
    side: str                   # LONG / SHORT
    entry: float
    stop: float
    qty: float
    risk_amount: float
    R: float
    tp1: float
    tp2: float
    tp3: float
    tp1_qty: float
    tp2_qty: float
    tp3_qty: float
    runner_qty: float
    remaining_qty: float
    tp1_done: bool = False
    tp2_done: bool = False
    tp3_done: bool = False
    trailing_active: bool = False
    trailing_stop: Optional[float] = None
    status: str = "OPEN"

@dataclass
class SentimentSnapshot:
    lsr: Optional[float] = None
    oi_change_pct: Optional[float] = None
    fng_value: Optional[int] = None
    cvd_slope: Optional[float] = None

# =========================
# PERSISTENCE
# =========================
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"equity": INITIAL_EQUITY, "positions": {}, "last_signal_ts": {}}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

# =========================
# TELEGRAM
# =========================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram not configured. Message: %s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logging.exception("Telegram error: %s", e)

# =========================
# MARKET DATA
# =========================
def fetch_ohlcv(symbol: str, interval: str = "15m", limit: int = 500) -> pd.DataFrame:
    url = f"{BINANCE_BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json()
    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_asset_volume", "n_trades", "taker_buy_base", "taker_buy_quote", "ignore"
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_asset_volume", "taker_buy_base", "taker_buy_quote"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df

def coinglass_get(path: str, params: Optional[dict] = None) -> dict:
    headers = {"CG-API-KEY": COINGLASS_API_KEY} if COINGLASS_API_KEY else {}
    url = f"{COINGLASS_BASE_URL}{path}"
    r = requests.get(url, headers=headers, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_fng() -> Optional[int]:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=15)
        r.raise_for_status()
        data = r.json()
        return int(data["data"][0]["value"])
    except Exception:
        return None

def fetch_lsr(symbol: str) -> Optional[float]:
    # CoinGlass global long/short account ratio history endpoint
    # Expected symbol example: BTCUSDT / ETHUSDT depending on API plan and exchange filters.
    if not COINGLASS_API_KEY:
        return None
    try:
        data = coinglass_get("/futures/global-long-short-account-ratio/history", params={
            "symbol": symbol,
            "interval": "15m",
            "limit": 3
        })
        rows = data.get("data") or data.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("list", [])
        if not rows:
            return None
        last = rows[-1]
        # Common fields on vendor APIs vary; try multiple keys
        for key in ("longShortRatio", "long_short_ratio", "ratio", "value"):
            if key in last:
                return float(last[key])
        return None
    except Exception:
        return None

def fetch_oi_change_pct(symbol: str) -> Optional[float]:
    # CoinGlass aggregated OI history, last 2 bars
    if not COINGLASS_API_KEY:
        return None
    try:
        data = coinglass_get("/futures/open-interest/aggregated-history", params={
            "symbol": symbol,
            "interval": "15m",
            "limit": 3
        })
        rows = data.get("data") or data.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("list", [])
        if len(rows) < 2:
            return None
        prev, last = rows[-2], rows[-1]
        def _extract_close(x):
            for key in ("close", "c", "oiClose", "value"):
                if key in x:
                    return float(x[key])
            return None
        a, b = _extract_close(prev), _extract_close(last)
        if a is None or b is None or a == 0:
            return None
        return (b - a) / a * 100
    except Exception:
        return None

# =========================
# INDICATORS
# =========================
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h_l = df["high"] - df["low"]
    h_pc = (df["high"] - df["close"].shift(1)).abs()
    l_pc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def keltner(df: pd.DataFrame, ema_n: int = 20, atr_n: int = 20, mult: float = 1.5):
    mid = ema(df["close"], ema_n)
    rng = atr(df, atr_n)
    upper = mid + mult * rng
    lower = mid - mult * rng
    return mid, upper, lower

def approximate_cvd(df: pd.DataFrame) -> pd.Series:
    # Approximation using candle direction * volume.
    signed = np.where(df["close"] >= df["open"], df["volume"], -df["volume"])
    return pd.Series(signed, index=df.index).cumsum()

def cvd_slope(cvd: pd.Series, lookback: int = 5) -> float:
    if len(cvd) < lookback + 1:
        return 0.0
    return float(cvd.iloc[-1] - cvd.iloc[-1 - lookback])

def detect_structure(df: pd.DataFrame, lookback: int = 20) -> str:
    recent = df.tail(lookback).copy()
    highs = recent["high"]
    lows = recent["low"]
    hh = highs.iloc[-1] > highs.iloc[:-1].max()
    ll = lows.iloc[-1] < lows.iloc[:-1].min()
    higher_low = lows.iloc[-5:].min() > lows.iloc[-10:-5].min() if len(lows) >= 10 else False
    lower_high = highs.iloc[-5:].max() < highs.iloc[-10:-5].max() if len(highs) >= 10 else False
    if hh or higher_low:
        return "BULLISH"
    if ll or lower_high:
        return "BEARISH"
    return "NEUTRAL"

def whale_vwap(df: pd.DataFrame, volume_mult: float = 2.0, lookback: int = 120) -> Optional[float]:
    recent = df.tail(lookback).copy()
    avg_vol = recent["volume"].rolling(20).mean()
    whale_mask = recent["volume"] > (avg_vol * volume_mult)
    whale_idx = recent.index[whale_mask.fillna(False)]
    if len(whale_idx) == 0:
        return None
    idx = whale_idx[-1]
    chunk = recent.loc[idx:]
    typical_price = (chunk["high"] + chunk["low"] + chunk["close"]) / 3
    vwap = (typical_price * chunk["volume"]).sum() / chunk["volume"].sum()
    return float(vwap)

def trend_poc(df: pd.DataFrame, bins: int = 40, lookback: int = 120) -> Optional[float]:
    recent = df.tail(lookback).copy()
    if recent.empty:
        return None
    prices = recent["close"].values
    vols = recent["volume"].values
    hist, edges = np.histogram(prices, bins=bins, weights=vols)
    idx = int(np.argmax(hist))
    poc = (edges[idx] + edges[idx + 1]) / 2
    return float(poc)

# =========================
# STRATEGY
# =========================
def sentiment_snapshot(df: pd.DataFrame, symbol: str) -> SentimentSnapshot:
    cvd = approximate_cvd(df)
    snap = SentimentSnapshot(
        lsr=fetch_lsr(symbol) if ENABLE_SENTIMENT else None,
        oi_change_pct=fetch_oi_change_pct(symbol) if ENABLE_SENTIMENT else None,
        fng_value=fetch_fng() if ENABLE_SENTIMENT else None,
        cvd_slope=cvd_slope(cvd, lookback=5),
    )
    return snap

def side_filters(side: str, snap: SentimentSnapshot) -> bool:
    ok = True
    if side == "LONG":
        if snap.lsr is not None and snap.lsr > 1.8:
            ok = False
        if snap.oi_change_pct is not None and snap.oi_change_pct < -1.0:
            ok = False
        if snap.fng_value is not None and snap.fng_value > 80:
            ok = False
        if snap.cvd_slope is not None and snap.cvd_slope < 0:
            ok = False
    else:
        if snap.lsr is not None and snap.lsr < 0.7:
            ok = False
        if snap.oi_change_pct is not None and snap.oi_change_pct < -1.0:
            ok = False
        if snap.fng_value is not None and snap.fng_value < 20:
            ok = False
        if snap.cvd_slope is not None and snap.cvd_slope > 0:
            ok = False
    return ok

def generate_signal(df: pd.DataFrame, symbol: str) -> Tuple[Optional[dict], SentimentSnapshot]:
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema200"] = ema(df["close"], 200)
    df["atr14"] = atr(df, 14)
    df["rsi14"] = rsi(df["close"], 14)
    mid, kup, klo = keltner(df, 20, 20, 1.5)
    df["keltner_mid"] = mid
    df["keltner_up"] = kup
    df["keltner_lo"] = klo

    poc = trend_poc(df, bins=POC_BINS, lookback=120)
    wvwap = whale_vwap(df, volume_mult=VWAP_WHALE_MULT, lookback=120)
    structure = detect_structure(df, lookback=20)
    snap = sentiment_snapshot(df, symbol)

    row = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(row["close"])
    ema20_now = float(row["ema20"])
    ema20_prev = float(prev["ema20"])
    ema200_now = float(row["ema200"])
    atr_now = float(row["atr14"]) if not np.isnan(row["atr14"]) else None
    if atr_now is None or atr_now == 0:
        return None, snap

    long_context = (
        price > ema200_now and
        ema20_now > ema20_prev and
        structure == "BULLISH" and
        (poc is None or price >= poc * 0.997)
    )

    short_context = (
        price < ema200_now and
        ema20_now < ema20_prev and
        structure == "BEARISH" and
        (poc is None or price <= poc * 1.003)
    )

    near_level_long = any([
        abs(price - ema20_now) <= 0.35 * atr_now,
        abs(price - float(row["keltner_mid"])) <= 0.35 * atr_now,
        poc is not None and abs(price - poc) <= 0.35 * atr_now,
        wvwap is not None and abs(price - wvwap) <= 0.35 * atr_now,
    ])

    near_level_short = near_level_long

    bullish_candle = row["close"] > row["open"] and row["close"] > prev["high"]
    bearish_candle = row["close"] < row["open"] and row["close"] < prev["low"]

    if long_context and near_level_long and bullish_candle and side_filters("LONG", snap):
        entry = price * (1 + ENTRY_OFFSET_BPS / 10000)
        stop_candidates = [
            float(df["low"].tail(5).min()),
            ema20_now - 0.5 * atr_now,
            (poc - 0.4 * atr_now) if poc is not None else None,
            (wvwap - 0.4 * atr_now) if wvwap is not None else None,
        ]
        stop_candidates = [x for x in stop_candidates if x is not None and x < entry]
        if not stop_candidates:
            return None, snap
        stop = max(stop_candidates)
        return {
            "symbol": symbol,
            "side": "LONG",
            "entry": float(entry),
            "stop": float(stop),
            "atr": atr_now,
            "poc": poc,
            "wvwap": wvwap,
            "context": {"structure": structure, "price": price}
        }, snap

    if short_context and near_level_short and bearish_candle and side_filters("SHORT", snap):
        entry = price * (1 - ENTRY_OFFSET_BPS / 10000)
        stop_candidates = [
            float(df["high"].tail(5).max()),
            ema20_now + 0.5 * atr_now,
            (poc + 0.4 * atr_now) if poc is not None else None,
            (wvwap + 0.4 * atr_now) if wvwap is not None else None,
        ]
        stop_candidates = [x for x in stop_candidates if x is not None and x > entry]
        if not stop_candidates:
            return None, snap
        stop = min(stop_candidates)
        return {
            "symbol": symbol,
            "side": "SHORT",
            "entry": float(entry),
            "stop": float(stop),
            "atr": atr_now,
            "poc": poc,
            "wvwap": wvwap,
            "context": {"structure": structure, "price": price}
        }, snap

    return None, snap

# =========================
# EXECUTION ENGINE (PAPER)
# =========================
def build_position(signal: dict, equity: float) -> Position:
    entry = signal["entry"]
    stop = signal["stop"]
    side = signal["side"]
    R = abs(entry - stop)
    risk_amount = equity * RISK_PCT
    qty = risk_amount / R

    if side == "LONG":
        tp1 = entry + R
        tp2 = entry + 2 * R
        tp3 = entry + 3 * R
    else:
        tp1 = entry - R
        tp2 = entry - 2 * R
        tp3 = entry - 3 * R

    tp1_qty = qty * TP1_PCT
    tp2_qty = qty * TP2_PCT
    tp3_qty = qty * TP3_PCT
    runner_qty = qty * RUNNER_PCT

    return Position(
        symbol=signal["symbol"],
        side=side,
        entry=entry,
        stop=stop,
        qty=qty,
        risk_amount=risk_amount,
        R=R,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        tp1_qty=tp1_qty,
        tp2_qty=tp2_qty,
        tp3_qty=tp3_qty,
        runner_qty=runner_qty,
        remaining_qty=qty,
    )

def fmt_sentiment(s: SentimentSnapshot) -> str:
    return (
        f"LSR={s.lsr if s.lsr is not None else 'NA'} | "
        f"OIΔ={round(s.oi_change_pct,2) if s.oi_change_pct is not None else 'NA'}% | "
        f"F&G={s.fng_value if s.fng_value is not None else 'NA'} | "
        f"CVDslope={round(s.cvd_slope,2) if s.cvd_slope is not None else 'NA'}"
    )

def open_position_alert(pos: Position, snap: SentimentSnapshot):
    send_telegram(
        f"🟢 <b>ENTRADA {pos.side}</b>\n"
        f"<b>{pos.symbol}</b> | 15m\n"
        f"Entry: <code>{pos.entry:.2f}</code>\n"
        f"Stop: <code>{pos.stop:.2f}</code>\n"
        f"R: <code>{pos.R:.2f}</code>\n"
        f"Qty: <code>{pos.qty:.6f}</code>\n"
        f"TP1 (40%): <code>{pos.tp1:.2f}</code>\n"
        f"TP2 (25%): <code>{pos.tp2:.2f}</code>\n"
        f"TP3 (25%): <code>{pos.tp3:.2f}</code>\n"
        f"Runner (10%): trailing após TP3\n"
        f"Sentimento: {fmt_sentiment(snap)}"
    )

def partial_alert(symbol: str, label: str, px: float, qty: float, remaining: float, stop: Optional[float] = None):
    extra = f"\nNovo stop: <code>{stop:.2f}</code>" if stop is not None else ""
    send_telegram(
        f"📌 <b>{label}</b>\n"
        f"<b>{symbol}</b>\n"
        f"Preço: <code>{px:.2f}</code>\n"
        f"Qty executada: <code>{qty:.6f}</code>\n"
        f"Restante: <code>{remaining:.6f}</code>{extra}"
    )

def close_alert(symbol: str, reason: str, px: float, pnl: float, equity: float):
    emoji = "✅" if pnl >= 0 else "🔴"
    send_telegram(
        f"{emoji} <b>SAÍDA {reason}</b>\n"
        f"<b>{symbol}</b>\n"
        f"Preço: <code>{px:.2f}</code>\n"
        f"PnL estimado: <code>{pnl:.2f}</code>\n"
        f"Equity: <code>{equity:.2f}</code>"
    )

def price_crossed(side: str, current_price: float, target: float, for_stop: bool = False) -> bool:
    if side == "LONG":
        return current_price <= target if for_stop else current_price >= target
    return current_price >= target if for_stop else current_price <= target

def calc_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    if side == "LONG":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty

def manage_position(pos: Position, last_price: float, last_atr: float, equity: float) -> Tuple[Position, float]:
    # Rule 8: never manual close. Only stop, TP, trailing.
    realized_pnl = 0.0

    # Hard stop
    active_stop = pos.trailing_stop if pos.trailing_active and pos.trailing_stop is not None else pos.stop
    if price_crossed(pos.side, last_price, active_stop, for_stop=True):
        pnl = calc_pnl(pos.side, pos.entry, active_stop, pos.remaining_qty)
        realized_pnl += pnl
        equity += pnl
        pos.remaining_qty = 0.0
        pos.status = "CLOSED"
        close_alert(pos.symbol, "STOP" if not pos.trailing_active else "TRAILING", active_stop, pnl, equity)
        return pos, equity

    # TP1
    if (not pos.tp1_done) and price_crossed(pos.side, last_price, pos.tp1):
        pnl = calc_pnl(pos.side, pos.entry, pos.tp1, pos.tp1_qty)
        realized_pnl += pnl
        equity += pnl
        pos.remaining_qty -= pos.tp1_qty
        pos.tp1_done = True
        pos.stop = pos.entry  # move stop to entry
        partial_alert(pos.symbol, "TP1 1:1", pos.tp1, pos.tp1_qty, pos.remaining_qty, stop=pos.stop)

    # TP2
    if pos.tp1_done and (not pos.tp2_done) and price_crossed(pos.side, last_price, pos.tp2):
        pnl = calc_pnl(pos.side, pos.entry, pos.tp2, pos.tp2_qty)
        realized_pnl += pnl
        equity += pnl
        pos.remaining_qty -= pos.tp2_qty
        pos.tp2_done = True
        partial_alert(pos.symbol, "TP2 2R", pos.tp2, pos.tp2_qty, pos.remaining_qty)

    # TP3
    if pos.tp2_done and (not pos.tp3_done) and price_crossed(pos.side, last_price, pos.tp3):
        pnl = calc_pnl(pos.side, pos.entry, pos.tp3, pos.tp3_qty)
        realized_pnl += pnl
        equity += pnl
        pos.remaining_qty -= pos.tp3_qty
        pos.tp3_done = True
        pos.trailing_active = True
        if pos.side == "LONG":
            pos.trailing_stop = max(pos.entry, last_price - ATR_MULT * last_atr)
        else:
            pos.trailing_stop = min(pos.entry, last_price + ATR_MULT * last_atr)
        partial_alert(pos.symbol, "TP3 3R", pos.tp3, pos.tp3_qty, pos.remaining_qty, stop=pos.trailing_stop)

    # Update trailing for runner
    if pos.trailing_active and pos.remaining_qty > 0:
        if pos.side == "LONG":
            new_trail = max(pos.trailing_stop or pos.entry, last_price - ATR_MULT * last_atr, pos.entry)
            pos.trailing_stop = new_trail
        else:
            new_trail = min(pos.trailing_stop or pos.entry, last_price + ATR_MULT * last_atr, pos.entry)
            pos.trailing_stop = new_trail

    # If runner stop gets hit next loop, trade closes.
    return pos, equity

# =========================
# MAIN LOOP
# =========================
def main():
    state = load_state()
    send_telegram("🤖 Bot iniciado. Monitorando BTC/ETH 15m com entradas, parciais, breakeven e trailing.")

    while True:
        try:
            equity = float(state.get("equity", INITIAL_EQUITY))
            positions: Dict[str, dict] = state.get("positions", {})

            for symbol in SYMBOLS:
                df = fetch_ohlcv(symbol, TIMEFRAME, MAX_BARS)
                if len(df) < 220:
                    continue

                last_price = float(df.iloc[-1]["close"])
                last_atr = float(atr(df, 14).iloc[-1])

                # Manage open position first
                if symbol in positions and positions[symbol].get("status") == "OPEN":
                    pos = Position(**positions[symbol])
                    pos, equity = manage_position(pos, last_price, last_atr, equity)
                    positions[symbol] = asdict(pos)
                    if pos.status == "CLOSED":
                        del positions[symbol]
                    state["equity"] = equity
                    state["positions"] = positions
                    save_state(state)
                    continue

                # No open position => look for new signal
                signal, snap = generate_signal(df, symbol)
                if signal is None:
                    continue

                pos = build_position(signal, equity)
                positions[symbol] = asdict(pos)
                state["positions"] = positions
                save_state(state)
                open_position_alert(pos, snap)

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            logging.info("Bot finalizado manualmente.")
            break
        except Exception as e:
            logging.exception("Loop error: %s", e)
            send_telegram(f"⚠️ Erro no loop: <code>{str(e)[:350]}</code>")
            time.sleep(15)

if __name__ == "__main__":
    main()
