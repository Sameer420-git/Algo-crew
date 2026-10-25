import os, time, hmac, hashlib, json, requests

# ================= MODE: "demo" or "live" =================
MODE = "demo"
URLS = {"demo": "https://cdn-ind.testnet.deltaex.org",
        "live": "https://api.india.delta.exchange"}
BASE_URL = URLS[MODE]

# Use env variables, or paste keys between the quotes (demo keys for demo, live keys for live)
API_KEY    = os.environ.get("DELTA_API_KEY")    or "PASTE_API_KEY"
API_SECRET = os.environ.get("DELTA_API_SECRET") or "PASTE_API_SECRET"

# ================= COMMON SETTINGS =================
SL_PCT, TP_PCT   = 2.0, 4.0
CHECK_EVERY      = 10
TIMEFRAMES       = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
MAX_SLIPPAGE_PCT = 1.0
CANDLE_COUNT     = 300
RSI_PERIOD       = 14

# ================= STRATEGY 1 SETTINGS =================
FAST, SLOW = 9, 20
USE_VWAP, USE_PA, USE_RSI = True, True, True
MIN_CONFIRMATIONS = 2
RSI_OB, RSI_OS, RSI_MID = 70, 30, 50
BREAKOUT_N, PA_LOOKBACK = 20, 3

# ================= STRATEGY 2 SETTINGS =================
RSI_MA_PERIOD = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MACD_MIN = 0.0              # MACD must be above this value (0 = original behaviour)
S2_RSI_MIN = 0.0            # RSI must be at least this value (0 = no extra filter)
EXIT_SHRINK_CANDLES = 1     # histogram must shrink this many candles in a row to exit
S2_USE_TP = False           # True = also attach the 4% take-profit

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

def get_tradeable_products():
    r = requests.get(f"{BASE_URL}/v2/products",
                     params={"contract_types": "perpetual_futures", "page_size": 100},
                     timeout=10).json()
    return sorted([p for p in r["result"] if p.get("state") == "live"],
                  key=lambda p: p["symbol"])

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
    print(msg)
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
    print("    Entry order:", r)
    if not r.get("success"):
        return False
    time.sleep(2)
    pos, entry = get_position(pid)
    if pos == 0 or entry == 0:
        print("    Could not read position. Check the exchange manually!")
        return False
    close_side = "sell" if pos > 0 else "buy"
    slip = abs(entry - mark_before) / mark_before * 100
    print(f"    Mark={mark_before}  Fill={entry}  Slippage={slip:.2f}%")
    if slip > MAX_SLIPPAGE_PCT:
        print("    !! Slippage too high - closing position")
        print("    ", place_market(pid, close_side, abs(pos)))
        return False
    res = place_sl_tp(pid, symbol, get_mark_price(symbol), pos > 0, tick, use_tp)
    print("    SL/TP response:", res)
    if not res.get("success"):
        print("    !! SL/TP FAILED - closing position for safety")
        print("    ", place_market(pid, close_side, abs(pos)))
        return False
    return True

# ================= STRATEGY 1: EMA 9/20 + confirmations =================
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
    print(f"    Filters [{detail}] -> {passed}/{needed} required")
    if not ok:
        print("    >> Signal SKIPPED (not enough confirmation)")
        return
    print(f"    >> CONFIRMED - going {direction.upper()}")
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
    print(f"[{time.strftime('%H:%M:%S')}] {ctx['symbol']} Close={closes[-1]:.2f} | "
          f"EMA{FAST}={f_now:.2f} | EMA{SLOW}={s_now:.2f} | Pos={position:+d}")
    print(s1_describe(info))
    if state["last"] is None:
        state["last"] = times[-1]
        print("    Ignoring existing signal, waiting for the next fresh crossover...")
    elif times[-1] != state["last"]:
        up = f_prev <= s_prev and f_now > s_now
        down = f_prev >= s_prev and f_now < s_now
        if up and position <= 0:
            print(">>> BULLISH CROSS detected")
            s1_signal("long", info, ctx, position)
        elif down and position >= 0:
            print(">>> BEARISH CROSS detected")
            s1_signal("short", info, ctx, position)
        state["last"] = times[-1]

# ================= STRATEGY 2: RSI > RSI-MA9 + MACD buy, MACD > MACD_MIN =================
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
        print(f"[{time.strftime('%H:%M:%S')}] Not enough candles yet for RSI/MACD...")
        return
    ok = lambda b: "OK" if b else "--"
    print(f"[{time.strftime('%H:%M:%S')}] {ctx['symbol']} Close={closes[-1]:.2f} | Pos={position:+d}")
    print(f"    RSI={cur['rsi']:.1f} vs MA{RSI_MA_PERIOD}={cur['rsi_ma']:.1f} (min {S2_RSI_MIN:g}) [{ok(cur['rsi_ok'])}] | "
          f"MACD={cur['macd']:.2f} Sig={cur['signal']:.2f} [{ok(cur['macd_buy'])}] | "
          f"MACD>{MACD_MIN:g} [{ok(cur['macd_pos'])}] | Hist={cur['hist']:.2f}")

    if position == 0:
        state["entry_time"] = None          # flat (e.g. stop-loss hit), reset

    # EXIT: judged only on candles that closed after the entry candle
    if position > 0 and (state["entry_time"] is None or times[-1] > state["entry_time"]):
        exit_now, why = s2_exit_signal(hist, macd, sig)
        if exit_now:
            print(f">>> EXIT: {why}")
            cancel_all_orders(ctx["pid"])
            print("    ", place_market(ctx["pid"], "sell", abs(position)))
            state["entry_time"] = None
            return

    # ENTRY: long only, fresh setup on a newly closed candle
    if state["last"] is None:
        state["last"] = times[-1]
        print("    Ignoring existing setup, waiting for a fresh buy signal...")
    elif times[-1] != state["last"]:
        state["last"] = times[-1]
        if position == 0 and s2_all(cur) and not s2_all(prev):
            print(">>> BUY SIGNAL: RSI > RSI-MA, MACD buy signal, MACD above minimum")
            if open_trade(ctx, "buy", use_tp=S2_USE_TP):
                state["entry_time"] = times[-1]

# ================= User prompts =================
STRATEGIES = {
    "1": "EMA 9/20 crossover + confirmations (VWAP, price action, RSI)",
    "2": "RSI above its 9-MA + MACD buy signal + MACD above minimum (long only)",
}

def choose_strategy():
    print("\n=== SELECT STRATEGY ===")
    print(f"  1 -> {STRATEGIES['1']}")
    print(f"       BUY : EMA9 crosses above EMA20, needs {MIN_CONFIRMATIONS} of 3 confirmations")
    print(f"       SELL: EMA9 crosses below EMA20, needs {MIN_CONFIRMATIONS} of 3 confirmations")
    print(f"       Risk: SL {SL_PCT}% / TP {TP_PCT}%  (long & short)")
    print(f"  2 -> {STRATEGIES['2']}")
    print(f"       BUY : RSI({RSI_PERIOD}) > its {RSI_MA_PERIOD}-period MA (and >= {S2_RSI_MIN:g})  AND  MACD above signal  AND  MACD > {MACD_MIN:g}")
    print("       EXIT: MACD histogram shrinks from its high / MACD crosses below signal")
    if S2_USE_TP:
        print(f"       Risk: SL {SL_PCT}% / TP {TP_PCT}%")
    else:
        print(f"       Risk: SL {SL_PCT}%, exit decided by MACD")
    while True:
        s = input("\nEnter 1 or 2: ").strip()
        if s in STRATEGIES:
            return s
        print("Please type only 1 or 2.")

def choose_inputs():
    products = get_tradeable_products()
    print("\nAvailable perpetual contracts:")
    for i, p in enumerate(products, 1):
        print(f"  {i:>2}. {p['symbol']}")
    while True:
        pick = input("\nEnter number or symbol (e.g. 1 or BTCUSD): ").strip().upper()
        if pick.isdigit() and 1 <= int(pick) <= len(products):
            prod = products[int(pick) - 1]; break
        match = [p for p in products if p["symbol"] == pick]
        if match:
            prod = match[0]; break
        print("Invalid choice, try again.")
    tf = ""
    while tf not in TIMEFRAMES:
        tf = input(f"Timeframe {list(TIMEFRAMES)}: ").strip().lower()
    size = 0
    while size < 1:
        s = input("Size in contracts (e.g. 1): ").strip()
        size = int(s) if s.isdigit() else 0
    return prod, tf, size

def main():
    print(f"\n######## MODE: {MODE.upper()}  ({BASE_URL}) ########")
    if MODE == "live":
        print("!!! REAL MONEY. Orders will be placed on your LIVE account. !!!")
        if input("Type LIVE to continue: ").strip() != "LIVE":
            raise SystemExit("Cancelled.")
    strategy = choose_strategy()
    prod, tf, size = choose_inputs()
    print(f"\n>> Strategy {strategy} | {prod['symbol']} | {tf} | size {size} | {MODE.upper()}")
    if input("Start bot? (y/n): ").strip().lower() != "y":
        raise SystemExit("Cancelled.")

    ctx = {"pid": prod["id"], "symbol": prod["symbol"], "size": size,
           "tick": float(prod.get("tick_size", 0.5))}
    state = {"last": None, "entry_time": None}
    tick_fn = tick_s1 if strategy == "1" else tick_s2
    tf_sec = TIMEFRAMES[tf]
    print("\nBot running... press Ctrl+C to stop.\n")

    while True:
        try:
            candles = get_candles(ctx["symbol"], tf, tf_sec)[:-1]    # closed candles only
            position, _ = get_position(ctx["pid"])
            tick_fn(candles, position, state, ctx)
        except KeyboardInterrupt:
            print("\nStopped. Open positions/orders (if any) remain on the exchange.")
            break
        except Exception as e:
            print("Error:", e)
        time.sleep(CHECK_EVERY)

if __name__ == "__main__":
    main()