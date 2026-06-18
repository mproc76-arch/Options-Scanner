# Options Scanner

A self-hosted Flask dashboard that scans a stock watchlist for options trade
setups using technical indicators (from Yahoo Finance via `yfinance`) and
live option chain data (from the Tastytrade API).

It looks for two kinds of setups:

- **LEAPS — Bounce Scalp**: long-dated calls (6 months–2 years out, 0.50–0.70
  delta) bought on signs of a short-term bounce in an otherwise healthy stock.
- **Naked put**: cash-secured puts (21–60 DTE, 0.15–0.35 delta) sold when IV
  rank is elevated.

## How it works

1. Every `REFRESH_MINUTES` (configurable), the scanner loops over the
   watchlist in `config.py` and pulls price history for each ticker.
2. It computes technical indicators — RSI, RSI divergence, MACD, Bollinger
   Bands, moving averages, golden/death cross, 52-week range, volume ratio,
   IV rank — and scores each ticker against a weighted checklist of bullish
   signals for both setup types.
3. For the **LEAPS path only**, five of the bounce signals (RSI oversold &
   curling up, RSI bullish divergence, near lower Bollinger Band, MACD
   bullish crossover, golden cross) are checked independently on both daily
   (1D) and hourly (1H) candles. A signal is tagged with which timeframe(s)
   it fired on — `(1D)`, `(1H)`, or `(1D + 1H)` — and firing on both
   timeframes at once (confluence) earns a scoring bonus. If hourly data
   isn't available for a ticker, it falls back to 1D-only scoring for that
   ticker.
4. Tickers that clear `MIN_SCORE` get matched against the live options
   chain (via Tastytrade) to find the best-fitting contract for the
   dashboard card.
5. The dashboard also tracks logged positions, surfaces alerts (e.g.
   overbought RSI, death cross, earnings before expiration, big moves on
   open positions), and shows a per-ticker news feed.

## Running it

```
pip install flask yfinance schedule requests
python scanner.py
```

This opens `http://localhost:5000` in your browser automatically. On
Windows you can also just run `START_SCANNER.bat`.

## Configuration

All settings live in `config.py` (not committed to this repo — it holds
your Tastytrade API credentials). You'll need to create your own with:

- `TASTYTRADE_CLIENT_ID` / `TASTYTRADE_CLIENT_SECRET` / `TASTYTRADE_REFRESH_TOKEN`
- `WATCHLIST` — list of tickers to scan
- `MIN_SCORE`, `REFRESH_MINUTES`
- `LEAPS_MIN_DTE` / `LEAPS_MAX_DTE` / `LEAPS_MIN_DELTA` / `LEAPS_MAX_DELTA` / `LEAPS_MAX_IV_RANK`
- `PUT_MIN_DTE` / `PUT_MAX_DTE` / `PUT_MIN_DELTA` / `PUT_MAX_DELTA` / `PUT_MIN_IV_RANK`
- `PORT`

Logged positions are stored locally in `positions.json` (also not
committed).

## Known limitation

Contract pricing (strike/expiration/delta/bid-ask) on the dashboard cards
currently shows as blank. Tastytrade's nested option-chain endpoint only
returns option symbols, not greeks or quotes — those require a DXLink
streaming connection that isn't implemented yet. Scores and signals are
unaffected; only the suggested contract field is missing.

## Disclaimer

This is a personal scanning tool, not financial advice. It pulls
third-party market data (Yahoo Finance, Tastytrade) that may be delayed,
incomplete, or wrong — verify everything before trading on it.
