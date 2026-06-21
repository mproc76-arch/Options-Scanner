# ============================================================
#  OPTIONS SCANNER v2 — scanner.py
#  Enhanced: Watchlist UI, StockTwits, Fundamentals, Pros/Cons
#  Analysis, Live Portfolio, Futures, yfinance Options Fallback,
#  TradingView Charts, Optional AI (Anthropic API)
# ============================================================

import sys, time, threading, webbrowser, schedule, requests, os, json, uuid, random
import concurrent.futures
from datetime import datetime, date
from flask import Flask, jsonify, render_template_string, request
import pandas as pd
import yfinance as yf
import config

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

scan_results           = {"leaps": [], "puts": [], "alerts": [], "last_scan": None, "status": "Starting..."}
tasty_token            = None
tasty_headers          = {}
tasty_token_expires_at = 0
TASTY_BASE             = "https://api.tastytrade.com"
TASTY_AUTH             = "https://api.tastytrade.com/oauth/token"
POSITIONS_FILE         = "positions.json"
WATCHLIST_FILE         = "watchlist.json"
_analysis_cache        = {}   # {ticker+type: (timestamp, result)}
_st_cache              = {}   # StockTwits cache {ticker: (timestamp, result)}
_fund_cache            = {}   # Fundamentals cache
NEWS_CACHE             = {}   # {ticker: (timestamp, articles)}

# ── Watchlist management ──────────────────────────────────
def load_watchlist():
    """Load watchlist from JSON file; fall back to config.py on first run."""
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    wl = list(config.WATCHLIST)
    save_watchlist(wl)
    return wl

def save_watchlist(tickers):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(set(t.upper() for t in tickers)), f, indent=2)

@app.route("/watchlist", methods=["GET"])
def api_get_watchlist():
    return jsonify({"tickers": load_watchlist()})

@app.route("/watchlist", methods=["POST"])
def api_add_ticker():
    ticker = ((request.json or {}).get("ticker", "")).upper().strip()
    if not ticker:
        return jsonify({"ok": False, "error": "No ticker"})
    valid = False
    try:
        info = yf.Ticker(ticker).info
        valid = bool(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("navPrice"))
    except Exception:
        pass
    if not valid:
        return jsonify({"ok": False, "error": f"'{ticker}' not found"})
    wl = load_watchlist()
    if ticker not in wl:
        wl.append(ticker)
        save_watchlist(wl)
    return jsonify({"ok": True, "tickers": load_watchlist()})

@app.route("/watchlist/<ticker>", methods=["DELETE"])
def api_remove_ticker(ticker):
    wl = [t for t in load_watchlist() if t.upper() != ticker.upper()]
    save_watchlist(wl)
    return jsonify({"ok": True, "tickers": load_watchlist()})

# ── OAuth / tastytrade ────────────────────────────────────
def login_tastytrade():
    global tasty_token, tasty_headers, tasty_token_expires_at
    refresh_token = getattr(config, "TASTYTRADE_REFRESH_TOKEN", "")
    client_secret = getattr(config, "TASTYTRADE_CLIENT_SECRET", "")
    scopes        = getattr(config, "TASTYTRADE_OAUTH_SCOPES", ["read"])
    if not refresh_token:
        print("[WARN] Missing TASTYTRADE_REFRESH_TOKEN in config.py")
        return False
    try:
        resp = requests.post(
            TASTY_AUTH,
            json={"refresh_token": refresh_token, "client_secret": client_secret,
                  "scope": " ".join(scopes), "grant_type": "refresh_token"},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15
        )
        data  = resp.json() if resp.content else {}
        token = data.get("access_token")
        if not token:
            print(f"[WARN] OAuth failed ({resp.status_code}): {data}")
            return False
        tasty_token            = token
        tasty_headers          = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        tasty_token_expires_at = time.time() + int(data.get("expires_in", 900)) - 30
        print("[OK] Tastytrade OAuth token received")
        return True
    except Exception as e:
        print(f"[WARN] OAuth error: {e}")
        return False

def refresh_token_loop():
    while True:
        time.sleep(30)
        if tasty_token and time.time() < tasty_token_expires_at:
            continue
        login_tastytrade()

def ensure_tastytrade_token():
    if tasty_token and time.time() < tasty_token_expires_at:
        return True
    return login_tastytrade()

# ── Options chain — tastytrade primary ───────────────────
def get_options_data_tasty(ticker, want_leaps=True, want_puts=True):
    results = []
    if not ensure_tastytrade_token():
        return results
    try:
        resp = requests.get(f"{TASTY_BASE}/option-chains/{ticker}/nested",
                            headers=tasty_headers, timeout=15)
        if resp.status_code != 200:
            return results
        today       = date.today()
        chain_items = resp.json().get("data", {}).get("items", [])
        expirations = []
        for item in chain_items:
            expirations.extend(item.get("expirations", [item]))
        print(f"  [INFO] {ticker}: {len(expirations)} expirations returned by Tastytrade")
        for exp_group in expirations:
            exp_str = exp_group.get("expiration-date", "")
            try:
                exp_date = date.fromisoformat(exp_str)
            except Exception:
                continue
            dte = (exp_date - today).days
            if dte < 1:
                continue
            for strike_group in exp_group.get("strikes", []):
                strike_price = float(strike_group.get("strike-price", 0))
                for side in ["call", "put"]:
                    opt = strike_group.get(side, {})
                    if not opt or isinstance(opt, str):
                        continue
                    try:
                        delta = float(opt.get("delta", 0) or 0)
                        bid   = float(opt.get("bid",   0) or 0)
                        ask   = float(opt.get("ask",   0) or 0)
                        iv    = float(opt.get("implied-volatility", 0) or 0) * 100
                        mid   = round((bid + ask) / 2, 2)
                        if want_leaps and side == "call":
                            if config.LEAPS_MIN_DTE <= dte <= config.LEAPS_MAX_DTE and config.LEAPS_MIN_DELTA <= delta <= config.LEAPS_MAX_DELTA:
                                results.append({"type": "LEAPS - Bounce Scalp", "strike": strike_price,
                                                "exp": exp_str, "dte": dte, "delta": round(delta, 2),
                                                "mid": mid, "iv": round(iv, 1), "source": "tasty"})
                        if want_puts and side == "put":
                            abs_d = abs(delta)
                            if config.PUT_MIN_DTE <= dte <= config.PUT_MAX_DTE and config.PUT_MIN_DELTA <= abs_d <= config.PUT_MAX_DELTA:
                                results.append({"type": "Naked put", "strike": strike_price,
                                                "exp": exp_str, "dte": dte, "delta": round(abs_d, 2),
                                                "mid": mid, "iv": round(iv, 1), "source": "tasty"})
                    except Exception:
                        continue
    except Exception as e:
        print(f"  [ERR] Tasty options chain {ticker}: {e}")
    return results

# ── Options chain — yfinance fallback ────────────────────
def get_options_data_yf(ticker, want_leaps=True, want_puts=True):
    """Used when tastytrade returns no contracts (after-hours or data gap)."""
    results = []
    try:
        tk    = yf.Ticker(ticker)
        today = date.today()
        hist  = _yf_history(ticker, period="2d", interval="1d")
        if hist.empty:
            return results
        price = float(hist["Close"].iloc[-1])
        if not price:
            return results

        with _YF_SEM:
            try:
                expirations = tk.options or []
            except Exception:
                expirations = []

        for exp_str in expirations:
            try:
                exp_date = date.fromisoformat(exp_str)
            except Exception:
                continue
            dte = (exp_date - today).days
            if dte < 1:
                continue
            chain = None
            with _YF_SEM:
                for attempt in range(2):
                    try:
                        chain = tk.option_chain(exp_str)
                        break
                    except Exception:
                        if attempt < 1:
                            time.sleep(1.0 + random.random())
            if chain is None:
                continue

            if want_leaps and config.LEAPS_MIN_DTE <= dte <= config.LEAPS_MAX_DTE:
                for _, row in chain.calls.iterrows():
                    try:
                        strike = float(row.get("strike", 0))
                        bid    = float(row.get("bid", 0) or 0)
                        ask    = float(row.get("ask", 0) or 0)
                        iv     = float(row.get("impliedVolatility", 0) or 0) * 100
                        if not (bid or ask):
                            continue
                        mid = round((bid + ask) / 2, 2)
                        # Approximate delta from moneyness
                        mono  = (price - strike) / (price * 0.01)
                        delta = max(0.05, min(0.95, 0.5 + mono * 0.05))
                        if config.LEAPS_MIN_DELTA <= delta <= config.LEAPS_MAX_DELTA:
                            results.append({"type": "LEAPS - Bounce Scalp", "strike": strike,
                                            "exp": exp_str, "dte": dte, "delta": round(delta, 2),
                                            "mid": mid, "iv": round(iv, 1), "source": "yf"})
                    except Exception:
                        continue

            if want_puts and config.PUT_MIN_DTE <= dte <= config.PUT_MAX_DTE:
                for _, row in chain.puts.iterrows():
                    try:
                        strike  = float(row.get("strike", 0))
                        bid     = float(row.get("bid", 0) or 0)
                        ask     = float(row.get("ask", 0) or 0)
                        iv      = float(row.get("impliedVolatility", 0) or 0) * 100
                        if not (bid or ask):
                            continue
                        mid      = round((bid + ask) / 2, 2)
                        otm_pct  = max(0, (price - strike) / price)
                        abs_delta = max(0.05, min(0.49, 0.5 - otm_pct * 3))
                        if config.PUT_MIN_DELTA <= abs_delta <= config.PUT_MAX_DELTA:
                            results.append({"type": "Naked put", "strike": strike,
                                            "exp": exp_str, "dte": dte, "delta": round(abs_delta, 2),
                                            "mid": mid, "iv": round(iv, 1), "source": "yf"})
                    except Exception:
                        continue
    except Exception as e:
        print(f"  [WARN] yf options fallback {ticker}: {e}")
    return results

def get_options_data(ticker, want_leaps=True, want_puts=True):
    """Priority: tastytrade → yfinance fallback."""
    results = get_options_data_tasty(ticker, want_leaps, want_puts)
    if results:
        return results
    # Fall back to yfinance
    print(f"  [INFO] {ticker}: falling back to yfinance options")
    return get_options_data_yf(ticker, want_leaps, want_puts)

@app.route("/options/<ticker>")
def api_options(ticker):
    opts = get_options_data(ticker.upper())
    return jsonify({"options": opts, "count": len(opts)})

# ── Technical indicators ──────────────────────────────────
# Yahoo rate-limits bursty concurrent requests from one IP. The scan fires up to
# 12 tickers at once via ThreadPoolExecutor, so yfinance calls go through this
# semaphore (capped well below 12) with retry/backoff to avoid silently skipping
# tickers when a request gets throttled.
_YF_SEM = threading.Semaphore(4)

def _yf_history(ticker, retries=3, **kwargs):
    with _YF_SEM:
        for attempt in range(retries):
            try:
                hist = yf.Ticker(ticker).history(**kwargs)
                if not hist.empty:
                    return hist
            except Exception:
                if attempt == retries - 1:
                    raise
            time.sleep(1.5 * (attempt + 1) + random.random())
    return pd.DataFrame()


def _compute_indicators_from_hist(hist):
    try:
        if hist.empty or len(hist) < 50:
            return None
        close  = hist["Close"]
        volume = hist["Volume"]
        price  = close.iloc[-1]
        ma20   = close.rolling(20).mean()
        ma50   = close.rolling(50).mean()
        ma200  = close.rolling(200).mean() if len(close) >= 200 else None

        d    = close.diff()
        gain = d.clip(lower=0).rolling(14).mean()
        loss = (-d.clip(upper=0)).rolling(14).mean()
        rs   = gain / loss.replace(0, float("nan"))
        rsi  = float((100 - 100 / (1 + rs)).iloc[-1])
        rsi_s = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))

        close_v  = close.values
        rsi_v    = rsi_s.values
        lookback = min(len(close_v) - 2, 60)
        sw_prices, sw_rsis = [], []
        for _i in range(len(close_v) - lookback, len(close_v) - 1):
            if _i > 0 and close_v[_i] < close_v[_i - 1] and close_v[_i] < close_v[_i + 1]:
                sw_prices.append(close_v[_i])
                sw_rsis.append(rsi_v[_i])
        bull_div = False
        if len(sw_prices) >= 3:
            for _j in range(len(sw_prices) - 1):
                if sw_prices[-1] < sw_prices[_j] and sw_rsis[-1] > sw_rsis[_j]:
                    bull_div = True
                    break

        bear_div     = (close.iloc[-1] > close.iloc[-10]) and (rsi_s.iloc[-1] < rsi_s.iloc[-10])
        rsi_cross_50 = any(rsi_s.iloc[-_i] > 50 and rsi_s.iloc[-_i - 1] <= 50 for _i in range(1, min(4, len(rsi_s))))
        rsi_was_oversold_5 = any(float(rsi_s.iloc[-_i]) < 35 for _i in range(1, min(6, len(rsi_s))))
        rsi_curling_up = (rsi_was_oversold_5 and len(rsi_s) >= 3 and
                          float(rsi_s.iloc[-1]) > float(rsi_s.iloc[-2]) and
                          float(rsi_s.iloc[-1]) > float(rsi_s.iloc[-3]))

        ema12     = close.ewm(span=12).mean()
        ema26     = close.ewm(span=26).mean()
        macd_line = ema12 - ema26
        sig_line  = macd_line.ewm(span=9).mean()
        hist_     = macd_line - sig_line
        macd_xup  = (macd_line.iloc[-1] > sig_line.iloc[-1]) and (macd_line.iloc[-2] <= sig_line.iloc[-2])

        bb_std = close.rolling(20).std()
        bb_mid = close.rolling(20).mean()
        bb_up  = bb_mid + 2 * bb_std
        bb_lo  = bb_mid - 2 * bb_std
        bb_rng = bb_up.iloc[-1] - bb_lo.iloc[-1]
        bb_pct = float((price - bb_lo.iloc[-1]) / bb_rng) if bb_rng else 0.5
        bb_sq  = float(bb_std.iloc[-1]) < float(bb_std.rolling(20).mean().iloc[-1]) * 0.85

        avg_vol   = float(volume.rolling(20).mean().iloc[-1])
        vol_ratio = float(volume.iloc[-1]) / avg_vol if avg_vol else 1.0
        gc = any(ma20.iloc[-_i] > ma50.iloc[-_i] and ma20.iloc[-_i - 1] <= ma50.iloc[-_i - 1] for _i in range(1, min(6, len(ma20))))
        dc = any(ma20.iloc[-_i] < ma50.iloc[-_i] and ma20.iloc[-_i - 1] >= ma50.iloc[-_i - 1] for _i in range(1, min(6, len(ma20))))

        hi52 = float(close.rolling(252, min_periods=1).max().iloc[-1]) if len(close) >= 252 else float(close.max())
        lo52 = float(close.rolling(252, min_periods=1).min().iloc[-1]) if len(close) >= 252 else float(close.min())
        pfl  = (price - lo52) / (hi52 - lo52) if (hi52 - lo52) else 0.5

        return {
            "price": round(float(price), 2), "ma20": round(float(ma20.iloc[-1]), 2),
            "ma50": round(float(ma50.iloc[-1]), 2),
            "ma200": round(float(ma200.iloc[-1]), 2) if ma200 is not None else None,
            "rsi": round(rsi, 1), "bull_div": bool(bull_div), "bear_div": bool(bear_div),
            "rsi_cross_50": bool(rsi_cross_50), "rsi_curling_up": bool(rsi_curling_up),
            "macd_cross_up": bool(macd_xup), "macd_above_signal": float(macd_line.iloc[-1]) > float(sig_line.iloc[-1]),
            "hist_growing": bool(hist_.iloc[-1] > hist_.iloc[-2]), "bb_pct": round(bb_pct, 2),
            "bb_squeeze": bool(bb_sq), "vol_ratio": round(vol_ratio, 2),
            "golden_cross": bool(gc), "death_cross": bool(dc), "pct_from_lo": round(pfl, 2),
            "hi52": round(hi52, 2), "lo52": round(lo52, 2),
        }
    except Exception as e:
        print(f"  [ERR] Indicators: {e}")
        return None

def compute_indicators(ticker):
    try:
        hist = _yf_history(ticker, period="1y", interval="1d")
        return _compute_indicators_from_hist(hist)
    except Exception as e:
        print(f"  [ERR] Daily indicators {ticker}: {e}")
        return None

def compute_indicators_1h(ticker):
    try:
        hist = _yf_history(ticker, period="60d", interval="1h")
        return _compute_indicators_from_hist(hist)
    except Exception as e:
        print(f"  [WARN] 1H indicators {ticker}: {e}")
        return None

def get_iv_rank_yf(ticker):
    """Fallback when Tastytrade has no market-metrics for this symbol (e.g. futures
    continuous contracts). Uses a realized-volatility percentile (current 20D HV vs.
    its own trailing 1Y range) as an IV-rank proxy — yfinance's 'impliedVolatility'
    info field is no longer populated by Yahoo, so it can't be used directly."""
    try:
        hist = _yf_history(ticker, period="1y", interval="1d")
        close = hist["Close"]
        roll_hv = (close.pct_change().rolling(20).std() * (252 ** 0.5) * 100).dropna()
        if len(roll_hv) < 20:
            return {"iv": 0, "iv_rank": 0}
        current = float(roll_hv.iloc[-1])
        rank    = float((roll_hv < current).mean() * 100)
        return {"iv": round(current, 1), "iv_rank": round(rank, 1)}
    except Exception:
        return {"iv": 0, "iv_rank": 0}

def get_market_metrics(symbols):
    """Real implied-volatility-rank data straight from Tastytrade's /market-metrics
    endpoint (batched, up to 50 symbols per request). Returns {symbol: {iv, iv_rank}}."""
    out = {}
    if not symbols or not ensure_tastytrade_token():
        return out
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        try:
            resp = requests.get(f"{TASTY_BASE}/market-metrics",
                                 params={"symbols": ",".join(chunk)},
                                 headers=tasty_headers, timeout=15)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("data", {}).get("items", []):
                sym  = item.get("symbol")
                ivx  = item.get("implied-volatility-index")
                rank = item.get("tw-implied-volatility-index-rank") or item.get("implied-volatility-index-rank")
                if not sym or ivx is None or rank is None:
                    continue
                try:
                    out[sym] = {"iv": round(float(ivx) * 100, 1), "iv_rank": round(float(rank) * 100, 1)}
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            print(f"  [WARN] market-metrics: {e}")
    return out

def get_iv_rank(ticker, metrics_cache=None):
    """Real IV rank from Tastytrade when available, falling back to the realized-vol
    proxy for symbols Tastytrade doesn't cover (e.g. yfinance futures tickers)."""
    if metrics_cache is None:
        metrics_cache = get_market_metrics([ticker])
    if ticker in metrics_cache:
        return metrics_cache[ticker]
    return get_iv_rank_yf(ticker)

def get_earnings_date(ticker):
    try:
        tk  = yf.Ticker(ticker)
        cal = tk.calendar
        if isinstance(cal, dict):
            ed  = cal.get("Earnings Date") or []
            val = ed[0] if isinstance(ed, (list, tuple)) else ed
            if val and hasattr(val, "date"):
                return val.date()
        if hasattr(cal, "empty") and not cal.empty and "Earnings Date" in cal.columns:
            val = cal["Earnings Date"].iloc[0]
            return val.date() if hasattr(val, "date") else None
    except Exception:
        pass
    return None

# ── Fundamentals ──────────────────────────────────────────
def get_fundamentals(ticker):
    now = time.time()
    if ticker in _fund_cache and now - _fund_cache[ticker][0] < 3600:
        return _fund_cache[ticker][1]
    try:
        info = yf.Ticker(ticker).info
        result = {
            "market_cap":      info.get("marketCap"),
            "pe_ratio":        info.get("trailingPE"),
            "forward_pe":      info.get("forwardPE"),
            "revenue_growth":  info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "profit_margin":   info.get("profitMargins"),
            "debt_equity":     info.get("debtToEquity"),
            "analyst_target":  info.get("targetMeanPrice"),
            "analyst_low":     info.get("targetLowPrice"),
            "analyst_high":    info.get("targetHighPrice"),
            "recommendation":  info.get("recommendationKey"),
            "num_analysts":    info.get("numberOfAnalystOpinions"),
            "short_percent":   info.get("shortPercentOfFloat"),
            "beta":            info.get("beta"),
            "sector":          info.get("sector"),
            "industry":        info.get("industry"),
            "52w_high":        info.get("fiftyTwoWeekHigh"),
            "52w_low":         info.get("fiftyTwoWeekLow"),
            "current_price":   info.get("currentPrice") or info.get("regularMarketPrice"),
            "company_name":    info.get("longName") or info.get("shortName", ticker),
            "description":     (info.get("longBusinessSummary") or "")[:400],
        }
        _fund_cache[ticker] = (now, result)
        return result
    except Exception as e:
        print(f"  [WARN] Fundamentals {ticker}: {e}")
        return {}

@app.route("/fundamentals/<ticker>")
def api_fundamentals(ticker):
    return jsonify(get_fundamentals(ticker.upper()))

# ── StockTwits sentiment ──────────────────────────────────
def get_stocktwits_sentiment(ticker):
    now = time.time()
    if ticker in _st_cache and now - _st_cache[ticker][0] < 300:
        return _st_cache[ticker][1]
    try:
        url  = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        messages = resp.json().get("messages", [])
        bull, bear = 0, 0
        samples = []
        for m in messages:
            sent = (m.get("entities", {}).get("sentiment") or {}).get("basic", "")
            if sent == "Bullish":
                bull += 1
            elif sent == "Bearish":
                bear += 1
            if len(samples) < 5 and m.get("body"):
                samples.append({"text": m["body"][:140], "sentiment": sent.lower(), "user": m.get("user", {}).get("username", "")})
        total = bull + bear
        result = {
            "bull": bull, "bear": bear, "total": len(messages),
            "bull_pct": round(bull / total * 100) if total else 50,
            "samples": samples,
        }
        _st_cache[ticker] = (now, result)
        return result
    except Exception as e:
        print(f"  [WARN] StockTwits {ticker}: {e}")
        return None

@app.route("/stocktwits/<ticker>")
def api_stocktwits(ticker):
    data = get_stocktwits_sentiment(ticker.upper())
    return jsonify(data or {"error": "unavailable", "bull_pct": 50, "total": 0})

# ── Enhanced news ─────────────────────────────────────────
def get_news_enhanced(ticker):
    now = time.time()
    if ticker in NEWS_CACHE and now - NEWS_CACHE[ticker][0] < 600:
        return NEWS_CACHE[ticker][1]
    try:
        tk    = yf.Ticker(ticker)
        items = tk.news or []
        news_list = []
        bull_kw = ["beat", "surge", "jump", "rally", "upgrade", "buy", "growth", "record", "bullish",
                   "raise", "raised", "strong", "exceed", "profit", "win", "positive", "up", "expands",
                   "breakthrough", "launch", "partner", "deal", "contract", "award"]
        bear_kw = ["miss", "drop", "fall", "cut", "downgrade", "sell", "loss", "warning", "bearish",
                   "decline", "weak", "below", "concern", "risk", "probe", "lawsuit", "fine",
                   "layoff", "restructure", "guidance", "withdraw", "halt"]
        for n in items[:12]:
            content  = n.get("content") or {}
            title    = n.get("title") or content.get("title", "")
            if not title:
                continue
            summary  = content.get("summary", "") or ""
            pub_time = n.get("providerPublishTime", 0)
            if pub_time:
                dt       = datetime.fromtimestamp(pub_time)
                time_str = dt.strftime("%b %d, %I:%M %p")
            else:
                pub_date = content.get("pubDate", "")
                try:
                    clean    = pub_date.split(".")[0].rstrip("Z")
                    dt       = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
                    time_str = dt.strftime("%b %d, %I:%M %p")
                except Exception:
                    time_str = "—"
            combined = (title + " " + summary).lower()
            bull_score = sum(1 for w in bull_kw if w in combined)
            bear_score = sum(1 for w in bear_kw if w in combined)
            if bull_score > bear_score:
                sentiment = "bull"
            elif bear_score > bull_score:
                sentiment = "bear"
            else:
                sentiment = "neutral"
            source = (content.get("provider") or n.get("publisher") or {})
            if isinstance(source, dict):
                source = source.get("displayName", "")
            news_list.append({
                "title": title,
                "summary": summary[:200] if summary else "",
                "time": time_str,
                "sentiment": sentiment,
                "bull_score": bull_score,
                "bear_score": bear_score,
                "source": source,
                "url": (content.get("canonicalUrl") or {}).get("url", "") if isinstance(content.get("canonicalUrl"), dict) else "",
            })
        NEWS_CACHE[ticker] = (now, news_list)
        return news_list
    except Exception as e:
        return []

@app.route("/news")
def news():
    ticker = request.args.get("ticker", "AAPL").upper()
    return jsonify({"news": get_news_enhanced(ticker)})

# ── Scoring (unchanged logic) ─────────────────────────────
def score_ticker(ticker, ind, ivdata, for_leaps=False, ind_1h=None):
    signals, score, max_pts = [], 0, 0
    def add(pts, earned, label):
        nonlocal score, max_pts
        max_pts += pts
        if earned: score += pts
        signals.append({"label": label, "bullish": earned})
    def add_mtf(pts_leaps, pts_puts, daily_flag, hourly_flag, label):
        nonlocal score, max_pts
        if not for_leaps:
            add(pts_puts, daily_flag, label)
            return
        if ind_1h is None:
            add(pts_leaps, daily_flag, label)
            return
        bonus = round(pts_leaps * 0.5)
        max_pts += pts_leaps + bonus
        if daily_flag and hourly_flag:
            score += pts_leaps + bonus
            signals.append({"label": f"{label} (1D + 1H)", "bullish": True})
        elif daily_flag:
            score += pts_leaps
            signals.append({"label": f"{label} (1D)", "bullish": True})
        elif hourly_flag:
            score += pts_leaps
            signals.append({"label": f"{label} (1H)", "bullish": True})
        else:
            signals.append({"label": label, "bullish": False})
    p, rsi, ivr = ind["price"], ind["rsi"], ivdata["iv_rank"]
    h = ind_1h or {}
    add(8 if not for_leaps else 4,  p > ind["ma20"],              "Above 20 MA")
    add(10 if not for_leaps else 4, p > ind["ma50"],              "Above 50 MA")
    if ind["ma200"]: add(12 if not for_leaps else 5, p > ind["ma200"], "Above 200 MA")
    add(8,  ind["ma20"] > ind["ma50"],    "20 MA above 50 MA")
    add_mtf(10, 10, ind["golden_cross"], h.get("golden_cross", False), "Golden cross just formed")
    add(10, not ind["death_cross"],       "No death cross")
    add_mtf(20, 12, ind["bull_div"], h.get("bull_div", False), "RSI bullish divergence — HIGH PRIORITY")
    add(8,  not ind["bear_div"],          "No RSI bearish divergence")
    add(8,  30 < rsi < 70,               "RSI in healthy range")
    add(10, rsi > 50,                    "RSI above 50")
    add(6,  rsi < 35,                    "RSI oversold — bounce zone")
    add_mtf(18, 6, ind["rsi_curling_up"], h.get("rsi_curling_up", False), "RSI oversold and curling up")
    add(8,  ind["rsi_cross_50"],         "RSI crossed above 50 — momentum shift")
    add_mtf(10, 10, ind["macd_cross_up"], h.get("macd_cross_up", False), "MACD bullish crossover")
    add(6,  ind["macd_above_signal"],    "MACD above signal line")
    add(6,  ind["hist_growing"],         "MACD histogram expanding")
    add_mtf(20, 8, ind["bb_pct"] < 0.25, h.get("bb_pct", 1) < 0.25, "Near lower Bollinger band")
    add(6,  ind["bb_squeeze"],           "Bollinger squeeze — big move pending")
    add(6,  ind["vol_ratio"] >= 1.5,    "Above-average volume")
    add(8,  ind["pct_from_lo"] < 0.35,  "Lower third of 52-week range")
    add(6,  ind["pct_from_lo"] > 0.65,  "Upper range — momentum")
    add(10, ivr >= config.PUT_MIN_IV_RANK,   "IV rank elevated — good for puts")
    add(8 if not for_leaps else 12, ivr <= config.LEAPS_MAX_IV_RANK, "IV moderate — OK to buy LEAPS")
    if for_leaps:
        add(15, ivr < 30, "IV rank low — cheap premium for LEAPS")
    return round(score / max_pts * 100) if max_pts else 0, signals

def best_contract(opts, trade_type):
    f = [o for o in opts if o["type"] == trade_type]
    if not f: return None
    if trade_type == "LEAPS - Bounce Scalp":
        return min(f, key=lambda o: abs(o["delta"] - 0.60))
    return min(f, key=lambda o: abs(o["delta"] - 0.28) + abs(o["dte"] - 45) * 0.01)

def support_cushion_info(strike, ind):
    candidates = []
    for name, price in [("200 MA", ind.get("ma200")), ("50 MA", ind["ma50"]), ("20 MA", ind["ma20"])]:
        if price and float(price) < float(strike):
            candidates.append((name, round(float(price), 2)))
    if not candidates:
        return None
    nearest    = max(candidates, key=lambda x: x[1])
    dollar_gap = round(float(strike) - nearest[1], 2)
    pct_gap    = round(dollar_gap / nearest[1] * 100, 1)
    return {"level": nearest[0], "price": nearest[1], "dollar": dollar_gap, "pct": pct_gap}

# ── Detailed trade analysis (pros / cons) ────────────────
def build_trade_analysis(ticker, trade_type, ind, ivdata, fund, st_data, opts, news_items):
    price    = ind.get("price", 0)
    rsi      = ind.get("rsi", 50)
    is_leaps = "LEAPS" in trade_type
    ivr      = ivdata.get("iv_rank", 0)
    pros, cons = [], []

    # --- Technical ---
    if ind.get("rsi_curling_up"):
        pros.append({"cat": "Technical", "text": f"RSI was oversold and is now curling up from below 35 — classic reversal signal."})
    if ind.get("bull_div"):
        pros.append({"cat": "Technical", "text": "Bullish RSI divergence: price making lower lows while RSI makes higher lows — early sign sellers are losing control."})
    if ind.get("macd_cross_up"):
        pros.append({"cat": "Technical", "text": "MACD just crossed above signal line — momentum is shifting bullish."})
    if ind.get("bb_pct", 1) < 0.2:
        pros.append({"cat": "Technical", "text": f"Price near lower Bollinger Band (BB%: {ind['bb_pct']}) — historically a high-probability bounce zone."})
    if ind.get("golden_cross"):
        pros.append({"cat": "Technical", "text": "Golden cross recently formed (20MA crossed above 50MA) — medium-term trend turning bullish."})
    if ind.get("pct_from_lo", 1) < 0.3:
        pros.append({"cat": "Technical", "text": f"Stock in lower 30% of its 52-week range (${ind.get('lo52','?')} - ${ind.get('hi52','?')}) — mean reversion potential."})
    if rsi < 35:
        pros.append({"cat": "Technical", "text": f"RSI at {rsi} — oversold territory, historically favorable entry for bounces."})
    if ind.get("rsi_cross_50"):
        pros.append({"cat": "Technical", "text": "RSI just crossed above 50 — momentum confirming bullish shift."})
    if ind.get("bb_squeeze"):
        pros.append({"cat": "Technical", "text": "Bollinger squeeze active — compressed volatility often precedes a big directional move."})
    if ind.get("vol_ratio", 0) >= 1.5:
        pros.append({"cat": "Technical", "text": f"Volume {ind['vol_ratio']}x above 20-day average — above-average participation signals conviction."})

    if ind.get("death_cross"):
        cons.append({"cat": "Technical", "text": "Death cross formed recently (20MA crossed below 50MA) — bearish trend signal. Countertrend entries carry more risk."})
    if ind.get("bear_div"):
        cons.append({"cat": "Technical", "text": "Bearish RSI divergence: price rising but RSI falling — momentum weakening, watch for a pullback."})
    if rsi > 70:
        cons.append({"cat": "Technical", "text": f"RSI at {rsi} — overbought. Consider waiting for a pullback to a better entry."})
    if ind.get("pct_from_lo", 0) > 0.8:
        cons.append({"cat": "Technical", "text": "Near 52-week highs — limited overhead room. Higher-risk entry point."})
    if ind.get("bb_pct", 0) > 0.85:
        cons.append({"cat": "Technical", "text": "Price near upper Bollinger Band — stretched. Better entries exist after a consolidation."})
    if price and ind.get("ma200") and price < ind["ma200"]:
        cons.append({"cat": "Technical", "text": f"Price below 200 MA (${ind['ma200']}) — technically in a downtrend. LEAPS carry more risk in downtrends."})

    # --- Fundamental ---
    rev_growth = fund.get("revenue_growth")
    if rev_growth and rev_growth > 0.15:
        pros.append({"cat": "Fundamental", "text": f"Revenue growing at {round(rev_growth*100)}% YoY — strong top-line momentum."})
    elif rev_growth and rev_growth > 0:
        pros.append({"cat": "Fundamental", "text": f"Revenue up {round(rev_growth*100)}% YoY — modest but positive growth trajectory."})

    rec = fund.get("recommendation", "")
    n_analysts = fund.get("num_analysts") or 0
    if rec in ["buy", "strongBuy"]:
        pros.append({"cat": "Fundamental", "text": f"Analyst consensus is '{rec}' across {n_analysts} analysts — street is constructive."})

    target = fund.get("analyst_target")
    if target and price:
        upside = round((target - price) / price * 100)
        if upside > 20:
            pros.append({"cat": "Fundamental", "text": f"Analyst avg price target: ${target} — implies {upside}% upside from ${price}."})
        elif upside > 5:
            pros.append({"cat": "Fundamental", "text": f"Analyst avg target ${target} implies {upside}% upside — moderate street support."})

    short_pct = fund.get("short_percent")
    if short_pct and short_pct > 0.12:
        pros.append({"cat": "Fundamental", "text": f"Short interest at {round(short_pct*100,1)}% of float — significant short squeeze potential on positive catalysts."})

    earn_growth = fund.get("earnings_growth")
    if earn_growth and earn_growth > 0.20:
        pros.append({"cat": "Fundamental", "text": f"Earnings growing {round(earn_growth*100)}% YoY — accelerating profitability supports higher valuation."})

    de = fund.get("debt_equity")
    if de and de > 2.0:
        cons.append({"cat": "Fundamental", "text": f"Debt/equity ratio of {round(de,1)} is elevated. If rates stay high, financing costs are a headwind."})
    if rev_growth and rev_growth < 0:
        cons.append({"cat": "Fundamental", "text": f"Revenue declining {round(abs(rev_growth)*100)}% YoY — fundamental weakness that needs to reverse for thesis to work."})
    if rec in ["sell", "strongSell"]:
        cons.append({"cat": "Fundamental", "text": f"Analyst consensus is '{rec}' — the street is not supportive of this setup."})
    pe = fund.get("pe_ratio")
    if pe and pe > 60 and is_leaps:
        cons.append({"cat": "Fundamental", "text": f"P/E of {round(pe,1)} is elevated — expensive valuation already prices in significant growth. Less margin of safety."})
    if target and price and (target - price) / price < -0.05:
        cons.append({"cat": "Fundamental", "text": f"Analyst avg target (${target}) is BELOW current price (${price}) — street thinks it's overvalued."})

    # --- Options Setup ---
    if is_leaps:
        if ivr < 25:
            pros.append({"cat": "Options", "text": f"IV Rank at {ivr} — premiums are historically cheap. Good time to be a buyer of LEAPS."})
        elif ivr > 50:
            cons.append({"cat": "Options", "text": f"IV Rank at {ivr} — premiums are elevated. You'll overpay for LEAPS; consider waiting for IV to cool."})
        else:
            pros.append({"cat": "Options", "text": f"IV Rank at {ivr} — moderate premium environment. Reasonable entry cost for LEAPS."})
        best = [o for o in opts if o.get("type") == "LEAPS - Bounce Scalp"]
        if best:
            b = min(best, key=lambda o: abs(o.get("delta", 0) - 0.60))
            cost = b.get("mid", 0)
            pros.append({"cat": "Options", "text": f"Best contract: ${b['strike']}C exp {b['exp']} | Delta {b['delta']} | Mid ${b['mid']} | DTE {b['dte']} | IV {b['iv']}% | Source: {b.get('source','?').upper()}"})
            if cost:
                be = round(b['strike'] + cost, 2)
                pros.append({"cat": "Options", "text": f"Breakeven at expiration: ${be} ({round((be-price)/price*100,1)}% move required from current ${price})"})
    else:
        if ivr >= 30:
            pros.append({"cat": "Options", "text": f"IV Rank at {ivr} — elevated IV means fat put premiums. Good environment to be a seller."})
        else:
            cons.append({"cat": "Options", "text": f"IV Rank at {ivr} — put premiums are thin. Risk/reward less favorable for naked put selling right now."})
        best_put = [o for o in opts if o.get("type") == "Naked put"]
        if best_put:
            b = min(best_put, key=lambda o: abs(o.get("delta", 0) - 0.28) + abs(o.get("dte", 45) - 45) * 0.01)
            pros.append({"cat": "Options", "text": f"Best put: ${b['strike']}P exp {b['exp']} | Delta {b['delta']} | Mid ${b['mid']} | DTE {b['dte']} | IV {b['iv']}% | Source: {b.get('source','?').upper()}"})
            cushion = support_cushion_info(b.get("strike"), ind)
            if cushion:
                cls = "strong" if cushion["pct"] >= 5 else "moderate" if cushion["pct"] >= 2 else "thin"
                cons_or_pro = pros if cushion["pct"] >= 3 else cons
                cons_or_pro.append({"cat": "Options", "text": f"Strike support: {cushion['level']} at ${cushion['price']} is ${cushion['dollar']} / {cushion['pct']}% below strike ({cls} cushion)."})

    # --- Sentiment ---
    if st_data and st_data.get("total", 0) >= 5:
        bull_pct = st_data.get("bull_pct", 50)
        total    = st_data.get("total", 0)
        if bull_pct >= 65:
            pros.append({"cat": "Sentiment", "text": f"StockTwits: {bull_pct}% bullish out of {total} recent messages — retail crowd aligned with bullish thesis."})
        elif bull_pct <= 35:
            cons.append({"cat": "Sentiment", "text": f"StockTwits: only {bull_pct}% bullish from {total} messages — retail sentiment negative. Contrarian buy signal possible but thesis needs confirmation."})
        else:
            pros.append({"cat": "Sentiment", "text": f"StockTwits: {bull_pct}% bullish ({total} messages) — neutral to mildly bullish retail sentiment."})

    # --- News sentiment summary ---
    if news_items:
        bull_news = [n for n in news_items if n.get("sentiment") == "bull"]
        bear_news = [n for n in news_items if n.get("sentiment") == "bear"]
        if len(bull_news) > len(bear_news) * 1.5:
            pros.append({"cat": "Sentiment", "text": f"News flow: {len(bull_news)} bullish vs {len(bear_news)} bearish headlines in recent coverage. Positive news environment."})
        elif len(bear_news) > len(bull_news) * 1.5:
            cons.append({"cat": "Sentiment", "text": f"News flow: {len(bear_news)} bearish vs {len(bull_news)} bullish headlines. Negative news environment — wait for clarity."})

    # --- Recommendation ---
    pro_count = len(pros)
    con_count = len(cons)
    if pro_count >= con_count * 2 and pro_count >= 4:
        rec       = "Strong Setup" if is_leaps else "Strong Put-Selling Opportunity"
        rec_color = "green"
        rec_detail = "Multiple confirming signals across technical, fundamental, and sentiment. Size appropriately and set your stops."
    elif pro_count > con_count:
        rec       = "Moderate Setup — Worth Considering"
        rec_color = "amber"
        rec_detail = "Decent signal mix but some headwinds. Start with a smaller position. Add if the thesis plays out over the next few days."
    elif con_count > pro_count:
        rec       = "Wait for Better Setup"
        rec_color = "red"
        rec_detail = "More headwinds than tailwinds right now. Put this on your watchlist and re-scan in 3-5 days. Patience over FOMO."
    else:
        rec       = "Mixed Signals — Proceed with Caution"
        rec_color = "amber"
        rec_detail = "Conflicting signals. Only enter if you have strong fundamental conviction. Keep position size small."

    # --- Risk / Reward ---
    rr = {}
    if is_leaps and opts:
        best_c = [o for o in opts if o.get("type") == "LEAPS - Bounce Scalp"]
        if best_c:
            b    = min(best_c, key=lambda o: abs(o.get("delta", 0) - 0.60))
            cost = b.get("mid", 0)
            if cost and price:
                rr = {
                    "max_loss":  f"${round(cost * 100):,} per contract (total premium paid — defined risk)",
                    "target_1x": f"${round(price*1.15, 2)} stock (+15%) → option est. ~${round(cost*1.8,2)}",
                    "target_2x": f"${round(price*1.25, 2)} stock (+25%) → option est. ~${round(cost*2.5,2)}",
                    "stop_rule": f"Exit if stock drops below ${round(price*0.91,2)} (-9%) OR option loses 40-50% of premium",
                    "breakeven": f"${round(b['strike']+cost, 2)} at expiration (stock needs {round((b['strike']+cost-price)/price*100,1)}% move)",
                }
    elif not is_leaps and opts:
        best_p = [o for o in opts if o.get("type") == "Naked put"]
        if best_p:
            b      = min(best_p, key=lambda o: abs(o.get("delta",0)-0.28)+abs(o.get("dte",45)-45)*0.01)
            prem   = b.get("mid", 0)
            strike = b.get("strike", 0)
            if prem and strike:
                rr = {
                    "max_profit":   f"${round(prem*100):,} per contract (keep full premium if stock stays above ${strike})",
                    "max_loss":     f"${round((strike-prem)*100):,} per contract (assigned and stock goes to $0 — very unlikely)",
                    "breakeven":    f"${round(strike-prem,2)} at expiration",
                    "roll_trigger": f"Roll or exit if stock drops within 5% of ${strike} strike before expiration",
                    "profit_target": f"Close at 50% premium (${ round(prem*0.5,2)}) — standard income strategy exit",
                }
    return {
        "ticker": ticker, "trade_type": trade_type, "price": price,
        "pros": pros, "cons": cons,
        "recommendation": rec, "rec_color": rec_color, "rec_detail": rec_detail,
        "risk_reward": rr,
        "fundamentals": {
            "company":         fund.get("company_name", ticker),
            "sector":          fund.get("sector", "—"),
            "industry":        fund.get("industry", "—"),
            "pe":              fund.get("pe_ratio"),
            "forward_pe":      fund.get("forward_pe"),
            "revenue_growth":  fund.get("revenue_growth"),
            "earn_growth":     fund.get("earnings_growth"),
            "analyst_target":  fund.get("analyst_target"),
            "analyst_low":     fund.get("analyst_low"),
            "analyst_high":    fund.get("analyst_high"),
            "recommendation":  fund.get("recommendation"),
            "num_analysts":    fund.get("num_analysts"),
            "short_percent":   fund.get("short_percent"),
            "beta":            fund.get("beta"),
            "description":     fund.get("description", ""),
        },
    }

@app.route("/analysis/<ticker>")
def api_analysis(ticker):
    ticker     = ticker.upper()
    trade_type = request.args.get("type", "LEAPS - Bounce Scalp")
    cache_key  = f"{ticker}_{trade_type}"
    now        = time.time()
    if cache_key in _analysis_cache and now - _analysis_cache[cache_key][0] < 300:
        return jsonify(_analysis_cache[cache_key][1])

    ind = compute_indicators(ticker)
    if not ind:
        return jsonify({"error": "Could not compute indicators for this ticker"})
    ind_1h     = compute_indicators_1h(ticker)
    ivdata     = get_iv_rank(ticker)
    fund       = get_fundamentals(ticker)
    st_data    = get_stocktwits_sentiment(ticker)
    opts       = get_options_data(ticker)
    news_items = get_news_enhanced(ticker)

    result = build_trade_analysis(ticker, trade_type, ind, ivdata, fund, st_data, opts, news_items)

    # Optional: AI enhancement via Anthropic
    anthropic_key = getattr(config, "ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            import anthropic as ant
            client   = ant.Anthropic(api_key=anthropic_key)
            pros_txt = "\n".join(f"- {p['text']}" for p in result["pros"][:6])
            cons_txt = "\n".join(f"- {c['text']}" for c in result["cons"][:4])
            prompt   = f"""You are a professional options trader. Be direct, specific, and concise (4-5 sentences max).

Ticker: {ticker} | Trade: {trade_type} | Price: ${ind['price']} | RSI: {ind['rsi']} | IV Rank: {ivdata['iv_rank']}
Sector: {fund.get('sector','?')} | Analyst rec: {fund.get('recommendation','?')} | Target: ${fund.get('analyst_target','?')}

Bullish signals:
{pros_txt}

Risk factors:
{cons_txt}

Give your professional take: Is this a good entry right now? What's the #1 risk? What would make you more or less confident in this trade?"""
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                messages=[{"role": "user", "content": prompt}]
            )
            result["ai_take"] = resp.content[0].text
        except Exception as e:
            print(f"[WARN] AI analysis error: {e}")
            result["ai_take"] = None
    else:
        result["ai_take"] = None

    _analysis_cache[cache_key] = (now, result)
    return jsonify(result)

# ── Live portfolio from tastytrade ────────────────────────
@app.route("/portfolio")
def api_portfolio():
    if not ensure_tastytrade_token():
        return jsonify({"error": "Not authenticated with Tastytrade. Check your token in config.py."})
    accounts_cfg = getattr(config, "TASTYTRADE_ACCOUNTS", {})
    if not accounts_cfg:
        return jsonify({"error": "No accounts configured. Add TASTYTRADE_ACCOUNTS to config.py."})
    result = {}
    for label, acct_num in accounts_cfg.items():
        try:
            pos_resp = requests.get(f"{TASTY_BASE}/accounts/{acct_num}/positions",
                                    headers=tasty_headers, timeout=15)
            bal_resp = requests.get(f"{TASTY_BASE}/accounts/{acct_num}/balances",
                                    headers=tasty_headers, timeout=15)
            positions = []
            if pos_resp.status_code == 200:
                for item in pos_resp.json().get("data", {}).get("items", []):
                    positions.append({
                        "symbol":       item.get("symbol"),
                        "type":         item.get("instrument-type"),
                        "qty":          item.get("quantity"),
                        "direction":    item.get("quantity-direction"),
                        "close_price":  item.get("close-price"),
                        "avg_price":    item.get("average-open-price"),
                        "multiplier":   item.get("multiplier", 1),
                        "days_held":    item.get("days-held"),
                    })
            balance = {}
            if bal_resp.status_code == 200:
                bd = bal_resp.json().get("data", {})
                balance = {
                    "net_liq":       bd.get("net-liquidating-value"),
                    "cash":          bd.get("cash-balance"),
                    "buying_power":  bd.get("derivative-buying-power"),
                }
            result[label] = {"account": acct_num, "positions": positions, "balance": balance}
        except Exception as e:
            result[label] = {"error": str(e)}
    return jsonify(result)

# ── Futures scanner ───────────────────────────────────────
@app.route("/futures")
def api_futures():
    symbols = getattr(config, "FUTURES_SYMBOLS", {})
    results = []
    for label, yf_sym in symbols.items():
        try:
            ind    = compute_indicators(yf_sym)
            if not ind: continue
            ivdata = get_iv_rank_yf(yf_sym)
            score, signals = score_ticker(yf_sym, ind, ivdata, for_leaps=False)
            bull_sigs = [s["label"] for s in signals if s["bullish"]][:5]
            bias = "Bullish" if score >= 60 else "Bearish" if score < 40 else "Neutral"
            results.append({
                "symbol":     label,
                "yf_sym":     yf_sym,
                "price":      ind["price"],
                "rsi":        ind["rsi"],
                "score":      score,
                "bias":       bias,
                "macd_cross": ind.get("macd_cross_up", False),
                "bull_div":   ind.get("bull_div", False),
                "bb_pct":     ind.get("bb_pct"),
                "vol_ratio":  ind.get("vol_ratio"),
                "ma50":       ind.get("ma50"),
                "ma200":      ind.get("ma200"),
                "pct_from_lo": ind.get("pct_from_lo"),
                "signals":    bull_sigs,
            })
        except Exception as e:
            print(f"[WARN] Futures {label}: {e}")
    results.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"futures": results, "scanned_at": datetime.now().strftime("%I:%M %p")})

# ── Quote route ───────────────────────────────────────────
@app.route("/quote")
def quote():
    ticker = request.args.get("ticker", "SPY")
    try:
        tk        = yf.Ticker(ticker)
        hist_long = tk.history(period="1y", interval="1d")
        above_50ma = rsi_val = None

        hist = tk.history(period="5d", interval="1d")
        if hist.empty:
            return jsonify({"price": 0, "chg": 0, "chg_pct": 0})
        price = round(float(hist["Close"].iloc[-1]), 2)
        prev  = round(float(hist["Close"].iloc[-2]), 2) if len(hist) > 1 else price

        chg     = round(price - prev, 2)
        chg_pct = round((chg / prev) * 100, 2) if prev else 0

        if not hist_long.empty and len(hist_long) >= 50:
            ma50       = hist_long["Close"].rolling(50).mean().iloc[-1]
            above_50ma = bool(price > ma50)
            d          = hist_long["Close"].diff()
            gain       = d.clip(lower=0).rolling(14).mean()
            loss       = (-d.clip(upper=0)).rolling(14).mean()
            rs         = gain / loss.replace(0, float("nan"))
            rsi_val    = round(float((100 - 100 / (1 + rs)).iloc[-1]), 1)

        return jsonify({"price": price, "chg": chg, "chg_pct": chg_pct,
                        "above_50ma": above_50ma, "rsi": rsi_val,
                        "source": "yfinance"})
    except Exception as e:
        return jsonify({"error": str(e), "price": 0, "chg": 0, "chg_pct": 0})

# ── Positions log ─────────────────────────────────────────
def load_positions():
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_positions(positions):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2)

@app.route("/positions")
def get_positions():
    enriched = []
    for p in load_positions():
        cur = None
        try:
            h   = yf.Ticker(p["ticker"]).history(period="1d", interval="1d")
            cur = round(float(h["Close"].iloc[-1]), 2) if not h.empty else None
        except Exception:
            pass
        pos = dict(p)
        pos["current_underlying"] = cur
        eu = float(p.get("entry_underlying") or 0)
        if cur and eu:
            pos["underlying_chg_pct"] = round((cur - eu) / eu * 100, 1)
        enriched.append(pos)
    return jsonify({"positions": enriched})

@app.route("/log_position", methods=["POST"])
def log_position():
    data = request.json or {}
    eu   = None
    try:
        h  = yf.Ticker(data.get("ticker", "")).history(period="1d", interval="1d")
        eu = round(float(h["Close"].iloc[-1]), 2) if not h.empty else None
    except Exception:
        pass
    pos = {
        "id":               str(uuid.uuid4())[:8],
        "ticker":           data.get("ticker"),
        "type":             data.get("type"),
        "strike":           data.get("strike"),
        "expiry":           data.get("expiry"),
        "entry_price":      data.get("entry_price"),
        "entry_underlying": eu,
        "contracts":        int(data.get("contracts", 1)),
        "logged_at":        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "account":          data.get("account", ""),
        "notes":            data.get("notes", ""),
    }
    positions = load_positions()
    positions.append(pos)
    save_positions(positions)
    return jsonify({"ok": True, "id": pos["id"]})

@app.route("/log_position/<pos_id>", methods=["DELETE"])
def delete_position(pos_id):
    save_positions([p for p in load_positions() if p.get("id") != pos_id])
    return jsonify({"ok": True})

# ── Main scan ─────────────────────────────────────────────
def scan_ticker(ticker, metrics_cache):
    """Runs all per-ticker work for one symbol. Returns (leaps_item, puts_item, alerts)
    so the caller can collect results without sharing mutable state across threads."""
    print(f"  → {ticker}")
    alerts = []
    leaps_item = puts_item = None

    ind = compute_indicators(ticker)
    if not ind:
        return leaps_item, puts_item, alerts
    ind_1h = compute_indicators_1h(ticker)
    ivdata = get_iv_rank(ticker, metrics_cache)

    leaps_score, leaps_signals = score_ticker(ticker, ind, ivdata, for_leaps=True, ind_1h=ind_1h)
    puts_score,  puts_signals  = score_ticker(ticker, ind, ivdata, for_leaps=False)
    leaps_bull = [s["label"] for s in leaps_signals if s["bullish"]]
    leaps_bear = [s["label"] for s in leaps_signals if not s["bullish"]]
    puts_bull  = [s["label"] for s in puts_signals  if s["bullish"]]
    puts_bear  = [s["label"] for s in puts_signals  if not s["bullish"]]

    if ind["rsi"] > 75:    alerts.append({"ticker": ticker, "msg": f"RSI overbought at {ind['rsi']} — review LEAPS exits"})
    if ind["bear_div"]:    alerts.append({"ticker": ticker, "msg": "Bearish RSI divergence — caution on longs"})
    if ind["death_cross"]: alerts.append({"ticker": ticker, "msg": "Death cross — avoid new LEAPS entries"})

    base = {"ticker": ticker, "price": ind["price"], "rsi": ind["rsi"],
            "iv_rank": ivdata["iv_rank"], "ma20": ind["ma20"], "ma50": ind["ma50"],
            "ma200": ind["ma200"], "vol_ratio": ind["vol_ratio"],
            "hi52": ind.get("hi52"), "lo52": ind.get("lo52")}

    opts           = get_options_data(ticker)
    leaps_contract = best_contract(opts, "LEAPS - Bounce Scalp")
    put_contract   = best_contract(opts, "Naked put")
    blank_leaps    = {"type": "LEAPS - Bounce Scalp", "strike": "—", "exp": "—", "dte": "—", "delta": "—", "mid": "—", "source": "—"}
    blank_put      = {"type": "Naked put",            "strike": "—", "exp": "—", "dte": "—", "delta": "—", "mid": "—", "source": "—"}

    ls = leaps_score - (10 if ind["rsi"] > 70 else 0) - (15 if ivdata["iv_rank"] > config.LEAPS_MAX_IV_RANK else 0)
    if ls >= config.MIN_SCORE:
        lc               = leaps_contract or blank_leaps
        earnings_dt      = get_earnings_date(ticker)
        earnings_warning = None
        if earnings_dt and isinstance(lc.get("dte"), int):
            days_to_earn = (earnings_dt - date.today()).days
            if 0 < days_to_earn <= lc["dte"]:
                earnings_warning = earnings_dt.strftime("%b %d")
        leaps_item = {**base, "score": ls, "contract": lc,
                      "trade_type": "LEAPS - Bounce Scalp",
                      "bull_signals": leaps_bull, "bear_signals": leaps_bear,
                      "earnings_warning": earnings_warning}

    ps = puts_score - (20 if ivdata["iv_rank"] < config.PUT_MIN_IV_RANK else 0) - (15 if ind["death_cross"] else 0) - (10 if ind["bear_div"] else 0)
    if ps >= config.MIN_SCORE:
        pc      = put_contract or blank_put
        cushion = support_cushion_info(pc.get("strike"), ind) if isinstance(pc.get("strike"), (int, float)) else None
        puts_item = {**base, "score": ps, "contract": pc,
                     "trade_type": "Naked put",
                     "bull_signals": puts_bull, "bear_signals": puts_bear,
                     "support_cushion": cushion}

    return leaps_item, puts_item, alerts

def run_scan():
    global scan_results
    scan_results["status"] = "Scanning..."
    watchlist = load_watchlist()
    print(f"\n[SCAN] {datetime.now().strftime('%I:%M %p')} — {len(watchlist)} tickers")
    leaps_list, puts_list, alerts = [], [], []
    metrics_cache = get_market_metrics(watchlist)
    print(f"  [INFO] Real IV rank from Tastytrade for {len(metrics_cache)}/{len(watchlist)} symbols")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(scan_ticker, ticker, metrics_cache): ticker for ticker in watchlist}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                leaps_item, puts_item, ticker_alerts = future.result()
            except Exception as e:
                print(f"  [ERR] {ticker}: {e}")
                continue
            if leaps_item: leaps_list.append(leaps_item)
            if puts_item:  puts_list.append(puts_item)
            alerts.extend(ticker_alerts)

    # Position alerts
    price_map = {item["ticker"]: item["price"] for item in leaps_list + puts_list}
    for p in load_positions():
        tk_p = p.get("ticker", "")
        cur  = price_map.get(tk_p)
        if cur is None:
            try:
                h   = yf.Ticker(tk_p).history(period="1d", interval="1d")
                cur = round(float(h["Close"].iloc[-1]), 2) if not h.empty else None
            except Exception:
                pass
        if cur is None: continue
        eu  = float(p.get("entry_underlying") or 0)
        st  = p.get("strike")
        typ = p.get("type", "")
        if "LEAPS" in typ and eu:
            ratio = cur / eu
            if ratio >= 1.5:
                alerts.append({"ticker": tk_p, "msg": f"LEAPS: stock up {round((ratio-1)*100)}% from entry — may be 3x on option, consider scaling out"})
            elif ratio >= 1.3:
                alerts.append({"ticker": tk_p, "msg": f"LEAPS: stock up {round((ratio-1)*100)}% from entry — may be 2x on option, review position"})
        if "put" in typ.lower() and st:
            pct = round((cur - float(st)) / float(st) * 100, 1)
            if pct < 4:
                alerts.append({"ticker": tk_p, "msg": f"Put ${st} strike: stock ${cur} only {pct}% above — urgent: roll or exit"})
            elif pct < 8:
                alerts.append({"ticker": tk_p, "msg": f"Put ${st} strike: stock ${cur} is {pct}% above — consider rolling"})

    leaps_list.sort(key=lambda x: x["score"], reverse=True)
    puts_list.sort(key=lambda x:  x["score"], reverse=True)
    scan_results = {"leaps": leaps_list, "puts": puts_list, "alerts": alerts,
                    "last_scan": datetime.now().strftime("%I:%M %p"), "status": "Ready",
                    "watchlist_count": len(watchlist)}
    print(f"[DONE] {len(leaps_list)} LEAPS | {len(puts_list)} puts | {len(alerts)} alerts")

# ── Flask root ────────────────────────────────────────────
@app.route("/")
def index():
    dashboard_path = os.path.join(SCRIPT_DIR, "dashboard.html")
    return render_template_string(open(dashboard_path, encoding="utf-8").read()
                                  if os.path.exists(dashboard_path) else "<h2>dashboard.html missing</h2>")

@app.route("/data")
def data():
    return jsonify(scan_results)

@app.route("/rescan", methods=["POST"])
def rescan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"ok": True})

def schedule_loop():
    schedule.every(config.REFRESH_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  OPTIONS SCANNER v2")
    print("=" * 55)
    ai_key = getattr(config, "ANTHROPIC_API_KEY", "")
    print(f"[INFO] AI Analysis: {'Enabled (Anthropic)' if ai_key else 'Disabled — add ANTHROPIC_API_KEY to config.py'}")
    if login_tastytrade():
        print("[OK] Tastytrade authenticated")
    threading.Thread(target=refresh_token_loop, daemon=True).start()
    print("[INFO] Running first scan...")
    threading.Thread(target=run_scan, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()
    print(f"[INFO] Dashboard → http://localhost:{config.PORT}")
    webbrowser.open(f"http://localhost:{config.PORT}", new=2)
    app.run(port=config.PORT, debug=False, use_reloader=False)
