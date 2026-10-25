# Algo Crew

Algo Crew is a Python-based algorithmic trading toolkit for Delta Exchange demo
accounts. It includes a browser-based Flask dashboard for configuring and
running demo trading strategies, a reusable command-line trading bot, and
historical backtesting utilities with chart generation.

> **Important:** This project is for experimentation and demo trading only.
> It is not financial advice, and it does not guarantee profit. Never use
> real funds until the code, exchange settings, order behavior, and risk
> controls have been independently reviewed and tested.

## Abstract

Algo Crew provides a lightweight, configurable environment for evaluating
rule-based crypto trading strategies against Delta Exchange market data. The
system downloads candle and ticker data, calculates technical indicators,
generates entry and exit signals, and optionally places market and bracket
orders on a Delta demo account.

The project is intentionally small and self-contained:

- `app.py` serves the web UI and runs the demo trading engine.
- `ema_bot.py` contains the reusable indicator and strategy implementation
  with a command-line interface.
- `backtest.py` downloads historical candles, simulates trades, and reports
  performance.
- `backtest_plots.py` produces price, trade, equity, drawdown, and P&L charts.

The live/demo engine uses closed candles for candle-based strategies, polls
market data on a configurable interval, tracks the current position, and
supports stop-loss and optional take-profit behavior. API credentials entered
through the dashboard are kept in process memory and sent to the selected
Delta Exchange environment for authenticated requests.

## Features

- Flask web dashboard with:
  - Demo India and Demo Global environment selection
  - Live product/symbol loading from Delta Exchange
  - Timeframe selection: `1m`, `5m`, `15m`, and `1h`
  - Position size, stop-loss, target, and RSI configuration
  - Start, stop, and stop-and-close controls
  - Live status, price, position, and activity log display
- Four configurable demo strategies
- HMAC-SHA256 authenticated Delta Exchange requests
- Market order execution with optional bracket stop-loss/take-profit orders
- Historical backtesting with configurable assumptions for:
  - Trading fees
  - Slippage
  - Indicator warm-up
- Matplotlib chart generation for:
  - Price and trade markers
  - EMA overlays
  - Strategy equity versus buy-and-hold
  - Drawdown
  - P&L per trade
- No credentials committed in the source code

## Trading strategies

### Strategy 1: EMA crossover with confirmations

Uses a fast and slow exponential moving average crossover as the primary
signal. Optional confirmations include:

- Session VWAP
- Price action and breakout behavior
- RSI position relative to configurable oversold, midline, and overbought
  levels

The number of confirmations required can be configured in the web UI.

### Strategy 2: RSI and MACD momentum

Combines:

- RSI relative to an RSI moving average
- Minimum RSI and MACD thresholds
- MACD crossover behavior
- Configurable exit after the MACD histogram shrinks for a selected number of
  candles

A fixed take-profit can be enabled or disabled.

### Strategy 3: Breakout with volume

Detects breaks above the previous lookback high or below the previous
lookback low. The strategy can optionally require:

- Volume above a configurable multiple of average volume
- A confirming engulfing candle

### Strategy 4: Price levels

Places a long or short entry when the selected market reaches configured buy
or sell levels. Each configured level fires once during a run. The strategy
continues monitoring the position and applies the configured stop-loss and
optional target.

## Project structure

```text
.
├── app.py                # Flask dashboard and demo trading engine
├── ema_bot.py            # CLI bot, indicators, and strategies
├── backtest.py           # Historical data and trade simulation
├── backtest_plots.py     # Backtest visualization helpers
├── requirements.txt      # Python dependencies
└── .gitignore            # Local secrets, caches, environments, and charts
```

## Requirements

- Python 3.9 or newer recommended
- Internet access for Delta Exchange market data
- A Delta Exchange **demo** account
- A demo API key with the required trading permissions

Install the dependencies:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS/Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run the web dashboard

Start the Flask application:

```bash
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000) if the browser does not
open automatically.

In the dashboard:

1. Select the correct Delta demo environment.
2. Enter the demo API key and API secret.
3. Select a market symbol and timeframe.
4. Choose a strategy and review its parameters.
5. Set position size, stop-loss, and target values.
6. Start the bot and monitor the status and activity log.
7. Use **Stop** to stop signal processing, or **Stop and close** to cancel
   orders and close the open position at market.

The application binds to `127.0.0.1` by default so the dashboard is local to
the machine running it.

## Run the command-line bot

The standalone bot reads API credentials from environment variables:

Windows PowerShell:

```powershell
$env:DELTA_API_KEY = "your_demo_api_key"
$env:DELTA_API_SECRET = "your_demo_api_secret"
python ema_bot.py
```

macOS/Linux:

```bash
export DELTA_API_KEY="your_demo_api_key"
export DELTA_API_SECRET="your_demo_api_secret"
python ema_bot.py
```

Review the `MODE` and strategy configuration in `ema_bot.py` before running.
The default mode is `demo`.

## Run a backtest

The backtest utility downloads public historical candle data and caches it
locally for up to six hours:

```bash
python backtest.py
```

The backtest models estimated fees and slippage. These are assumptions, not a
guarantee of actual exchange execution costs. Review the values at the top of
`backtest.py` and confirm them against your Delta Exchange account and fee
tier.

To generate visualizations, use the plotting helpers through the workflow
defined in `backtest.py`/`backtest_plots.py`. Generated chart images are
ignored by Git.

## Data and order behavior

- Market data is retrieved from Delta Exchange public endpoints.
- Authenticated requests use the configured API key and secret.
- Candle strategies remove the still-forming candle before evaluating signals.
- Stop-loss and target calculations are based on the configured reference
  price and tick size.
- The dashboard keeps a bounded in-memory activity log.
- The bot does not provide portfolio management, guaranteed execution,
  liquidation protection, or production-grade fault tolerance.

## Security and operational notes

- Use demo credentials only while evaluating the project.
- Never commit API keys, API secrets, `.env` files, or private key material.
- Create exchange keys with the minimum permissions required.
- Do not expose the local Flask server to the public internet without adding
  authentication, HTTPS, request protection, and a production deployment
  architecture.
- Test order cancellation and position-closing behavior before relying on the
  system.
- Monitor the exchange account directly; a stopped process cannot manage an
  existing position.

## Vercel

- Official Vercel website: [vercel.com](https://vercel.com/)
- Official Vercel documentation: [vercel.com/docs](https://vercel.com/docs)

This repository currently contains a local Flask application and does not
include a Vercel deployment URL or Vercel configuration. A public production
deployment should not be created without adding authentication, secret
management, durable process handling, and protections around trading actions.
Once a deployment is created, its URL can be added here as:

```text
Live deployment: https://<your-project>.vercel.app
```

## License

No license file has been added yet. Until a license is published, all rights
remain with the copyright holder.
