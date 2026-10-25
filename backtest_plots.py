import matplotlib.pyplot as plt
from datetime import datetime, timezone
from ema_bot import ema_full, FAST, SLOW

def dt(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)

def equity_points(trades, start_time):
    xs, ys, eq = [dt(start_time)], [100.0], 100.0
    for t in trades:
        eq *= 1 + t["pnl"] / 100
        xs.append(dt(t["exit_time"])); ys.append(eq)
    return xs, ys

def plot_strategy(name, trades, candles, symbol, tf, days, tag, zoom_days, warmup, show_emas):
    times  = [dt(c["time"]) for c in candles]
    closes = [c["close"] for c in candles]
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(15, 11),
                                        gridspec_kw={"height_ratios": [3, 2, 1.5]})

    # ---------- Panel 1: price + trades (zoomed to the last N days) ----------
    zoom_start = candles[-1]["time"] - zoom_days * 86400
    idx = [i for i, c in enumerate(candles) if c["time"] >= zoom_start]
    if idx:
        a, b = idx[0], idx[-1] + 1
        ax1.plot(times[a:b], closes[a:b], color="#555555", linewidth=1, label="Close")
        if show_emas:
            ax1.plot(times[a:b], ema_full(closes, FAST)[a:b], color="orange", linewidth=1.2, label=f"EMA {FAST}")
            ax1.plot(times[a:b], ema_full(closes, SLOW)[a:b], color="green",  linewidth=1.2, label=f"EMA {SLOW}")
        shown = [t for t in trades if t["exit_time"] >= zoom_start]
        first = {"L": True, "S": True, "W": True, "X": True}
        for t in shown:
            if t["entry_time"] >= zoom_start:
                is_long = t["side"] == "LONG"
                ax1.scatter(dt(t["entry_time"]), t["entry"], marker="^" if is_long else "v",
                            color="green" if is_long else "red", s=90, zorder=5,
                            label=("Long entry" if is_long else "Short entry") if first["L" if is_long else "S"] else None)
                first["L" if is_long else "S"] = False
            win = t["pnl"] > 0
            ax1.scatter(dt(t["exit_time"]), t["exit"], marker="x", s=70, zorder=5,
                        color="green" if win else "red",
                        label=("Exit (win)" if win else "Exit (loss)") if first["W" if win else "X"] else None)
            first["W" if win else "X"] = False
            ax1.plot([dt(t["entry_time"]), dt(t["exit_time"])], [t["entry"], t["exit"]],
                     color="green" if win else "red", linestyle="--", linewidth=0.8, alpha=0.7)
        ax1.set_title(f"{name} | {symbol} {tf} | last {zoom_days} days ({len(shown)} trades in view)")
    ax1.legend(loc="upper left", ncol=3, fontsize=8)
    ax1.grid(alpha=0.3); ax1.set_ylabel("Price")

    # ---------- Panel 2: equity curve vs buy & hold (full period) ----------
    ax2.plot(times[warmup:], [c / closes[warmup] * 100 for c in closes[warmup:]],
             color="#aaaaaa", linewidth=1, label="Buy & hold")
    if trades:
        xs, ys = equity_points(trades, candles[warmup]["time"])
        ax2.step(xs, ys, where="post", color="blue", linewidth=1.8, label="Strategy equity")
        peak, dd_x, dd_y = ys[0], [], []
        for x, y in zip(xs, ys):
            peak = max(peak, y); dd_x.append(x); dd_y.append(peak)
        ax2.fill_between(dd_x, ys, dd_y, step="post", color="red", alpha=0.15, label="Drawdown")
    ax2.axhline(100, color="black", linewidth=0.8)
    ax2.set_title(f"Equity (start = 100) over {days} days")
    ax2.legend(loc="upper left", fontsize=8); ax2.grid(alpha=0.3); ax2.set_ylabel("Equity")

    # ---------- Panel 3: P&L of every trade ----------
    if trades:
        ax3.vlines([dt(t["exit_time"]) for t in trades], 0, [t["pnl"] for t in trades],
                   colors=["green" if t["pnl"] > 0 else "red" for t in trades], linewidth=1.5)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        ax3.set_title(f"P&L per trade (%) | {len(trades)} trades | win rate {wins / len(trades) * 100:.0f}%")
    ax3.axhline(0, color="black", linewidth=0.8)
    ax3.grid(alpha=0.3); ax3.set_ylabel("P&L %")

    fig.tight_layout()
    fname = f"backtest_{symbol}_{tf}_{tag}_chart.png"
    fig.savefig(fname, dpi=130)
    print(f"Saved chart -> {fname}")

def plot_all(symbol, tf, days, candles, t1, t2, zoom_days=7, warmup=100):
    plot_strategy("Strategy 1: EMA 9/20 + filters", t1, candles, symbol, tf, days,
                  "strategy1", zoom_days, warmup, show_emas=True)
    plot_strategy("Strategy 2: RSI>MA9 + MACD", t2, candles, symbol, tf, days,
                  "strategy2", zoom_days, warmup, show_emas=False)

    # comparison chart
    plt.figure(figsize=(13, 5))
    times = [dt(c["time"]) for c in candles]
    plt.plot(times[warmup:], [c["close"] / candles[warmup]["close"] * 100 for c in candles[warmup:]],
             color="#aaaaaa", label="Buy & hold")
    for trades, label, col in ((t1, "Strategy 1", "blue"), (t2, "Strategy 2", "darkorange")):
        if trades:
            xs, ys = equity_points(trades, candles[warmup]["time"])
            plt.step(xs, ys, where="post", color=col, linewidth=1.8, label=label)
    plt.axhline(100, color="black", linewidth=0.8)
    plt.title(f"Strategy comparison | {symbol} {tf} | {days} days (start = 100)")
    plt.legend(); plt.grid(alpha=0.3); plt.ylabel("Equity")
    plt.tight_layout()
    fname = f"backtest_{symbol}_{tf}_comparison.png"
    plt.savefig(fname, dpi=130)
    print(f"Saved chart -> {fname}")
    plt.show()          # opens the chart windows; close them to end the script