"""
Algo trader UI for Delta Exchange DEMO accounts. Single file - nothing else needed.

Run:   python app.py
Needs: pip install flask requests
"""
import os, time, hmac, hashlib, json, threading, webbrowser
import requests
from flask import Flask, jsonify, request, Response

PORT = 5000
ENVS = {
    "india":  "https://cdn-ind.testnet.deltaex.org",     # demo account on Delta India
    "global": "https://testnet-api.delta.exchange",      # demo account on Delta Global
}

app = Flask(__name__)

LOGS = []
LOG_LOCK = threading.Lock()
START_LOCK = threading.Lock()
STOP = threading.Event()
STATE = {"running": False, "thread": None, "ctx": None, "strategy": None,
         "timeframe": None, "position": 0, "price": None, "started": None}


# ---------------------------------------------------------------- logging
def log(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    with LOG_LOCK:
        for line in msg.split("\n"):
            LOGS.append(line)
        del LOGS[:-600]


# ================================================================
#                      TRADING ENGINE (demo only)
#   These settings are overwritten from the web page when you press Start.
# ================================================================
BASE_URL   = ENVS["india"]
API_KEY    = ""
API_SECRET = ""

# ---- common settings
SL_PCT, TP_PCT   = 2.0, 4.0
CHECK_EVERY      = 10
TIMEFRAMES       = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
MAX_SLIPPAGE_PCT = 1.0
CANDLE_COUNT     = 300
RSI_PERIOD       = 14

# ---- strategy 1
FAST, SLOW = 9, 20
USE_VWAP, USE_PA, USE_RSI = True, True, True
MIN_CONFIRMATIONS = 2
RSI_OB, RSI_OS, RSI_MID = 70, 30, 50
BREAKOUT_N, PA_LOOKBACK = 20, 3

# ---- strategy 2
RSI_MA_PERIOD = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MACD_MIN = 0.0              # MACD must be above this value
S2_RSI_MIN = 0.0            # RSI must be at least this value
EXIT_SHRINK_CANDLES = 1     # histogram must shrink this many candles in a row to exit
S2_USE_TP = False           # True = also attach the take-profit

# ---- strategy 3
S3_LOOKBACK = 20            # support/resistance and average volume come from the PREVIOUS N candles
S3_VOL_MULT = 1.5           # breakout candle volume must exceed this x average volume (0 = ignore volume)
S3_REQUIRE_ENGULF = True    # also require an engulfing candle in the breakout direction

# ---- strategy 4 (price levels)
S4_BUY_AT = None            # buy when the market reaches this price (None = not used)
S4_SELL_AT = None           # sell when the market reaches this price (None = not used)
S4_USE_TP = True            # True = bot exits when PnL reaches Target % (stop-loss % is always applied)
S4_CHECK_EVERY = 2          # seconds between live price checks


# ---------------- API helpers ----------------
def signed_request(method, path, params=None, body=None):
    ts = str(int(time.time()))
    query = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    payload = json.dumps(body, separators=(",", ":")) if body else ""
    sig = hmac.new(API_SECRET.encode(), (method + ts + path + query + payload).encode(),
                   hashlib.sha256).hexdigest()
    headers = {"api-key": API_KEY, "timestamp": ts, "signature": sig,
               "User-Agent": "python-bot", "Content-Type": "application/json"}
    r = requests.request(method, BASE_URL + path + query,
                         data=payload or None, headers=headers, timeout=10)
    return r.json()


def get_candles(symbol, tf, tf_sec):
    end = int(time.time())
    r = requests.get(f"{BASE_URL}/v2/history/candles",
                     params={"symbol": symbol, "resolution": tf,
                             "start": end - tf_sec * CANDLE_COUNT, "end": end},
                     timeout=10).json()
    raw = sorted(r["result"], key=lambda c: c["time"])
    return [{"time": int(c["time"]), "open": float(c["open"]), "high": float(c["high"]),
             "low": float(c["low"]), "close": float(c["close"]),
             "volume": float(c.get("volume") or 0)} for c in raw]


def get_mark_price(symbol):
    r = requests.get(f"{BASE_URL}/v2/tickers/{symbol}", timeout=10).json()
    return float(r["result"]["mark_price"])


def get_position(pid):
    r = signed_request("GET", "/v2/positions", params={"product_id": pid})
    res = r.get("result") or {}
    return int(res.get("size", 0) or 0), float(res.get("entry_price", 0) or 0)


def place_market(pid, side, size):
    return signed_request("POST", "/v2/orders", body={
        "product_id": pid, "size": size, "side": side, "order_type": "market_order"})


def cancel_all_orders(pid):
    return signed_request("DELETE", "/v2/orders/all", body={
        "product_id": pid, "cancel_limit_orders": True, "cancel_stop_orders": True})


def round_tick(price, tick):
    return round(round(price / tick) * tick, 8)


def place_sl_tp(pid, symbol, ref, is_long, tick, use_tp=True):
    if is_long:
        sl, tp = ref * (1 - SL_PCT / 100), ref * (1 + TP_PCT / 100)
    else:
        sl, tp = ref * (1 + SL_PCT / 100), ref * (1 - TP_PCT / 100)
    sl, tp = round_tick(sl, tick), round_tick(tp, tick)
    msg = f"    Ref={ref}  SL={sl} (-{SL_PCT}%)"
    msg += f"  TP={tp} (+{TP_PCT}%)" if use_tp else "  (no fixed TP)"
    log(msg)
    body = {"product_id": pid, "product_symbol": symbol,
            "stop_loss_order": {"order_type": "market_order", "stop_price": str(sl)},
            "bracket_stop_trigger_method": "last_traded_price"}
    if use_tp:
        body["take_profit_order"] = {"order_type": "limit_order",
                                     "stop_price": str(tp), "limit_price": str(tp)}
    return signed_request("POST", "/v2/orders/bracket", body=body)


# ---------------- Indicators ----------------
def ema(values, period):                       # used by strategy 1 (aligned to the end)
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema_full(values, period):                  # same length as input, None until warmed up
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi_series(closes, period=14):             # Wilder's RSI, one value per candle
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    calc = lambda g, l: 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    out[period] = calc(ag, al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = calc(ag, al)
    return out


def rsi(closes, period=14):
    return rsi_series(closes, period)[-1]


def sma_at(series, i, n):
    if i - n + 1 < 0:
        return None
    w = series[i - n + 1:i + 1]
    return None if any(v is None for v in w) else sum(w) / n


def macd_calc(closes):
    e_f, e_s = ema_full(closes, MACD_FAST), ema_full(closes, MACD_SLOW)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e_f, e_s)]
    first = next((i for i, v in enumerate(macd) if v is not None), None)
    if first is None:
        return macd, [None] * len(macd), [None] * len(macd)
    signal = [None] * first + ema_full(macd[first:], MACD_SIGNAL)
    hist = [m - s if m is not None and s is not None else None for m, s in zip(macd, signal)]
    return macd, signal, hist


def session_vwap(candles):
    now = int(time.time()); start = now - (now % 86400)
    pv = vol = 0.0
    for c in candles:
        if c["time"] >= start:
            pv += (c["high"] + c["low"] + c["close"]) / 3 * c["volume"]; vol += c["volume"]
    return pv / vol if vol > 0 else None


def price_action(candles):
    bull, bear = [], []
    for k in range(PA_LOOKBACK):
        sub = candles[:len(candles) - k]
        if len(sub) < BREAKOUT_N + 2:
            break
        cur, prev = sub[-1], sub[-2]
        when = "now" if k == 0 else f"{k} candle(s) ago"
        if (prev["close"] < prev["open"] and cur["close"] > cur["open"]
                and cur["close"] >= prev["open"] and cur["open"] <= prev["close"]):
            bull.append(f"Bullish engulfing ({when})")
        if (prev["close"] > prev["open"] and cur["close"] < cur["open"]
                and cur["close"] <= prev["open"] and cur["open"] >= prev["close"]):
            bear.append(f"Bearish engulfing ({when})")
        window = sub[-BREAKOUT_N - 1:-1]
        if cur["close"] > max(c["high"] for c in window):
            bull.append(f"Breakout above {BREAKOUT_N}-candle high ({when})")
        if cur["close"] < min(c["low"] for c in window):
            bear.append(f"Breakdown below {BREAKOUT_N}-candle low ({when})")
    return {"bull": bull, "bear": bear}


# ---------------- Shared trade execution ----------------
def open_trade(ctx, side, use_tp=True):
    pid, symbol, size, tick = ctx["pid"], ctx["symbol"], ctx["size"], ctx["tick"]
    mark_before = get_mark_price(symbol)
    r = place_market(pid, side, size)
    log("    Entry order:", r)
    if not r.get("success"):
        return False
    time.sleep(2)
    pos, entry = get_position(pid)
    if pos == 0 or entry == 0:
        log("    Could not read position. Check the exchange manually!")
        return False
    close_side = "sell" if pos > 0 else "buy"
    slip = abs(entry - mark_before) / mark_before * 100
    log(f"    Mark={mark_before}  Fill={entry}  Slippage={slip:.2f}%")
    if slip > MAX_SLIPPAGE_PCT:
        log("    !! Slippage too high - closing position")
        log("    ", place_market(pid, close_side, abs(pos)))
        return False
    res = place_sl_tp(pid, symbol, get_mark_price(symbol), pos > 0, tick, use_tp)
    log("    SL/TP response:", res)
    if not res.get("success"):
        log("    !! SL/TP FAILED - closing position for safety")
        log("    ", place_market(pid, close_side, abs(pos)))
        return False
    return True


# ================= STRATEGY 1: EMA crossover + confirmations =================
def s1_analyze(candles):
    closes = [c["close"] for c in candles]
    return {"price": closes[-1], "vwap": session_vwap(candles),
            "rsi": rsi(closes, RSI_PERIOD), "pa": price_action(candles)}


def s1_describe(info):
    v, r = info["vwap"], info["rsi"]
    if v is None:
        vtxt = "VWAP n/a"
    else:
        vtxt = f"VWAP={v:.2f} (price {'ABOVE' if info['price'] > v else 'BELOW'})"
    if r is None:
        rtxt = "RSI n/a"
    else:
        st = ("overbought" if r >= RSI_OB else "oversold" if r <= RSI_OS
              else "bullish momentum" if r >= RSI_MID else "bearish momentum")
        rtxt = f"RSI={r:.1f} ({st})"
    pa = info["pa"]["bull"] + info["pa"]["bear"]
    ptxt = "PA: " + (", ".join(pa) if pa else "no pattern")
    return f"    {vtxt} | {rtxt} | {ptxt}"


def s1_filters(direction, info):
    is_long = direction == "long"
    res = []
    if USE_VWAP:
        v = info["vwap"]
        res.append(("VWAP", v is not None and (info["price"] > v if is_long else info["price"] < v)))
    if USE_PA:
        res.append(("PriceAction", bool(info["pa"]["bull" if is_long else "bear"])))
    if USE_RSI:
        r = info["rsi"]
        if r is None:
            ok = False
        else:
            ok = (RSI_MID <= r < RSI_OB) if is_long else (RSI_OS < r <= RSI_MID)
        res.append(("RSI", ok))
    needed = min(MIN_CONFIRMATIONS, len(res))
    passed = sum(1 for _, ok in res if ok)
    return passed >= needed, passed, needed, res


def s1_signal(direction, info, ctx, position):
    ok, passed, needed, res = s1_filters(direction, info)
    detail = ", ".join(n + ":" + ("PASS" if p else "FAIL") for n, p in res)
    log(f"    Filters [{detail}] -> {passed}/{needed} required")
    if not ok:
        log("    >> Signal SKIPPED (not enough confirmation)")
        return
    log(f"    >> CONFIRMED - going {direction.upper()}")
    cancel_all_orders(ctx["pid"])
    if position != 0:
        place_market(ctx["pid"], "sell" if position > 0 else "buy", abs(position))
        time.sleep(1)
    open_trade(ctx, "buy" if direction == "long" else "sell", use_tp=True)


def tick_s1(candles, position, state, ctx):
    times = [c["time"] for c in candles]
    closes = [c["close"] for c in candles]
    fast, slow = ema(closes, FAST), ema(closes, SLOW)
    f_now, f_prev, s_now, s_prev = fast[-1], fast[-2], slow[-1], slow[-2]
    info = s1_analyze(candles)
    log(f"[{time.strftime('%H:%M:%S')}] {ctx['symbol']} Close={closes[-1]:.2f} | "
        f"EMA{FAST}={f_now:.2f} | EMA{SLOW}={s_now:.2f} | Pos={position:+d}")
    log(s1_describe(info))
    if state["last"] is None:
        state["last"] = times[-1]
        log("    Ignoring existing signal, waiting for the next fresh crossover...")
    elif times[-1] != state["last"]:
        up = f_prev <= s_prev and f_now > s_now
        down = f_prev >= s_prev and f_now < s_now
        if up and position <= 0:
            log(">>> BULLISH CROSS detected")
            s1_signal("long", info, ctx, position)
        elif down and position >= 0:
            log(">>> BEARISH CROSS detected")
            s1_signal("short", info, ctx, position)
        state["last"] = times[-1]


# ================= STRATEGY 2: RSI > RSI-MA + MACD buy, MACD > minimum =================
def s2_at(rs, macd, sig, hist, i):
    if i < 0:
        return None
    r, rma = rs[i], sma_at(rs, i, RSI_MA_PERIOD)
    m, s, h = macd[i], sig[i], hist[i]
    if None in (r, rma, m, s, h):
        return None
    return {"rsi": r, "rsi_ma": rma, "macd": m, "signal": s, "hist": h,
            "rsi_ok": r > rma and r >= S2_RSI_MIN,
            "macd_buy": m > s,
            "macd_pos": m > MACD_MIN}


def s2_all(d):
    return bool(d) and d["rsi_ok"] and d["macd_buy"] and d["macd_pos"]


def s2_exit_signal(hist, macd, sig):
    n = EXIT_SHRINK_CANDLES
    h = hist[-(n + 1):]
    if len(h) < n + 1 or any(v is None for v in h) or macd[-1] is None or sig[-1] is None:
        return False, ""
    if h[0] > 0 and all(h[j + 1] < h[j] for j in range(n)):
        return True, f"MACD histogram shrinking for {n} candle(s) from its high"
    if macd[-1] < sig[-1]:
        return True, "MACD crossed below signal line"
    return False, ""


def tick_s2(candles, position, state, ctx):
    times = [c["time"] for c in candles]
    closes = [c["close"] for c in candles]
    rs = rsi_series(closes, RSI_PERIOD)
    macd, sig, hist = macd_calc(closes)
    i = len(closes) - 1
    cur, prev = s2_at(rs, macd, sig, hist, i), s2_at(rs, macd, sig, hist, i - 1)
    if not cur:
        log(f"[{time.strftime('%H:%M:%S')}] Not enough candles yet for RSI/MACD...")
        return
    ok = lambda b: "OK" if b else "--"
    log(f"[{time.strftime('%H:%M:%S')}] {ctx['symbol']} Close={closes[-1]:.2f} | Pos={position:+d}")
    log(f"    RSI={cur['rsi']:.1f} vs MA{RSI_MA_PERIOD}={cur['rsi_ma']:.1f} (min {S2_RSI_MIN:g}) [{ok(cur['rsi_ok'])}] | "
        f"MACD={cur['macd']:.2f} Sig={cur['signal']:.2f} [{ok(cur['macd_buy'])}] | "
        f"MACD>{MACD_MIN:g} [{ok(cur['macd_pos'])}] | Hist={cur['hist']:.2f}")

    if position == 0:
        state["entry_time"] = None          # flat (e.g. stop-loss hit), reset

    # EXIT: judged only on candles that closed after the entry candle
    if position > 0 and (state["entry_time"] is None or times[-1] > state["entry_time"]):
        exit_now, why = s2_exit_signal(hist, macd, sig)
        if exit_now:
            log(f">>> EXIT: {why}")
            cancel_all_orders(ctx["pid"])
            log("    ", place_market(ctx["pid"], "sell", abs(position)))
            state["entry_time"] = None
            return

    # ENTRY: long only, fresh setup on a newly closed candle
    if state["last"] is None:
        state["last"] = times[-1]
        log("    Ignoring existing setup, waiting for a fresh buy signal...")
    elif times[-1] != state["last"]:
        state["last"] = times[-1]
        if position == 0 and s2_all(cur) and not s2_all(prev):
            log(">>> BUY SIGNAL: RSI > RSI-MA, MACD buy signal, MACD above minimum")
            if open_trade(ctx, "buy", use_tp=S2_USE_TP):
                state["entry_time"] = times[-1]


# ================= STRATEGY 3: breakout + volume + engulfing candle =================
def s3_analyze(candles):
    n = S3_LOOKBACK
    if len(candles) < n + 2:
        return None
    cur, prev, window = candles[-1], candles[-2], candles[-n - 1:-1]   # window excludes the current candle
    res = max(c["high"] for c in window)
    sup = min(c["low"] for c in window)
    avg_vol = sum(c["volume"] for c in window) / n
    vol_ok = S3_VOL_MULT <= 0 or cur["volume"] > S3_VOL_MULT * avg_vol
    bull_engulf = (prev["close"] < prev["open"] and cur["close"] > cur["open"]
                   and cur["open"] <= prev["close"] and cur["close"] >= prev["open"])
    bear_engulf = (prev["close"] > prev["open"] and cur["close"] < cur["open"]
                   and cur["open"] >= prev["close"] and cur["close"] <= prev["open"])
    return {"close": cur["close"], "volume": cur["volume"], "avg_vol": avg_vol, "res": res, "sup": sup,
            "vol_ok": vol_ok, "bull_engulf": bull_engulf, "bear_engulf": bear_engulf,
            "bull_breakout": cur["close"] > res and vol_ok,
            "bear_breakout": cur["close"] < sup and vol_ok}


def tick_s3(candles, position, state, ctx):
    times = [c["time"] for c in candles]
    info = s3_analyze(candles)
    if not info:
        log(f"[{time.strftime('%H:%M:%S')}] Not enough candles yet for the {S3_LOOKBACK}-candle lookback...")
        return
    log(f"[{time.strftime('%H:%M:%S')}] {ctx['symbol']} Close={info['close']:.2f} | "
        f"Resistance={info['res']:.2f} | Support={info['sup']:.2f} | Pos={position:+d}")
    log(f"    Volume={info['volume']:.0f} vs avg {info['avg_vol']:.0f} (need x{S3_VOL_MULT:g}) "
        f"[{'OK' if info['vol_ok'] else '--'}] | "
        f"Engulfing: {'bullish' if info['bull_engulf'] else 'bearish' if info['bear_engulf'] else 'none'}")
    if state["last"] is None:
        state["last"] = times[-1]
        log("    Ignoring existing signal, waiting for a fresh breakout...")
        return
    if times[-1] == state["last"]:
        return
    state["last"] = times[-1]
    long_ok = info["bull_breakout"] and (info["bull_engulf"] or not S3_REQUIRE_ENGULF)
    short_ok = info["bear_breakout"] and (info["bear_engulf"] or not S3_REQUIRE_ENGULF)
    direction = "long" if long_ok and position <= 0 else "short" if short_ok and position >= 0 else None
    if not direction:
        return
    log(f">>> {'BULLISH' if direction == 'long' else 'BEARISH'} BREAKOUT CONFIRMED - going {direction.upper()}")
    cancel_all_orders(ctx["pid"])
    if position != 0:
        place_market(ctx["pid"], "sell" if position > 0 else "buy", abs(position))
        time.sleep(1)
    open_trade(ctx, "buy" if direction == "long" else "sell", use_tp=True)


# ================= STRATEGY 4: execute when the live price reaches your level =================
def s4_fire(side, ctx, position):
    """Close an opposite position, otherwise open a new trade (with stop-loss)."""
    if (side == "buy" and position < 0) or (side == "sell" and position > 0):
        log(f"    Closing the existing {'short' if position < 0 else 'long'} position ({abs(position)})")
        cancel_all_orders(ctx["pid"])
        r = place_market(ctx["pid"], side, abs(position))
        log("    Close order:", "OK" if r.get("success") else r)
    elif (side == "buy" and position > 0) or (side == "sell" and position < 0):
        log(f"    Already {'long' if position > 0 else 'short'}, no extra order placed")
    else:
        open_trade(ctx, side, use_tp=False)


def tick_s4(candles, position, state, ctx):
    price, pid = state["price"], ctx["pid"]
    levels = {"buy": S4_BUY_AT, "sell": S4_SELL_AT}
    state.setdefault("done", {})
    state.setdefault("start_side", {})
    waiting = []

    # 1) price levels: send an order once when the live price reaches each level
    for side, level in levels.items():
        if level is None or state["done"].get(side):
            continue
        if side not in state["start_side"]:
            state["start_side"][side] = "above" if price > level else "below"
            log(f"    {side.upper()} level {level:g}: price {price:g} is {state['start_side'][side]} it, "
                f"waiting for the market to {'fall to' if price > level else 'rise to'} it")
        reached = price <= level if state["start_side"][side] == "above" else price >= level
        if reached:
            log(f">>> PRICE LEVEL REACHED: {price:g} vs {side.upper()} at {level:g} - sending {side.upper()} order")
            state["done"][side] = True
            s4_fire(side, ctx, position)
            position, state["entry"] = get_position(pid)
            state["position"] = position
        else:
            waiting.append(f"{side} at {level:g}")

    # 2) PnL watch on the open position: exit at -Stop-loss % or +Target %
    entry, pnl_txt = state.get("entry") or 0, ""
    if position != 0 and entry > 0:
        pnl = (price - entry) / entry * 100 * (1 if position > 0 else -1)
        pnl_txt = f" | Entry={entry:g} PnL={pnl:+.2f}%"
        why = None
        if pnl <= -SL_PCT:
            why = f"STOP-LOSS: PnL {pnl:+.2f}% reached -{SL_PCT:g}%"
        elif S4_USE_TP and pnl >= TP_PCT:
            why = f"TARGET: PnL {pnl:+.2f}% reached +{TP_PCT:g}%"
        if why:
            log(f">>> EXIT - {why}")
            cancel_all_orders(pid)
            r = place_market(pid, "sell" if position > 0 else "buy", abs(position))
            log("    Close order:", "OK" if r.get("success") else r)
            if r.get("success"):
                position = state["position"] = 0
                pnl_txt = ""

    # 3) status line (the bot stays on until you press Stop)
    if position == 0 and not waiting:
        if not state.get("idle"):
            log("[app] Your price levels are done and there is no open position. The bot stays on but idle - "
                "press Stop bot when you are finished.")
            state["idle"] = True
    else:
        state["idle"] = False
        log(f"[{time.strftime('%H:%M:%S')}] {ctx['symbol']} Price={price:g} | Pos={position:+d}{pnl_txt} | "
            + (f"waiting: {', '.join(waiting)}" if waiting else "watching the open position"))


# ================================================================
#                          WEB APP
# ================================================================
def fetch_products(env):
    r = requests.get(ENVS[env] + "/v2/products",
                     params={"contract_types": "perpetual_futures", "page_size": 100},
                     timeout=15).json()
    return sorted([p for p in r["result"] if p.get("state") == "live"], key=lambda p: p["symbol"])


def _num(d, key, label, cast=float, lo=None, hi=None):
    try:
        v = cast(str(d.get(key, "")).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if lo is not None and v < lo:
        raise ValueError(f"{label} must be at least {lo}.")
    if hi is not None and v > hi:
        raise ValueError(f"{label} must be at most {hi}.")
    return v


def parse_config(d):
    c = {}
    c["env"] = d.get("env")
    if c["env"] not in ENVS:
        raise ValueError("Choose a demo environment.")
    c["api_key"] = (d.get("api_key") or "").strip()
    c["api_secret"] = (d.get("api_secret") or "").strip()
    if not c["api_key"] or not c["api_secret"]:
        raise ValueError("Enter both the API key and the API secret.")
    c["strategy"] = str(d.get("strategy"))
    if c["strategy"] not in ("1", "2", "3", "4"):
        raise ValueError("Choose strategy 1, 2, 3 or 4.")
    c["symbol"] = (d.get("symbol") or "").strip().upper()
    if not c["symbol"]:
        raise ValueError("Choose a coin. Use Reload coins if the list is empty.")
    c["timeframe"] = d.get("timeframe")
    if c["timeframe"] not in TIMEFRAMES:
        raise ValueError("Choose a timeframe.")
    c["size"] = _num(d, "size", "Size", int, 1, 100000)
    c["sl_pct"] = _num(d, "sl_pct", "Stop-loss %", float, 0.1, 50)
    c["tp_pct"] = _num(d, "tp_pct", "Target %", float, 0.1, 500)
    c["rsi_period"] = _num(d, "rsi_period", "RSI period", int, 2, 100)

    if c["strategy"] == "1":
        c["fast"] = _num(d, "fast", "Fast EMA", int, 2, 250)
        c["slow"] = _num(d, "slow", "Slow EMA", int, 3, 250)
        if c["fast"] >= c["slow"]:
            raise ValueError("The fast EMA must be shorter than the slow EMA (for example 9 and 20).")
        c["use_vwap"] = bool(d.get("use_vwap"))
        c["use_pa"] = bool(d.get("use_pa"))
        c["use_rsi"] = bool(d.get("use_rsi"))
        if not (c["use_vwap"] or c["use_pa"] or c["use_rsi"]):
            raise ValueError("Turn on at least one confirmation (VWAP, price action or RSI).")
        c["min_conf"] = _num(d, "min_conf", "Confirmations needed", int, 1, 3)
        c["rsi_mid"] = _num(d, "rsi_mid", "RSI midline", float, 1, 99)
        c["rsi_ob"] = _num(d, "rsi_ob", "RSI overbought level", float, 1, 100)
        c["rsi_os"] = _num(d, "rsi_os", "RSI oversold level", float, 0, 99)
        if not (c["rsi_os"] < c["rsi_mid"] < c["rsi_ob"]):
            raise ValueError("RSI levels must satisfy: oversold < midline < overbought.")
        c["brk"] = _num(d, "brk", "Breakout lookback", int, 3, 100)
    elif c["strategy"] == "3":
        c["s3_lookback"] = _num(d, "s3_lookback", "Breakout lookback", int, 3, 100)
        c["s3_vol"] = _num(d, "s3_vol", "Volume multiplier", float, 0, 20)
        c["s3_engulf"] = bool(d.get("s3_engulf"))
    elif c["strategy"] == "4":
        for key, label in (("s4_buy", "Buy at"), ("s4_sell", "Sell at")):
            raw = str(d.get(key, "")).strip()
            c[key] = _num(d, key, label, float, 0.0000001) if raw else None
        if c["s4_buy"] is None and c["s4_sell"] is None:
            raise ValueError("Enter a Buy at price, a Sell at price, or both.")
        c["s4_tp"] = bool(d.get("s4_tp"))
    else:
        c["rsi_ma"] = _num(d, "rsi_ma", "RSI moving average period", int, 2, 100)
        c["macd_fast"] = _num(d, "macd_fast", "MACD fast", int, 2, 100)
        c["macd_slow"] = _num(d, "macd_slow", "MACD slow", int, 3, 200)
        c["macd_signal"] = _num(d, "macd_signal", "MACD signal", int, 2, 100)
        if c["macd_fast"] >= c["macd_slow"]:
            raise ValueError("MACD fast must be shorter than MACD slow (for example 12 and 26).")
        c["macd_min"] = _num(d, "macd_min", "MACD minimum", float)
        c["rsi_min"] = _num(d, "rsi_min", "RSI minimum", float, 0, 100)
        c["shrink"] = _num(d, "shrink", "Exit after histogram shrinks for", int, 1, 20)
        c["s2_tp"] = bool(d.get("s2_tp"))
    return c


def apply_config(c):
    global BASE_URL, API_KEY, API_SECRET, SL_PCT, TP_PCT, RSI_PERIOD
    global FAST, SLOW, USE_VWAP, USE_PA, USE_RSI, MIN_CONFIRMATIONS
    global RSI_MID, RSI_OB, RSI_OS, BREAKOUT_N
    global RSI_MA_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL, MACD_MIN, S2_RSI_MIN
    global EXIT_SHRINK_CANDLES, S2_USE_TP, S3_LOOKBACK, S3_VOL_MULT, S3_REQUIRE_ENGULF
    global S4_BUY_AT, S4_SELL_AT, S4_USE_TP
    BASE_URL = ENVS[c["env"]]
    API_KEY, API_SECRET = c["api_key"], c["api_secret"]
    SL_PCT, TP_PCT = c["sl_pct"], c["tp_pct"]
    RSI_PERIOD = c["rsi_period"]
    if c["strategy"] == "1":
        FAST, SLOW = c["fast"], c["slow"]
        USE_VWAP, USE_PA, USE_RSI = c["use_vwap"], c["use_pa"], c["use_rsi"]
        MIN_CONFIRMATIONS = c["min_conf"]
        RSI_MID, RSI_OB, RSI_OS = c["rsi_mid"], c["rsi_ob"], c["rsi_os"]
        BREAKOUT_N = c["brk"]
    elif c["strategy"] == "3":
        S3_LOOKBACK, S3_VOL_MULT, S3_REQUIRE_ENGULF = c["s3_lookback"], c["s3_vol"], c["s3_engulf"]
    elif c["strategy"] == "4":
        S4_BUY_AT, S4_SELL_AT, S4_USE_TP = c["s4_buy"], c["s4_sell"], c["s4_tp"]
    else:
        RSI_MA_PERIOD = c["rsi_ma"]
        MACD_FAST, MACD_SLOW, MACD_SIGNAL = c["macd_fast"], c["macd_slow"], c["macd_signal"]
        MACD_MIN, S2_RSI_MIN = c["macd_min"], c["rsi_min"]
        EXIT_SHRINK_CANDLES = c["shrink"]
        S2_USE_TP = c["s2_tp"]


def describe_config(c):
    if c["strategy"] == "4":
        parts = [f"BUY when the market reaches {c['s4_buy']:g}" if c["s4_buy"] else None,
                 f"SELL when the market reaches {c['s4_sell']:g}" if c["s4_sell"] else None]
        return "Strategy 4 | " + " | ".join(p for p in parts if p) + \
               f" | each level fires once | then exit at -{c['sl_pct']:g}% / " + (f"+{c['tp_pct']:g}%" if c["s4_tp"] else "no target") + " PnL | stays running"
    if c["strategy"] == "3":
        return (f"Strategy 3 | breakout of previous {c['s3_lookback']}-candle high/low | "
                + (f"volume > {c['s3_vol']:g}x average" if c["s3_vol"] > 0 else "volume ignored")
                + " | engulfing candle " + ("required" if c["s3_engulf"] else "not required"))
    if c["strategy"] == "1":
        on = [n for n, f in (("VWAP", c["use_vwap"]), ("price action", c["use_pa"]), ("RSI", c["use_rsi"])) if f]
        return (f"Strategy 1 | EMA {c['fast']}/{c['slow']} | confirmations: {', '.join(on)} "
                f"(need {min(c['min_conf'], len(on))}) | RSI {c['rsi_os']:g}/{c['rsi_mid']:g}/{c['rsi_ob']:g}")
    return (f"Strategy 2 | RSI({c['rsi_period']}) > MA{c['rsi_ma']} and RSI >= {c['rsi_min']:g} | "
            f"MACD({c['macd_fast']},{c['macd_slow']},{c['macd_signal']}) buy signal and MACD > {c['macd_min']:g} | "
            f"exit after {c['shrink']} shrinking candle(s)")


# ---------------------------------------------------------------- bot loop
def run_bot(strategy, tf, ctx):
    tick_fn = {"1": tick_s1, "2": tick_s2, "3": tick_s3, "4": tick_s4}[strategy]
    tf_sec = TIMEFRAMES[tf]
    state = {"last": None, "entry_time": None, "price": None}
    try:
        while not STOP.is_set():
            try:
                if strategy == "4":
                    candles, price = None, get_mark_price(ctx["symbol"])      # live price, no candles needed
                else:
                    candles = get_candles(ctx["symbol"], tf, tf_sec)[:-1]      # closed candles only
                    price = candles[-1]["close"]
                position, entry = get_position(ctx["pid"])
                STATE["position"] = position
                STATE["price"] = state["price"] = price
                state["entry"] = entry
                tick_fn(candles, position, state, ctx)
                STATE["position"] = state.get("position", position)
            except Exception as e:
                log(f"Error: {e}")
            STOP.wait(S4_CHECK_EVERY if strategy == "4" else CHECK_EVERY)
    finally:
        STATE["running"] = False
        log("[app] Bot stopped.")


# ---------------------------------------------------------------- routes
@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/symbols")
def symbols():
    env = request.args.get("env", "india")
    if env not in ENVS:
        return jsonify(error="Unknown environment."), 400
    try:
        return jsonify(symbols=[p["symbol"] for p in fetch_products(env)])
    except Exception as e:
        return jsonify(error=f"Could not load coins from Delta ({e})."), 502


@app.post("/api/start")
def start():
    with START_LOCK:
        if STATE["running"]:
            return jsonify(error="A bot is already running. Stop it first."), 400
        try:
            c = parse_config(request.get_json(force=True, silent=True) or {})
        except ValueError as e:
            return jsonify(error=str(e)), 400

        apply_config(c)

        # 1) check the keys before starting
        try:
            res = signed_request("GET", "/v2/wallet/balances")
        except Exception as e:
            return jsonify(error=f"Could not reach Delta: {e}"), 502
        if not res.get("success"):
            err = res.get("error")
            code = err.get("code") if isinstance(err, dict) else err
            return jsonify(error=f"Delta rejected the API key ({code}). Check that the key and secret come "
                                 f"from a demo account on the same environment you selected (India or "
                                 f"Global), and that trading is enabled on the key. If you set an IP "
                                 f"whitelist on the key, add this computer's IP."), 400

        # 2) find the coin
        try:
            prod = next((p for p in fetch_products(c["env"]) if p["symbol"] == c["symbol"]), None)
        except Exception as e:
            return jsonify(error=f"Could not load coins from Delta ({e})."), 502
        if not prod:
            return jsonify(error=f"{c['symbol']} is not a live contract on this environment."), 400

        ctx = {"pid": prod["id"], "symbol": prod["symbol"], "size": c["size"],
               "tick": float(prod.get("tick_size", 0.5))}

        with LOG_LOCK:
            LOGS.clear()
        log("[app] Connected to Delta demo (" + c["env"] + ").")
        log("[app] " + describe_config(c))
        log(f"[app] {ctx['symbol']} | {c['timeframe']} | size {c['size']} | "
            f"SL {c['sl_pct']:g}% | TP {c['tp_pct']:g}%")
        log("[app] Bot running. Orders are placed automatically when the conditions are met.")

        STOP.clear()
        STATE.update(running=True, ctx=ctx, strategy=c["strategy"], timeframe=c["timeframe"],
                     position=0, price=None, started=time.time())
        t = threading.Thread(target=run_bot, args=(c["strategy"], c["timeframe"], ctx), daemon=True)
        STATE["thread"] = t
        t.start()
        return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    d = request.get_json(force=True, silent=True) or {}
    close = bool(d.get("close"))
    ctx = STATE.get("ctx")
    STOP.set()
    t = STATE.get("thread")
    if t and t.is_alive():
        t.join(timeout=20)
    if close and ctx:
        try:
            log("[app] Cancelling open orders and closing the position...")
            cancel_all_orders(ctx["pid"])
            pos, _ = get_position(ctx["pid"])
            if pos != 0:
                res = place_market(ctx["pid"], "sell" if pos > 0 else "buy", abs(pos))
                log("[app] Close order sent:", "OK" if res.get("success") else res)
            else:
                log("[app] No open position to close.")
        except Exception as e:
            log(f"[app] Could not close the position automatically: {e}. Check the Delta demo site.")
    elif not close and STATE.get("position"):
        log("[app] Bot stopped. Any open position and its stop-loss/target stay on the exchange.")
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    with LOG_LOCK:
        lines = LOGS[-300:]
    ctx = STATE.get("ctx") or {}
    return jsonify(running=STATE["running"], logs=lines, position=STATE["position"],
                   price=STATE["price"], symbol=ctx.get("symbol"), strategy=STATE["strategy"],
                   timeframe=STATE["timeframe"], started=STATE["started"])


# ---------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Algo trader - Delta demo</title>
<style>
  :root{
    --bg:#e8edf1; --panel:#ffffff; --ink:#172230; --muted:#5a6a79; --line:#d0d9e1;
    --accent:#0e5a64; --accent-d:#0a464e; --danger:#b3261e; --ok:#1b7f4b;
    --tape:#14202b; --tape-ink:#d5e1e9;
    --sans:"Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;
    --mono:"Cascadia Mono",Consolas,"SF Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
  header{padding:22px clamp(16px,4vw,40px) 6px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{margin:0;font-size:22px;font-weight:650;letter-spacing:-.01em}
  header p{margin:0;color:var(--muted)}
  main{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,.95fr);gap:20px;
       padding:14px clamp(16px,4vw,40px) 40px;align-items:start}
  @media (max-width:960px){main{grid-template-columns:1fr}}
  section.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}
  h2{margin:0 0 4px;font-size:16px;font-weight:650}
  .hint{margin:0 0 14px;color:var(--muted);font-size:13.5px;max-width:62ch}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px 14px}
  label{display:block;font-size:13.5px;color:var(--muted)}
  label>span{display:block;margin-bottom:3px}
  input[type=text],input[type=password],input[type=number],select{
    width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;
    color:var(--ink);font:inherit}
  input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  .checks{display:flex;flex-wrap:wrap;gap:8px 18px;margin:12px 0 2px}
  .checks label{display:flex;align-items:center;gap:7px;color:var(--ink)}
  .row{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
  .row>label{flex:1 1 220px}
  button{font:inherit;border-radius:7px;padding:9px 16px;border:1px solid var(--line);background:#fff;
         color:var(--ink);cursor:pointer}
  button:hover{background:#f3f6f8}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;padding:11px 22px}
  button.primary:hover{background:var(--accent-d)}
  button.danger{border-color:var(--danger);color:var(--danger)}
  button.danger:hover{background:#fdf1f0}
  button:disabled{opacity:.5;cursor:not-allowed}
  .picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
  @media (max-width:640px){.picks{grid-template-columns:1fr}}
  .pick{position:relative;border:1.5px solid var(--line);border-radius:9px;padding:13px 14px 12px 40px;cursor:pointer;background:#fff}
  .pick input{position:absolute;left:14px;top:16px;accent-color:var(--accent)}
  .pick b{display:block;margin-bottom:3px}
  .pick small{color:var(--muted);font-size:13px;display:block}
  .pick:has(input:checked){border-color:var(--accent);background:#eff7f7}
  .pick:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:1px}
  #msg{margin-top:12px;min-height:20px;font-size:14px}
  #msg.err{color:var(--danger)} #msg.ok{color:var(--ok)}
  .side{position:sticky;top:12px}
  @media (max-width:960px){.side{position:static}}
  .state{display:flex;gap:18px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
  .chip{display:inline-flex;align-items:center;gap:8px;font-weight:650}
  .dot{width:10px;height:10px;border-radius:50%;background:#9aa8b4}
  .running .dot{background:var(--ok);box-shadow:0 0 0 4px rgba(27,127,75,.18)}
  .kv{color:var(--muted);font-size:14px}.kv b{color:var(--ink);font-weight:650}
  #tape{background:var(--tape);color:var(--tape-ink);border-radius:8px;padding:12px 14px;height:460px;
        overflow:auto;font:12.5px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word}
  #tape .hl{color:#7fe0b0;font-weight:600}
  #tape .bad{color:#ff9d94}
  #tape .app{color:#8fb7ff}
  #tape .dim{color:#8ea1af}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
  .note{font-size:12.5px;color:var(--muted);margin-top:10px}
  .hide{display:none!important}
  @media (prefers-reduced-motion:no-preference){button{transition:background .12s}}
</style>
</head>
<body>
<header>
  <h1>Algo trader</h1>
  <p>Runs your strategy on a Delta Exchange demo account and places orders when the conditions match.</p>
</header>

<main>
<form id="f" autocomplete="off">

  <section class="card">
    <h2>Connect your demo account</h2>
    <p class="hint">Create an API key on the demo site with trading enabled. Keys stay on this computer and are sent only to Delta.</p>
    <div class="grid">
      <label><span>Demo environment</span>
        <select id="env">
          <option value="india">Delta India demo</option>
          <option value="global">Delta Global demo</option>
        </select></label>
      <label><span>API key</span><input id="key" type="text" spellcheck="false" autocomplete="off"></label>
      <label><span>API secret</span><input id="secret" type="password" autocomplete="new-password"></label>
    </div>
  </section>

  <section class="card">
    <h2>Pick a strategy</h2>
    <p class="hint">The bot only acts on closed candles, and ignores a signal that already exists when you start.</p>
    <div class="picks">
      <label class="pick"><input type="radio" name="strategy" value="1" checked>
        <b>Strategy 1: EMA crossover</b>
        <small>Buy when the fast EMA crosses above the slow EMA, sell when it crosses below. Only trades if enough confirmations agree: price versus VWAP, engulfing or breakout candles, and RSI. Long and short.</small></label>
      <label class="pick"><input type="radio" name="strategy" value="2">
        <b>Strategy 2: RSI and MACD momentum</b>
        <small>Buy (long only) when RSI is above its own moving average, MACD is above its signal line, and MACD is above your minimum. Sell when MACD slows down from its high or crosses back below the signal line.</small></label>
      <label class="pick"><input type="radio" name="strategy" value="3">
        <b>Strategy 3: Breakout with volume</b>
        <small>Buy when a candle closes above the highest high of the previous candles on strong volume, with a bullish engulfing candle. Sell is the mirror image below the lowest low. Long and short.</small></label>
      <label class="pick"><input type="radio" name="strategy" value="4">
        <b>Strategy 4: Price levels</b>
        <small>You type the price to buy at and/or the price to sell at. When the live market reaches that price, the bot sends the order to your account.</small></label>
    </div>
  </section>

  <section class="card">
    <h2>What to trade</h2>
    <div class="grid">
      <label><span>Coin</span><select id="symbol"><option value="">Loading...</option></select></label>
      <label><span>Candle size</span>
        <select id="tf"><option>1m</option><option selected>5m</option><option>15m</option><option>1h</option></select></label>
      <label><span>Size (contracts)</span><input id="size" type="number" min="1" step="1" value="1"></label>
    </div>
    <div class="row" style="margin-top:10px">
      <button type="button" id="reload">Reload coins</button>
    </div>
  </section>

  <section class="card s1">
    <h2>Strategy 1 settings</h2>
    <p class="hint">Common EMA pairs are 9 and 20, 20 and 50, or 50 and 200. Higher numbers react slower and trade less often.</p>
    <div class="grid">
      <label><span>Fast EMA</span><input id="fast" type="number" min="2" max="250" value="9"></label>
      <label><span>Slow EMA</span><input id="slow" type="number" min="3" max="250" value="20"></label>
      <label><span>Confirmations needed</span>
        <select id="min_conf"><option>1</option><option selected>2</option><option>3</option></select></label>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="use_vwap" checked> VWAP: price above (buy) or below (sell)</label>
      <label><input type="checkbox" id="use_pa" checked> Price action: engulfing candle or breakout</label>
      <label><input type="checkbox" id="use_rsi" checked> RSI between the levels below</label>
    </div>
    <div class="grid" style="margin-top:10px">
      <label><span>RSI midline</span><input id="rsi_mid" type="number" value="50"></label>
      <label><span>RSI overbought</span><input id="rsi_ob" type="number" value="70"></label>
      <label><span>RSI oversold</span><input id="rsi_os" type="number" value="30"></label>
      <label><span>Breakout lookback (candles)</span><input id="brk" type="number" min="3" value="20"></label>
    </div>
    <p class="note">Buy needs RSI between the midline and overbought. Sell needs RSI between oversold and the midline.</p>
  </section>

  <section class="card s2 hide">
    <h2>Strategy 2 settings</h2>
    <p class="hint">Buy when all three hold on a closed candle that just turned true. Sell when the MACD histogram shrinks from its high.</p>
    <div class="grid">
      <label><span>RSI moving average period</span><input id="rsi_ma" type="number" min="2" value="9"></label>
      <label><span>RSI minimum</span><input id="rsi_min" type="number" min="0" max="100" value="0"></label>
      <label><span>MACD fast</span><input id="macd_fast" type="number" min="2" value="12"></label>
      <label><span>MACD slow</span><input id="macd_slow" type="number" min="3" value="26"></label>
      <label><span>MACD signal</span><input id="macd_signal" type="number" min="2" value="9"></label>
      <label><span>MACD minimum</span><input id="macd_min" type="number" step="any" value="0"></label>
      <label><span>Exit after histogram shrinks for (candles)</span><input id="shrink" type="number" min="1" value="1"></label>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="s2_tp"> Also set the fixed target below</label>
    </div>
  </section>

  <section class="card s3 hide">
    <h2>Strategy 3 settings</h2>
    <p class="hint">Resistance, support and average volume are measured on the candles before the signal candle. The bot enters when that candle closes, with no retest.</p>
    <div class="grid">
      <label><span>Lookback (candles)</span><input id="s3_lookback" type="number" min="3" max="100" value="20"></label>
      <label><span>Volume multiplier (0 = ignore volume)</span><input id="s3_vol" type="number" step="any" min="0" value="1.5"></label>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="s3_engulf" checked> Also require an engulfing candle</label>
    </div>
    <p class="note">Demo markets often have thin or zero volume. If the log keeps showing volume as not OK, lower the multiplier or set it to 0.</p>
  </section>

  <section class="card s4 hide">
    <h2>Strategy 4 settings</h2>
    <p class="hint">Fill in one price or both. The bot watches the live price every 2 seconds. When the market reaches a level, it sends a market order once for that level, then keeps watching your position's profit or loss. Leave a box empty to skip that side.</p>
    <div class="grid">
      <label><span>Buy at (price)</span><input id="s4_buy" type="number" step="any" min="0" placeholder="e.g. 60000"></label>
      <label><span>Sell at (price)</span><input id="s4_sell" type="number" step="any" min="0" placeholder="e.g. 62000"></label>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="s4_tp" checked> Exit automatically when profit reaches the Target % below (the Stop-loss % always applies)</label>
    </div>
    <p class="note">If you hold a short, the Buy level closes it. If you hold a long, the Sell level closes it. Otherwise the level opens a new trade. Profit or loss is the price move from your entry price (not multiplied by leverage): the bot closes the trade at market when it reaches minus the Stop-loss % or plus the Target %, and an exchange stop-loss is also placed as a backup. Stopping the bot stops the Target exit. The price must actually reach the level while this window and the terminal are open and the computer is awake.</p>
  </section>

  <section class="card">
    <h2>Risk per trade</h2>
    <p class="hint">Every entry gets a stop-loss. If the stop-loss cannot be placed, the bot closes the trade straight away.</p>
    <div class="grid">
      <label><span>Stop-loss (%)</span><input id="sl" type="number" step="0.1" min="0.1" value="2"></label>
      <label><span>Target (%)</span><input id="tp" type="number" step="0.1" min="0.1" value="4"></label>
      <label><span>RSI period</span><input id="rsi_period" type="number" min="2" value="14"></label>
    </div>
  </section>

  <button class="primary" type="submit" id="start">Start bot</button>
  <div id="msg" role="status"></div>
</form>

<aside class="side">
  <section class="card">
    <div class="state">
      <span class="chip" id="chip"><span class="dot"></span><span id="stateText">Not running</span></span>
      <span class="kv">Coin <b id="kvSym">-</b></span>
      <span class="kv">Last close <b id="kvPx">-</b></span>
      <span class="kv">Position <b id="kvPos">-</b></span>
    </div>
    <div id="tape" aria-live="polite"><span class="dim">The log appears here once the bot starts.</span></div>
    <div class="actions">
      <button type="button" id="stop" disabled>Stop bot</button>
      <button type="button" id="stopClose" class="danger" disabled>Stop and close position</button>
    </div>
    <p class="note">Stop bot leaves any open position and its stop-loss/target on the exchange. Keep this window and the terminal open while the bot runs.</p>
  </section>
</aside>
</main>

<script>
const $ = id => document.getElementById(id);
const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let running = false;

function strategy(){ return document.querySelector('input[name=strategy]:checked').value; }

function syncSections(){
  const s = strategy();
  document.querySelectorAll('.s1').forEach(e => e.classList.toggle('hide', s !== '1'));
  document.querySelectorAll('.s2').forEach(e => e.classList.toggle('hide', s !== '2'));
  document.querySelectorAll('.s3').forEach(e => e.classList.toggle('hide', s !== '3'));
  document.querySelectorAll('.s4').forEach(e => e.classList.toggle('hide', s !== '4'));
}
document.querySelectorAll('input[name=strategy]').forEach(r => r.addEventListener('change', syncSections));

async function loadSymbols(){
  const sel = $('symbol'), prev = sel.value;
  sel.innerHTML = '<option value="">Loading...</option>';
  try{
    const r = await fetch('/api/symbols?env=' + $('env').value);
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || 'failed');
    sel.innerHTML = j.symbols.map(s => `<option>${s}</option>`).join('');
    sel.value = j.symbols.includes(prev) ? prev : (j.symbols.includes('BTCUSD') ? 'BTCUSD' : j.symbols[0]);
  }catch(e){
    sel.innerHTML = '<option value="">Could not load coins</option>';
    say(String(e.message || e), true);
  }
}
$('env').addEventListener('change', loadSymbols);
$('reload').addEventListener('click', loadSymbols);

function say(t, err){ const m = $('msg'); m.textContent = t; m.className = err ? 'err' : 'ok'; }

function collect(){
  return {
    env: $('env').value, api_key: $('key').value, api_secret: $('secret').value,
    strategy: strategy(), symbol: $('symbol').value, timeframe: $('tf').value, size: $('size').value,
    sl_pct: $('sl').value, tp_pct: $('tp').value, rsi_period: $('rsi_period').value,
    fast: $('fast').value, slow: $('slow').value, min_conf: $('min_conf').value,
    use_vwap: $('use_vwap').checked, use_pa: $('use_pa').checked, use_rsi: $('use_rsi').checked,
    rsi_mid: $('rsi_mid').value, rsi_ob: $('rsi_ob').value, rsi_os: $('rsi_os').value, brk: $('brk').value,
    rsi_ma: $('rsi_ma').value, rsi_min: $('rsi_min').value, macd_fast: $('macd_fast').value,
    macd_slow: $('macd_slow').value, macd_signal: $('macd_signal').value, macd_min: $('macd_min').value,
    shrink: $('shrink').value, s2_tp: $('s2_tp').checked,
    s3_lookback: $('s3_lookback').value, s3_vol: $('s3_vol').value, s3_engulf: $('s3_engulf').checked,
    s4_buy: $('s4_buy').value, s4_sell: $('s4_sell').value, s4_tp: $('s4_tp').checked
  };
}

$('f').addEventListener('submit', async ev => {
  ev.preventDefault();
  say('Checking your keys...', false);
  $('start').disabled = true;
  try{
    const r = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify(collect())});
    const j = await r.json();
    if(!r.ok){ say(j.error || 'Could not start the bot.', true); $('start').disabled = false; return; }
    say('Bot started.', false);
  }catch(e){ say('Could not reach the app: ' + e, true); $('start').disabled = false; }
  poll();
});

async function stopBot(close){
  if(close && !confirm('Stop the bot, cancel open orders and close the position at market?')) return;
  $('stop').disabled = $('stopClose').disabled = true;
  await fetch('/api/stop', {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({close})});
  poll();
}
$('stop').addEventListener('click', () => stopBot(false));
$('stopClose').addEventListener('click', () => stopBot(true));

function paint(lines){
  const tape = $('tape');
  const nearEnd = tape.scrollHeight - tape.scrollTop - tape.clientHeight < 40;
  tape.innerHTML = lines.map(l => {
    const t = esc(l);
    let c = '';
    if(l.startsWith('[app]')) c = 'app';
    else if(/!!|Error|FAILED|rejected/i.test(l)) c = 'bad';
    else if(/>>>|CONFIRMED|BUY SIGNAL|EXIT/.test(l)) c = 'hl';
    else if(/^\s+/.test(l)) c = 'dim';
    return c ? `<span class="${c}">${t}</span>` : t;
  }).join('\n') || '<span class="dim">The log appears here once the bot starts.</span>';
  if(nearEnd) tape.scrollTop = tape.scrollHeight;
}

async function poll(){
  try{
    const j = await (await fetch('/api/status')).json();
    running = j.running;
    $('chip').classList.toggle('running', running);
    $('stateText').textContent = running ? 'Running' : 'Not running';
    $('kvSym').textContent = j.symbol ? `${j.symbol} ${j.timeframe || ''}` : '-';
    $('kvPx').textContent = j.price != null ? j.price.toLocaleString() : '-';
    $('kvPos').textContent = j.symbol ? (j.position > 0 ? `Long ${j.position}` : j.position < 0 ? `Short ${-j.position}` : 'Flat') : '-';
    $('start').disabled = running;
    $('stop').disabled = $('stopClose').disabled = !running;
    paint(j.logs);
  }catch(e){ /* server briefly busy */ }
}

syncSections();
loadSymbols();
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"Open {url} in your browser (it should open by itself). Press Ctrl+C here to quit.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)