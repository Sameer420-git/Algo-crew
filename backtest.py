import os, time, csv, requests
from ema_bot import (ema_full, rsi_series, macd_calc, price_action, s1_filters, s2_at, s2_all,
                     FAST, SLOW, SL_PCT, TP_PCT, TIMEFRAMES, RSI_PERIOD,
                     EXIT_SHRINK_CANDLES, S2_USE_TP, BREAKOUT_N, PA_LOOKBACK)

# ---------------- BACKTEST SETTINGS ----------------
BT_BASE_URL  = "https://api.india.delta.exchange"   # public market data, no keys needed
FEE_PCT      = 0.05    # per side, taker fee (ASSUMPTION - check your Delta fee tier + GST)
SLIPPAGE_PCT = 0.02    # per side, on market entries/exits and stop-loss fills (ASSUMPTION)
WARMUP       = 100     # candles used only to warm up indicators

# ---------------- Data download ----------------
def fetch_history(symbol, tf, days):
    tf_sec = TIMEFRAMES[tf]
    end = int(time.time())
    start = end - days * 86400 - WARMUP * tf_sec
    cache = f"history_{symbol}_{tf}_{days}d.csv"

    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 6 * 3600:
        print(f"Using cached data: {cache}")
        with open(cache) as f:
            rows = list(csv.DictReader(f))
        return [{k: (int(v) if k == "time" else float(v)) for k, v in r.items()} for r in rows]

    chunk = 1500 * tf_sec
    total = (end - start) // chunk + 1
    out, t, k = {}, start, 0
    while t < end:
        t2 = min(t + chunk, end)
        r = None
        for _ in range(3):
            try:
                r = requests.get(f"{BT_BASE_URL}/v2/history/candles",
                                 params={"symbol": symbol, "resolution": tf,
                                         "start": t, "end": t2}, timeout=20).json()
                break
            except Exception:
                time.sleep(2)
        if r and r.get("result"):
            for c in r["result"]:
                out[int(c["time"])] = c
        k += 1
        print(f"  downloading {k}/{total}  ({len(out)} candles)", end="\r")
        t = t2
        time.sleep(0.2)
    print()

    candles = [{"time": int(c["time"]), "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]), "close": float(c["close"]),
                "volume": float(c.get("volume") or 0)}
               for _, c in sorted(out.items())]
    candles = candles[:-1]                       # drop the still-forming candle
    if not candles:
        raise SystemExit("No candles returned. Check the symbol name / BT_BASE_URL / internet.")
    with open(cache, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(candles[0].keys()))
        w.writeheader(); w.writerows(candles)
    return candles

def vwap_series(candles):
    """Session VWAP that resets every UTC day (same idea as the live bot)."""
    out, day, pv, vol = [], None, 0.0, 0.0
    for c in candles:
        d = c["time"] // 86400
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        pv += (c["high"] + c["low"] + c["close"]) / 3 * c["volume"]
        vol += c["volume"]
        out.append(pv / vol if vol > 0 else None)
    return out

# ---------------- Trade mechanics ----------------
def open_pos(side, px, use_tp, idx, t):
    entry = px * (1 + side * SLIPPAGE_PCT / 100)
    return {"side": side, "entry": entry, "idx": idx, "time": t,
            "sl": entry * (1 - side * SL_PCT / 100),
            "tp": entry * (1 + side * TP_PCT / 100) if use_tp else None}

def stop_hit(pos, c):
    """SL checked before TP, so if both are touched in one candle we assume the worse (SL)."""
    if pos["side"] == 1:
        if c["low"] <= pos["sl"]:
            return pos["sl"], "SL", True
        if pos["tp"] and c["high"] >= pos["tp"]:
            return pos["tp"], "TP", False
    else:
        if c["high"] >= pos["sl"]:
            return pos["sl"], "SL", True
        if pos["tp"] and c["low"] <= pos["tp"]:
            return pos["tp"], "TP", False
    return None

def close_pos(pos, t, px, reason, market_exit):
    side = pos["side"]
    ex = px * (1 - side * SLIPPAGE_PCT / 100) if market_exit else px
    pnl = side * (ex - pos["entry"]) / pos["entry"] * 100 - 2 * FEE_PCT
    return {"entry_time": pos["time"], "exit_time": t, "side": "LONG" if side == 1 else "SHORT",
            "entry": round(pos["entry"], 4), "exit": round(ex, 4),
            "reason": reason, "pnl": round(pnl, 4)}

# ---------------- Strategy 1 simulation ----------------
def sim_s1(candles):
    closes = [c["close"] for c in candles]
    f, s = ema_full(closes, FAST), ema_full(closes, SLOW)
    rs, vw = rsi_series(closes, RSI_PERIOD), vwap_series(candles)
    trades, pos, n = [], None, len(candles)

    for i in range(WARMUP, n):
        c = candles[i]
        if pos:
            hit = stop_hit(pos, c)
            if hit:
                trades.append(close_pos(pos, c["time"], *hit)); pos = None
        if i == n - 1:
            break
        if None in (f[i - 1], s[i - 1], f[i], s[i]):
            continue

        up   = f[i - 1] <= s[i - 1] and f[i] > s[i]
        down = f[i - 1] >= s[i - 1] and f[i] < s[i]
        if not (up or down):
            continue
        if (up and pos and pos["side"] == 1) or (down and pos and pos["side"] == -1):
            continue

        window = candles[max(0, i - BREAKOUT_N - PA_LOOKBACK - 5):i + 1]
        info = {"price": c["close"], "vwap": vw[i], "rsi": rs[i], "pa": price_action(window)}
        ok, _, _, _ = s1_filters("long" if up else "short", info)
        if not ok:
            continue

        nxt = candles[i + 1]
        if pos:                                           # flip: close old position first
            trades.append(close_pos(pos, nxt["time"], nxt["open"], "FLIP", True))
        pos = open_pos(1 if up else -1, nxt["open"], True, i + 1, nxt["time"])

    if pos:
        trades.append(close_pos(pos, candles[-1]["time"], candles[-1]["close"], "END", True))
    return trades

# ---------------- Strategy 2 simulation ----------------
def bt_exit(hist, macd, sig, i):
    n = EXIT_SHRINK_CANDLES
    h = hist[i - n:i + 1]
    if len(h) < n + 1 or any(v is None for v in h) or macd[i] is None or sig[i] is None:
        return None
    if h[0] > 0 and all(h[j + 1] < h[j] for j in range(n)):
        return "MACD_SLOW"
    if macd[i] < sig[i]:
        return "MACD_CROSS"
    return None

def sim_s2(candles):
    closes = [c["close"] for c in candles]
    rs = rsi_series(closes, RSI_PERIOD)
    macd, sig, hist = macd_calc(closes)
    trades, pos, n = [], None, len(candles)

    for i in range(WARMUP, n):
        c = candles[i]
        if pos:
            hit = stop_hit(pos, c)
            if hit:
                trades.append(close_pos(pos, c["time"], *hit)); pos = None
        if i == n - 1:
            break
        nxt = candles[i + 1]

        if pos:
            if i > pos["sig_idx"]:                        # only candles after the signal candle
                why = bt_exit(hist, macd, sig, i)
                if why:
                    trades.append(close_pos(pos, nxt["time"], nxt["open"], why, True)); pos = None
        else:
            cur, prev = s2_at(rs, macd, sig, hist, i), s2_at(rs, macd, sig, hist, i - 1)
            if s2_all(cur) and not s2_all(prev):
                pos = open_pos(1, nxt["open"], S2_USE_TP, i + 1, nxt["time"])
                pos["sig_idx"] = i

    if pos:
        trades.append(close_pos(pos, candles[-1]["time"], candles[-1]["close"], "END", True))
    return trades

# ---------------- Reporting ----------------
def fmt_t(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))

def equity_curve(trades):
    eq, curve = 100.0, [100.0]
    for t in trades:
        eq *= 1 + t["pnl"] / 100
        curve.append(eq)
    return curve

def report(name, trades):
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    if not trades:
        print("No trades were generated in this period."); return
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    curve = equity_curve(trades)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100)
    gross_w, gross_l = sum(wins), abs(sum(losses))
    pf = f"{gross_w / gross_l:.2f}" if gross_l > 0 else "inf"

    print(f"Trades            : {len(trades)}")
    print(f"Win rate          : {len(wins) / len(trades) * 100:.1f}%")
    print(f"Avg win / avg loss: {sum(wins) / max(len(wins), 1):+.2f}% / {sum(losses) / max(len(losses), 1):+.2f}%")
    print(f"Profit factor     : {pf}")
    print(f"Total return      : {curve[-1] - 100:+.2f}%   (compounded, 1x, no leverage)")
    print(f"Max drawdown      : -{mdd:.2f}%")
    for side in ("LONG", "SHORT"):
        sub = [t["pnl"] for t in trades if t["side"] == side]
        if sub:
            print(f"  {side:<5}: {len(sub):>4} trades | net {sum(sub):+.2f}% | "
                  f"win rate {sum(1 for p in sub if p > 0) / len(sub) * 100:.0f}%")
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print("Exit reasons      :", ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))

def save_csv(trades, fname):
    if not trades:
        return
    rows = [dict(t, entry_time=fmt_t(t["entry_time"]), exit_time=fmt_t(t["exit_time"])) for t in trades]
    with open(fname, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"Saved trades -> {fname}")

def main():
    symbol = (input("Symbol (e.g. BTCUSD) [BTCUSD]: ").strip().upper() or "BTCUSD")
    tf = ""
    while tf not in TIMEFRAMES:
        tf = input(f"Timeframe {list(TIMEFRAMES)}: ").strip().lower()
    d = input("Days to backtest (e.g. 365 for 1 year, 90 for 3 months) [365]: ").strip()
    days = int(d) if d.isdigit() else 365

    est = days * 86400 // TIMEFRAMES[tf]
    print(f"\n~{est:,} candles, about {est // 1500 + 1} requests.")
    if est > 200000:
        print("That is a lot of data on a small timeframe; consider 5m or higher.")

    candles = fetch_history(symbol, tf, days)
    print(f"Loaded {len(candles):,} candles: {fmt_t(candles[0]['time'])} -> "
          f"{fmt_t(candles[-1]['time'])} UTC")

    t1, t2 = sim_s1(candles), sim_s2(candles)
    report(f"STRATEGY 1  EMA {FAST}/{SLOW} + confirmations  ({symbol} {tf}, {days}d)", t1)
    report(f"STRATEGY 2  RSI>MA9 + MACD>0, MACD exit  ({symbol} {tf}, {days}d)", t2)

    bh = (candles[-1]["close"] / candles[WARMUP]["close"] - 1) * 100
    print(f"\nBuy & hold over the same period: {bh:+.2f}%")
    print(f"Assumptions: fee {FEE_PCT}%/side, slippage {SLIPPAGE_PCT}%/side, "
          f"entries at next candle open, SL checked before TP.")

    save_csv(t1, f"backtest_{symbol}_{tf}_strategy1.csv")
    save_csv(t2, f"backtest_{symbol}_{tf}_strategy2.csv")

    try:
        from backtest_plots import plot_all
    except ModuleNotFoundError as e:
        print(f"\nCharts skipped: missing module '{e.name}'.")
        if e.name == "matplotlib":
            print("Fix: pip install matplotlib")
        else:
            print("Fix: make sure backtest_plots.py is in the same folder as backtest.py")
        return

    zd = input("\nDays to show in the price chart (e.g. 7) [7]: ").strip()
    plot_all(symbol, tf, days, candles, t1, t2, int(zd) if zd.isdigit() else 7, WARMUP)


if __name__ == "__main__":
    main()