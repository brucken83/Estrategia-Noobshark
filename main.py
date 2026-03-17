import os
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "100000"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.005"))
TP1_PCT = float(os.getenv("TP1_PCT", "0.40"))
TP2_PCT = float(os.getenv("TP2_PCT", "0.25"))
TP3_PCT = float(os.getenv("TP3_PCT", "0.25"))
RUNNER_PCT = float(os.getenv("RUNNER_PCT", "0.10"))
ATR_MULT = float(os.getenv("ATR_MULT", "1.0"))
MAX_BARS = int(os.getenv("MAX_BARS", "500"))
VWAP_WHALE_MULT = float(os.getenv("VWAP_WHALE_MULT", "2.0"))
POC_BINS = int(os.getenv("POC_BINS", "40"))
ENTRY_OFFSET_BPS = float(os.getenv("ENTRY_OFFSET_BPS", "0"))
ENABLE_SENTIMENT = os.getenv("ENABLE_SENTIMENT", "true").lower() == "true"

STATE_FILE = Path(os.getenv("STATE_FILE", "runtime_state.json"))
OUTPUT_JSON = Path(os.getenv("OUTPUT_JSON", "docs/data/latest.json"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
COINGLASS_BASE_URL = os.getenv("COINGLASS_BASE_URL", "https://open-api-v4.coinglass.com/api")

BINANCE_MARKET_TYPE = os.getenv("BINANCE_MARKET_TYPE", "auto").lower()


@dataclass
class Position:
    symbol: str
    side: str
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


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"equity": INITIAL_EQUITY, "positions": {}, "events": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20
    )


def fetch_ohlcv(symbol: str, interval: str = "15m", limit: int = 500) -> pd.DataFrame:
    endpoint_map = {
        "futures": [("futures", "https://fapi.binance.com/fapi/v1/klines")],
        "spot": [("spot", "https://api.binance.com/api/v3/klines")],
        "auto": [
            ("futures", "https://fapi.binance.com/fapi/v1/klines"),
            ("spot", "https://api.binance.com/api/v3/klines"),
        ],
    }

    endpoints = endpoint_map.get(BINANCE_MARKET_TYPE, endpoint_map["auto"])
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    last_error = None

    for market_type, url in endpoints:
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            raw = r.json()

            cols = [
                "open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_asset_volume", "n_trades", "taker_buy_base", "taker_buy_quote", "ignore"
            ]
            df = pd.DataFrame(raw, columns=cols)

            for c in [
                "open", "high", "low", "close", "volume",
                "quote_asset_volume", "taker_buy_base", "taker_buy_quote"
            ]:
                df[c] = pd.to_numeric(df[c], errors="coerce")

            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
            df["market_type"] = market_type
            return df

        except Exception as e:
            last_error = e

    raise RuntimeError(f"Falha ao buscar candles para {symbol}. Último erro: {last_error}")


def coinglass_get(path: str, params: Optional[dict] = None) -> dict:
    headers = {"CG-API-KEY": COINGLASS_API_KEY} if COINGLASS_API_KEY else {}
    r = requests.get(f"{COINGLASS_BASE_URL}{path}", headers=headers, params=params or {}, timeout=20)
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
    if not COINGLASS_API_KEY:
        return None
    try:
        data = coinglass_get("/futures/global-long-short-account-ratio/history", {
            "symbol": symbol, "interval": "15m", "limit": 3
        })
        rows = data.get("data") or data.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("list", [])
        if not rows:
            return None
        last = rows[-1]
        for k in ("longShortRatio", "long_short_ratio", "ratio", "value"):
            if k in last:
                return float(last[k])
    except Exception:
        return None
    return None


def fetch_oi_change_pct(symbol: str) -> Optional[float]:
    if not COINGLASS_API_KEY:
        return None
    try:
        data = coinglass_get("/futures/open-interest/aggregated-history", {
            "symbol": symbol, "interval": "15m", "limit": 3
        })
        rows = data.get("data") or data.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("list", [])
        if len(rows) < 2:
            return None

        def extract(x):
            for k in ("close", "c", "oiClose", "value"):
                if k in x:
                    return float(x[k])
            return None

        a, b = extract(rows[-2]), extract(rows[-1])
        if a is None or b is None or a == 0:
            return None
        return (b - a) / a * 100
    except Exception:
        return None


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
    tp = (chunk["high"] + chunk["low"] + chunk["close"]) / 3
    return float((tp * chunk["volume"]).sum() / chunk["volume"].sum())


def trend_poc(df: pd.DataFrame, bins: int = 40, lookback: int = 120) -> Optional[float]:
    recent = df.tail(lookback).copy()
    if recent.empty:
        return None
    hist, edges = np.histogram(recent["close"].values, bins=bins, weights=recent["volume"].values)
    idx = int(np.argmax(hist))
    return float((edges[idx] + edges[idx + 1]) / 2)


def side_filters(side: str, lsr, oi_change_pct, fng, cvd_slope_val) -> bool:
    if side == "LONG":
        if lsr is not None and lsr > 1.8:
            return False
        if oi_change_pct is not None and oi_change_pct < -1.0:
            return False
        if fng is not None and fng > 80:
            return False
        if cvd_slope_val < 0:
            return False
    else:
        if lsr is not None and lsr < 0.7:
            return False
        if oi_change_pct is not None and oi_change_pct < -1.0:
            return False
        if fng is not None and fng < 20:
            return False
        if cvd_slope_val > 0:
            return False
    return True


def generate_signal(df: pd.DataFrame, symbol: str):
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

    lsr = fetch_lsr(symbol) if ENABLE_SENTIMENT else None
    oi_change_pct = fetch_oi_change_pct(symbol) if ENABLE_SENTIMENT else None
    fng = fetch_fng() if ENABLE_SENTIMENT else None
    cvd = approximate_cvd(df)
    cvd_slope_val = cvd_slope(cvd, 5)

    row = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(row["close"])
    atr_now = float(row["atr14"]) if not np.isnan(row["atr14"]) else None
    if atr_now is None or atr_now == 0:
        return None

    long_context = (
        price > float(row["ema200"]) and
        float(row["ema20"]) > float(prev["ema20"]) and
        structure == "BULLISH" and
        (poc is None or price >= poc * 0.997) and
        side_filters("LONG", lsr, oi_change_pct, fng, cvd_slope_val)
    )

    short_context = (
        price < float(row["ema200"]) and
        float(row["ema20"]) < float(prev["ema20"]) and
        structure == "BEARISH" and
        (poc is None or price <= poc * 1.003) and
        side_filters("SHORT", lsr, oi_change_pct, fng, cvd_slope_val)
    )

    near_level = any([
        abs(price - float(row["ema20"])) <= 0.35 * atr_now,
        abs(price - float(row["keltner_mid"])) <= 0.35 * atr_now,
        poc is not None and abs(price - poc) <= 0.35 * atr_now,
        wvwap is not None and abs(price - wvwap) <= 0.35 * atr_now,
    ])

    bullish_candle = row["close"] > row["open"] and row["close"] > prev["high"]
    bearish_candle = row["close"] < row["open"] and row["close"] < prev["low"]

    if long_context and near_level and bullish_candle:
        entry = price * (1 + ENTRY_OFFSET_BPS / 10000)
        stops = [
            float(df["low"].tail(5).min()),
            float(row["ema20"]) - 0.5 * atr_now,
            (poc - 0.4 * atr_now) if poc is not None else None,
            (wvwap - 0.4 * atr_now) if wvwap is not None else None,
        ]
        stops = [s for s in stops if s is not None and s < entry]
        if stops:
            stop = max(stops)
            return {
                "symbol": symbol,
                "side": "LONG",
                "entry": float(entry),
                "stop": float(stop),
                "atr": atr_now,
                "price": price,
                "poc": poc,
                "wvwap": wvwap,
                "structure": structure,
                "rsi14": float(row["rsi14"]) if not np.isnan(row["rsi14"]) else None,
                "lsr": lsr,
                "oi_change_pct": oi_change_pct,
                "fng": fng,
                "cvd_slope": cvd_slope_val
            }

    if short_context and near_level and bearish_candle:
        entry = price * (1 - ENTRY_OFFSET_BPS / 10000)
        stops = [
            float(df["high"].tail(5).max()),
            float(row["ema20"]) + 0.5 * atr_now,
            (poc + 0.4 * atr_now) if poc is not None else None,
            (wvwap + 0.4 * atr_now) if wvwap is not None else None,
        ]
        stops = [s for s in stops if s is not None and s > entry]
        if stops:
            stop = min(stops)
            return {
                "symbol": symbol,
                "side": "SHORT",
                "entry": float(entry),
                "stop": float(stop),
                "atr": atr_now,
                "price": price,
                "poc": poc,
                "wvwap": wvwap,
                "structure": structure,
                "rsi14": float(row["rsi14"]) if not np.isnan(row["rsi14"]) else None,
                "lsr": lsr,
                "oi_change_pct": oi_change_pct,
                "fng": fng,
                "cvd_slope": cvd_slope_val
            }

    return {
        "symbol": symbol,
        "side": "WAIT",
        "price": price,
        "atr": atr_now,
        "poc": poc,
        "wvwap": wvwap,
        "structure": structure,
        "rsi14": float(row["rsi14"]) if not np.isnan(row["rsi14"]) else None,
        "lsr": lsr,
        "oi_change_pct": oi_change_pct,
        "fng": fng,
        "cvd_slope": cvd_slope_val
    }


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
        tp1_qty=qty * TP1_PCT,
        tp2_qty=qty * TP2_PCT,
        tp3_qty=qty * TP3_PCT,
        runner_qty=qty * RUNNER_PCT,
        remaining_qty=qty
    )


def calc_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    return (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty


def price_crossed(side: str, current_price: float, target: float, for_stop: bool = False) -> bool:
    if side == "LONG":
        return current_price <= target if for_stop else current_price >= target
    return current_price >= target if for_stop else current_price <= target


def push_event(state: dict, event: dict):
    events = state.get("events", [])
    events.append(event)
    state["events"] = events[-100:]


def manage_position(pos: Position, last_price: float, last_atr: float, equity: float, state: dict):
    active_stop = pos.trailing_stop if pos.trailing_active and pos.trailing_stop is not None else pos.stop

    if price_crossed(pos.side, last_price, active_stop, for_stop=True):
        pnl = calc_pnl(pos.side, pos.entry, active_stop, pos.remaining_qty)
        equity += pnl
        push_event(state, {
            "type": "EXIT",
            "symbol": pos.symbol,
            "reason": "TRAILING" if pos.trailing_active else "STOP",
            "price": round(active_stop, 4),
            "pnl": round(pnl, 2)
        })
        send_telegram(
            f"🔴 <b>SAÍDA {pos.symbol}</b>\n"
            f"Motivo: {'TRAILING' if pos.trailing_active else 'STOP'}\n"
            f"Preço: <code>{active_stop:.2f}</code>\n"
            f"PnL: <code>{pnl:.2f}</code>"
        )
        pos.remaining_qty = 0
        pos.status = "CLOSED"
        return pos, equity

    if (not pos.tp1_done) and price_crossed(pos.side, last_price, pos.tp1):
        pnl = calc_pnl(pos.side, pos.entry, pos.tp1, pos.tp1_qty)
        equity += pnl
        pos.remaining_qty -= pos.tp1_qty
        pos.tp1_done = True
        pos.stop = pos.entry
        push_event(state, {"type": "TP1", "symbol": pos.symbol, "price": round(pos.tp1, 4), "pnl": round(pnl, 2)})
        send_telegram(
            f"📌 <b>TP1 {pos.symbol}</b>\n"
            f"Preço: <code>{pos.tp1:.2f}</code>\n"
            f"Parcial: <code>{pos.tp1_qty:.6f}</code>\n"
            f"Stop movido para entrada."
        )

    if pos.tp1_done and (not pos.tp2_done) and price_crossed(pos.side, last_price, pos.tp2):
        pnl = calc_pnl(pos.side, pos.entry, pos.tp2, pos.tp2_qty)
        equity += pnl
        pos.remaining_qty -= pos.tp2_qty
        pos.tp2_done = True
        push_event(state, {"type": "TP2", "symbol": pos.symbol, "price": round(pos.tp2, 4), "pnl": round(pnl, 2)})
        send_telegram(
            f"📌 <b>TP2 {pos.symbol}</b>\n"
            f"Preço: <code>{pos.tp2:.2f}</code>\n"
            f"Parcial: <code>{pos.tp2_qty:.6f}</code>"
        )

    if pos.tp2_done and (not pos.tp3_done) and price_crossed(pos.side, last_price, pos.tp3):
        pnl = calc_pnl(pos.side, pos.entry, pos.tp3, pos.tp3_qty)
        equity += pnl
        pos.remaining_qty -= pos.tp3_qty
        pos.tp3_done = True
        pos.trailing_active = True
        pos.trailing_stop = (
            max(pos.entry, last_price - ATR_MULT * last_atr)
            if pos.side == "LONG"
            else min(pos.entry, last_price + ATR_MULT * last_atr)
        )
        push_event(state, {"type": "TP3", "symbol": pos.symbol, "price": round(pos.tp3, 4), "pnl": round(pnl, 2)})
        send_telegram(
            f"📌 <b>TP3 {pos.symbol}</b>\n"
            f"Preço: <code>{pos.tp3:.2f}</code>\n"
            f"Trailing ativado em <code>{pos.trailing_stop:.2f}</code>"
        )

    if pos.trailing_active and pos.remaining_qty > 0:
        if pos.side == "LONG":
            pos.trailing_stop = max(pos.trailing_stop or pos.entry, last_price - ATR_MULT * last_atr, pos.entry)
        else:
            pos.trailing_stop = min(pos.trailing_stop or pos.entry, last_price + ATR_MULT * last_atr, pos.entry)

    return pos, equity


def write_dashboard_json(state: dict, snapshots: dict):
    payload = {
        "updated_at_utc": pd.Timestamp.utcnow().isoformat(),
        "equity": round(float(state.get("equity", INITIAL_EQUITY)), 2),
        "positions": state.get("positions", {}),
        "events": state.get("events", [])[-25:],
        "snapshots": snapshots,
        "config": {
            "symbols": SYMBOLS,
            "timeframe": TIMEFRAME,
            "risk_pct": RISK_PCT,
            "tp1_pct": TP1_PCT,
            "tp2_pct": TP2_PCT,
            "tp3_pct": TP3_PCT,
            "runner_pct": RUNNER_PCT,
            "binance_market_type": BINANCE_MARKET_TYPE,
        }
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main():
    state = load_state()
    equity = float(state.get("equity", INITIAL_EQUITY))
    positions = state.get("positions", {})
    snapshots = {}

    for symbol in SYMBOLS:
        try:
            df = fetch_ohlcv(symbol, TIMEFRAME, MAX_BARS)
            signal = generate_signal(df, symbol)
            last_price = float(df.iloc[-1]["close"])
            last_atr = float(atr(df, 14).iloc[-1])

            if symbol in positions and positions[symbol].get("status") == "OPEN":
                pos = Position(**positions[symbol])
                pos, equity = manage_position(pos, last_price, last_atr, equity, state)
                if pos.status == "CLOSED":
                    positions.pop(symbol, None)
                else:
                    positions[symbol] = asdict(pos)

            elif signal and signal.get("side") in ("LONG", "SHORT"):
                pos = build_position(signal, equity)
                positions[symbol] = asdict(pos)
                push_event(state, {
                    "type": "ENTRY",
                    "symbol": symbol,
                    "side": pos.side,
                    "entry": round(pos.entry, 4),
                    "stop": round(pos.stop, 4),
                    "tp1": round(pos.tp1, 4),
                    "tp2": round(pos.tp2, 4),
                    "tp3": round(pos.tp3, 4)
                })
                send_telegram(
                    f"🟢 <b>ENTRADA {pos.side}</b>\n"
                    f"{symbol} | 15m\n"
                    f"Entry: <code>{pos.entry:.2f}</code>\n"
                    f"Stop: <code>{pos.stop:.2f}</code>\n"
                    f"TP1: <code>{pos.tp1:.2f}</code>\n"
                    f"TP2: <code>{pos.tp2:.2f}</code>\n"
                    f"TP3: <code>{pos.tp3:.2f}</code>"
                )

            snapshots[symbol] = signal if signal is not None else {
                "symbol": symbol,
                "side": "ERROR",
                "error": "signal_none"
            }

        except Exception as e:
            snapshots[symbol] = {
                "symbol": symbol,
                "side": "ERROR",
                "error": str(e)[:300]
            }

    state["equity"] = equity
    state["positions"] = positions
    save_state(state)
    write_dashboard_json(state, snapshots)


if __name__ == "__main__":
    main()
