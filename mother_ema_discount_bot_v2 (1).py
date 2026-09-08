"""
Mother Candle + EMA9/15 Discount Pullback Bot -- XAU/USD 5-min -- v2, full lifecycle alerts.

Five-stage alert pipeline per setup:
  1. Mother candle formed
  2. Breakout happened (direction set)
  3. Price came back into the range
  4. Entry taken (SL, TP)
  5. Trade closed (SL hit or TP hit)

Plus a daily summary sent at 2:30 AM IST (UTC+5:30, fixed offset, no DST) --
gold's daily close for the trading day in your local time.

Backtest reference (5-min, 6.5yr XAU/USD, sequential one-trade-at-a-time):
  28,981 trades at 1:3 -> 53.05% WR, +11,428R total, ~17 trades/trading day.
IMPORTANT: do NOT move to breakeven early -- backtest showed this badly hurts
this strategy (flips 1:3 from +11,428R to strongly negative). This bot holds
the original stop to TP or SL, no adjustment.

Requires environment variables (GitHub Secrets):
  TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
INTERVAL = "5min"
OUTPUT_SIZE = 300
STATE_FILE = "discount_bot_v2_state.json"

MAX_WAIT_BREAKOUT = 20     # candles to wait for breakout after mother candle
MAX_WAIT_REENTRY = 30      # candles to wait for price to come back into range
MAX_WAIT_ENTRY = 15        # candles to wait for EMA touch+rejection after re-entry
SL_BUFFER_ATR_MULT = 0.1
ATR_PERIOD = 14
MIN_RISK = 0.05
RR_TARGET = 3.0

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL, "interval": INTERVAL, "outputsize": OUTPUT_SIZE,
        "apikey": TWELVE_DATA_API_KEY, "format": "JSON",
    }
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    values = list(reversed(data["values"]))
    candles = []
    for v in values:
        candles.append({
            "dt": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
            "low": float(v["low"]), "close": float(v["close"]),
        })
    now = datetime.now(timezone.utc)
    last_dt = datetime.strptime(candles[-1]["dt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if (now - last_dt).total_seconds() < 5 * 60:
        candles = candles[:-1]
    return candles


def is_market_closed(now=None):
    now = now or datetime.now(timezone.utc)
    if now.weekday() == 5:
        return True
    if now.weekday() == 6 and now.hour < 22:
        return True
    return False


def compute_ema(closes, span):
    ema = [None] * len(closes)
    k = 2 / (span + 1)
    ema[0] = closes[0]
    for i in range(1, len(closes)):
        ema[i] = closes[i] * k + ema[i-1] * (1 - k)
    return ema


def compute_atr(candles, period=ATR_PERIOD):
    trs = [None]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = [None] * len(candles)
    for i in range(period, len(candles)):
        window = [t for t in trs[i-period+1:i+1] if t is not None]
        if len(window) == period:
            atr[i] = sum(window) / period
    return atr


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chat_ids = [c.strip() for c in TELEGRAM_CHAT_ID.split(",")]
    for chat_id in chat_ids:
        requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=15)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "stage": None,          # None, "watching_breakout", "watching_reentry", "watching_entry", "in_trade"
        "mother": None,         # {"time":..., "high":..., "low":...}
        "breakout": None,       # {"time":..., "direction":...}
        "reentry_time": None,
        "open_trade": None,
        "trade_log": [],
        "last_summary_date": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def idx_of(dt_list, target_time):
    for i, t in enumerate(dt_list):
        if t == target_time:
            return i
    return None


def process(state, candles):
    dt = [c["dt"] for c in candles]
    h = [c["high"] for c in candles]
    l = [c["low"] for c in candles]
    o = [c["open"] for c in candles]
    cl = [c["close"] for c in candles]
    ema9 = compute_ema(cl, 9)
    ema15 = compute_ema(cl, 15)
    atr = compute_atr(candles)
    n = len(candles)

    # --- in a trade: check for SL/TP ---
    if state["stage"] == "in_trade":
        trade = state["open_trade"]
        for k in range(n):
            if dt[k] <= trade["entry_time"]:
                continue
            direction = trade["direction"]
            hit = None
            if direction == "long":
                if l[k] <= trade["sl"]: hit = "sl"
                elif h[k] >= trade["target"]: hit = "tp"
            else:
                if h[k] >= trade["sl"]: hit = "sl"
                elif l[k] <= trade["target"]: hit = "tp"
            if hit == "sl":
                send_telegram(
                    f"*Stage 5 \u2014 STOPPED OUT* \u274c\nXAU/USD {direction.upper()}\nStop hit at `{trade['sl']}`.\n-1R on this trade."
                )
                state["trade_log"].append({"date": dt[k][:10], "outcome": "loss", "r_multiple": -1.0})
                state["open_trade"] = None
                state["stage"] = None
                state["mother"] = None
                state["breakout"] = None
                return state
            elif hit == "tp":
                send_telegram(
                    f"*Stage 5 \u2014 TARGET HIT* \u2705\nXAU/USD {direction.upper()}\nTarget hit at `{trade['target']}`.\n+{RR_TARGET:.0f}R on this trade."
                )
                state["trade_log"].append({"date": dt[k][:10], "outcome": "win", "r_multiple": RR_TARGET})
                state["open_trade"] = None
                state["stage"] = None
                state["mother"] = None
                state["breakout"] = None
                return state
        return state  # still in trade, nothing hit yet

    # --- idle: adopt the latest closed candle as a new mother candle ---
    if state["stage"] is None:
        latest_time = dt[-2]  # second-to-last, since last may still be forming depending on poll timing
        if state.get("mother") is None or state["mother"]["time"] != latest_time:
            idx = idx_of(dt, latest_time)
            if idx is not None:
                state["mother"] = {"time": dt[idx], "high": h[idx], "low": l[idx], "idx_ref": idx}
                state["stage"] = "watching_breakout"
                send_telegram(
                    f"*Stage 1 \u2014 Mother Candle Formed* \U0001F56F\ufe0f\nXAU/USD\nRange: `{round(l[idx],2)}` \u2013 `{round(h[idx],2)}`\nTime: {dt[idx]} UTC"
                )
        return state

    mother = state["mother"]
    m_idx = idx_of(dt, mother["time"])
    if m_idx is None:
        return state  # mother candle scrolled out of window, will be replaced next idle cycle after abandon below

    # --- watching for breakout ---
    if state["stage"] == "watching_breakout":
        j_end = min(m_idx+1+MAX_WAIT_BREAKOUT, n)
        for j in range(m_idx+1, j_end):
            if cl[j] > mother["high"]:
                state["breakout"] = {"time": dt[j], "direction": "long"}
                state["stage"] = "watching_reentry"
                send_telegram(f"*Stage 2 \u2014 Breakout* \U0001F4A5\nXAU/USD LONG bias\nBroke above `{mother['high']}`\nTime: {dt[j]} UTC")
                break
            elif cl[j] < mother["low"]:
                state["breakout"] = {"time": dt[j], "direction": "short"}
                state["stage"] = "watching_reentry"
                send_telegram(f"*Stage 2 \u2014 Breakout* \U0001F4A5\nXAU/USD SHORT bias\nBroke below `{mother['low']}`\nTime: {dt[j]} UTC")
                break
        else:
            if j_end - (m_idx+1) >= MAX_WAIT_BREAKOUT:
                state["stage"] = None; state["mother"] = None  # abandon, no breakout in time
        return state

    breakout = state.get("breakout")
    if breakout is None:
        state["stage"] = None; return state
    b_idx = idx_of(dt, breakout["time"])
    if b_idx is None:
        return state

    # --- watching for re-entry into range ---
    if state["stage"] == "watching_reentry":
        k_end = min(b_idx+1+MAX_WAIT_REENTRY, n)
        for k in range(b_idx+1, k_end):
            back_in = (l[k] <= mother["high"]) if breakout["direction"] == "long" else (h[k] >= mother["low"])
            if back_in:
                state["reentry_time"] = dt[k]
                state["stage"] = "watching_entry"
                send_telegram(f"*Stage 3 \u2014 Back Into Range* \U0001F504\nXAU/USD {breakout['direction'].upper()} bias\nPrice returned into the mother candle's range.\nTime: {dt[k]} UTC")
                break
        else:
            if k_end - (b_idx+1) >= MAX_WAIT_REENTRY:
                state["stage"] = None; state["mother"] = None; state["breakout"] = None
        return state

    # --- watching for EMA touch + rejection entry trigger ---
    if state["stage"] == "watching_entry":
        r_idx = idx_of(dt, state["reentry_time"])
        if r_idx is None:
            return state
        k_end = min(r_idx+MAX_WAIT_ENTRY, n)
        for k in range(r_idx, k_end):
            if breakout["direction"] == "long":
                touched = l[k] <= ema9[k] or l[k] <= ema15[k]
                rejecting = cl[k] > o[k]
            else:
                touched = h[k] >= ema9[k] or h[k] >= ema15[k]
                rejecting = cl[k] < o[k]
            if touched and rejecting:
                direction = breakout["direction"]
                entry_price = cl[k]
                ema_lo = min(ema9[k], ema15[k]); ema_hi = max(ema9[k], ema15[k])
                buffer = SL_BUFFER_ATR_MULT * atr[k] if atr[k] is not None else 0.0
                sl = (ema_lo - buffer) if direction == "long" else (ema_hi + buffer)
                risk = abs(entry_price - sl)
                if risk < MIN_RISK:
                    state["stage"] = None; state["mother"] = None; state["breakout"] = None
                    return state
                target = entry_price + RR_TARGET*risk if direction == "long" else entry_price - RR_TARGET*risk
                valid = (sl < entry_price < target) if direction == "long" else (target < entry_price < sl)
                if not valid:
                    state["stage"] = None; state["mother"] = None; state["breakout"] = None
                    return state

                state["open_trade"] = {
                    "entry_time": dt[k], "direction": direction,
                    "entry": round(entry_price, 2), "sl": round(sl, 2), "target": round(target, 2),
                }
                state["stage"] = "in_trade"
                send_telegram(
                    f"*Stage 4 \u2014 ENTRY* \U0001F3AF\nXAU/USD {direction.upper()}\n"
                    f"Entry: `{round(entry_price,2)}`\nStop-loss: `{round(sl,2)}`\nTake-profit (1:3): `{round(target,2)}`\n"
                    f"Time: {dt[k]} UTC"
                )
                break
        else:
            if k_end - r_idx >= MAX_WAIT_ENTRY:
                state["stage"] = None; state["mother"] = None; state["breakout"] = None
        return state

    return state


def maybe_send_daily_summary(state):
    now_ist = datetime.now(IST)
    if not (now_ist.hour == 2 and 25 <= now_ist.minute <= 35):
        return state
    today_ist = now_ist.strftime("%Y-%m-%d")
    if state.get("last_summary_date") == today_ist:
        return state

    # trade_log dates are stored as UTC candle dates; treat "today" loosely as the last 24h of entries.
    cutoff = (now_ist - timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    todays_trades = [t for t in state["trade_log"] if t["date"] >= cutoff]

    wins = [t for t in todays_trades if t["outcome"] == "win"]
    losses = [t for t in todays_trades if t["outcome"] == "loss"]
    total_r = sum(t["r_multiple"] for t in todays_trades)

    if todays_trades:
        msg = (
            f"*Daily Summary* \U0001F4CA\nDate: {today_ist} (IST)\n\n"
            f"Total trades: {len(todays_trades)}\nTake-profit hit: {len(wins)}\nStop-loss hit: {len(losses)}\n"
            f"Net result: {total_r:+.1f}R"
        )
    else:
        msg = f"*Daily Summary* \U0001F4CA\nDate: {today_ist} (IST)\n\nNo trades today."
    send_telegram(msg)
    state["last_summary_date"] = today_ist
    return state


def main():
    now = datetime.now(timezone.utc)
    if not is_market_closed(now):
        candles = fetch_candles()
        state = load_state()
        state = process(state, candles)
        save_state(state)
        state = maybe_send_daily_summary(state)
        save_state(state)
    else:
        state = load_state()
        state = maybe_send_daily_summary(state)
        save_state(state)


if __name__ == "__main__":
    main()
