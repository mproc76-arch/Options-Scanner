# ============================================================
#  OPTIONS SCANNER — scanner.py
#  Uses OAuth2 refresh token — no device challenge
# ============================================================

import time, threading, webbrowser, schedule, requests, os, json, uuid
from datetime import datetime, date
from flask import Flask, jsonify, render_template_string, request
import yfinance as yf

import config

app = Flask(__name__)

scan_results           = {"leaps": [], "puts": [], "alerts": [], "last_scan": None, "status": "Starting..."}
tasty_token            = None
tasty_headers          = {}
tasty_token_expires_at = 0
TASTY_BASE             = "https://api.tastytrade.com"
TASTY_AUTH             = "https://api.tastytrade.com/oauth/token"
POSITIONS_FILE         = "positions.json"

# ── OAuth login ───────────────────────────────────────────
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
            print(f"[WARN] OAuth token request failed ({resp.status_code}): {data}")
            return False
        tasty_token            = token
        tasty_headers          = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        tasty_token_expires_at = time.time() + int(data.get("expires_in", 900)) - 30
        print("[OK] Received Tastytrade OAuth access token")
        return True
    except Exception as e:
        print(f"[WARN] OAuth token error: {e}")
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

def get_api_quote_token():
    if not ensure_tastytrade_token():
        return None
    try:
        resp = requests.get(f"{TASTY_BASE}/api-quote-tokens", headers=tasty_headers, timeout=15)
        data = resp.json()
        if resp.status_code != 200:
            print(f"[WARN] Quote token failed ({resp.status_code}): {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"[WARN] Quote token error: {e}")
        return None

# ── Options chain ─────────────────────────────────────────
def get_options_data(ticker, want_leaps=True, want_puts=True):
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
                                results.append({"type": "LEAPS - Bounce Scalp", "strike": strike_price, "exp": exp_str, "dte": dte, "delta": round(delta, 2), "mid": mid, "iv": round(iv, 1)})
                        if want_puts and side == "put":
                            abs_d = abs(delta)
                            if config.PUT_MIN_DTE <= dte <= config.PUT_MAX_DTE and config.PUT_MIN_DELTA <= abs_d <= config.PUT_MAX_DELTA:
                                results.append({"type": "Naked put", "strike": strike_price, "exp": exp_str, "dte": dte, "delta": round(abs_d, 2), "mid": mid, "iv": round(iv, 1)})
                    except Exception:
                        continue
    except Exception as e:
        print(f"  [ERR] Options chain for {ticker}: {e}")
    return results

# ── Technical indicators ──────────────────────────────────
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

        # Swing-low divergence: price lower low + RSI higher low, requires ≥3 swing points
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

        bear_div = (close.iloc[-1] > close.iloc[-10]) and (rsi_s.iloc[-1] < rsi_s.iloc[-10])

        # RSI crossed above 50 from below within last 3 bars
        rsi_cross_50 = any(
            rsi_s.iloc[-_i] > 50 and rsi_s.iloc[-_i - 1] <= 50
            for _i in range(1, min(4, len(rsi_s)))
        )

        # RSI oversold and curling up: was below 35 in last 5 bars AND now rising
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

        # Golden/death cross: did the cross happen within the last 5 trading days?
        gc = any(
            ma20.iloc[-_i] > ma50.iloc[-_i] and ma20.iloc[-_i - 1] <= ma50.iloc[-_i - 1]
            for _i in range(1, min(6, len(ma20)))
        )
        dc = any(
            ma20.iloc[-_i] < ma50.iloc[-_i] and ma20.iloc[-_i - 1] >= ma50.iloc[-_i - 1]
            for _i in range(1, min(6, len(ma20)))
        )

        hi52 = float(close.rolling(252).max().iloc[-1]) if len(close) >= 252 else float(close.max())
        lo52 = float(close.rolling(252).min().iloc[-1]) if len(close) >= 252 else float(close.min())
        pfl  = (price - lo52) / (hi52 - lo52) if (hi52 - lo52) else 0.5

        return {
            "price":             round(float(price), 2),
            "ma20":              round(float(ma20.iloc[-1]), 2),
            "ma50":              round(float(ma50.iloc[-1]), 2),
            "ma200":             round(float(ma200.iloc[-1]), 2) if ma200 is not None else None,
            "rsi":               round(rsi, 1),
            "bull_div":          bull_div,
            "bear_div":          bear_div,
            "rsi_cross_50":      rsi_cross_50,
            "rsi_curling_up":    rsi_curling_up,
            "macd_cross_up":     macd_xup,
            "macd_above_signal": float(macd_line.iloc[-1]) > float(sig_line.iloc[-1]),
            "hist_growing":      hist_.iloc[-1] > hist_.iloc[-2],
            "bb_pct":            round(bb_pct, 2),
            "bb_squeeze":        bb_sq,
            "vol_ratio":         round(vol_ratio, 2),
            "golden_cross":      gc,
            "death_cross":       dc,
            "pct_from_lo":       round(pfl, 2),
        }
    except Exception as e:
        print(f"  [ERR] Indicators: {e}")
        return None

def compute_indicators(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="1y", interval="1d")
        return _compute_indicators_from_hist(hist)
    except Exception as e:
        print(f"  [ERR] Daily indicators for {ticker}: {e}")
        return None

def compute_indicators_1h(ticker):
    # yfinance limits 1h candles to the last 60 days — fine for short-term confluence checks.
    try:
        hist = yf.Ticker(ticker).history(period="60d", interval="1h")
        return _compute_indicators_from_hist(hist)
    except Exception as e:
        print(f"  [WARN] 1H indicators for {ticker}: {e}")
        return None

def get_iv_rank_yf(ticker):
    try:
        iv = yf.Ticker(ticker).info.get("impliedVolatility") or 0.0
        return {"iv": round(min(iv*100,100),1), "iv_rank": round(min(iv*200,100),1)}
    except Exception:
        return {"iv": 0, "iv_rank": 0}

def get_earnings_date(ticker):
    try:
        tk  = yf.Ticker(ticker)
        cal = tk.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or []
            val = ed[0] if isinstance(ed, (list, tuple)) else ed
            if val and hasattr(val, "date"):
                return val.date()
        if hasattr(cal, "empty") and not cal.empty and "Earnings Date" in cal.columns:
            val = cal["Earnings Date"].iloc[0]
            return val.date() if hasattr(val, "date") else None
    except Exception:
        pass
    return None

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

# ── Scoring ───────────────────────────────────────────────
def score_ticker(ticker, ind, ivdata, for_leaps=False, ind_1h=None):
    signals, score, max_pts = [], 0, 0
    def add(pts, earned, label):
        nonlocal score, max_pts
        max_pts += pts
        if earned: score += pts
        signals.append({"label": label, "bullish": earned})
    def add_mtf(pts_leaps, pts_puts, daily_flag, hourly_flag, label):
        # Confluence-aware scoring for the LEAPS bounce-scalp path only — checks the same
        # signal on both 1D and 1H candles and gives a bonus when both fire together.
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
    # MA trend signals — reduced weight for LEAPS bounce-scalp (catching reversals, not trend)
    add(8 if not for_leaps else 4,  p > ind["ma20"],               "Above 20 MA")
    add(10 if not for_leaps else 4, p > ind["ma50"],               "Above 50 MA")
    if ind["ma200"]: add(12 if not for_leaps else 5, p > ind["ma200"], "Above 200 MA")
    add(8,  ind["ma20"] > ind["ma50"],     "20 MA above 50 MA")
    add_mtf(10, 10, ind["golden_cross"], h.get("golden_cross", False), "Golden cross just formed")
    add(10, not ind["death_cross"],        "No death cross")
    # Top-weighted bounce signals for LEAPS
    add_mtf(20, 12, ind["bull_div"], h.get("bull_div", False), "RSI bullish divergence — HIGH PRIORITY")
    add(8,  not ind["bear_div"],           "No RSI bearish divergence")
    add(8,  30 < rsi < 70,                "RSI in healthy range")
    add(10, rsi > 50,                     "RSI above 50")
    add(6,  rsi < 35,                     "RSI oversold — bounce zone")
    add_mtf(18, 6, ind["rsi_curling_up"], h.get("rsi_curling_up", False), "RSI oversold and curling up")
    add(8,  ind["rsi_cross_50"],          "RSI crossed above 50 — momentum shift")
    add_mtf(10, 10, ind["macd_cross_up"], h.get("macd_cross_up", False), "MACD bullish crossover")
    add(6,  ind["macd_above_signal"],     "MACD above signal line")
    add(6,  ind["hist_growing"],          "MACD histogram expanding")
    add_mtf(20, 8, ind["bb_pct"] < 0.25, h.get("bb_pct", 1) < 0.25, "Near lower Bollinger band")
    add(6,  ind["bb_squeeze"],            "Bollinger squeeze — big move pending")
    add(6,  ind["vol_ratio"] >= 1.5,     "Above-average volume")
    add(8,  ind["pct_from_lo"] < 0.35,   "Lower third of 52-week range")
    add(6,  ind["pct_from_lo"] > 0.65,   "Upper range — momentum")
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

# ── Main scan ─────────────────────────────────────────────
def run_scan():
    global scan_results
    scan_results["status"] = "Scanning..."
    print(f"\n[SCAN] {datetime.now().strftime('%I:%M %p')} — scanning {len(config.WATCHLIST)} tickers")
    leaps_list, puts_list, alerts = [], [], []

    for ticker in config.WATCHLIST:
        print(f"  → {ticker}")
        ind = compute_indicators(ticker)
        if not ind: continue
        ind_1h = compute_indicators_1h(ticker)  # None if unavailable — leaps scoring falls back to 1D-only
        ivdata = get_iv_rank_yf(ticker)

        leaps_score, leaps_signals = score_ticker(ticker, ind, ivdata, for_leaps=True, ind_1h=ind_1h)
        puts_score,  puts_signals  = score_ticker(ticker, ind, ivdata, for_leaps=False)
        leaps_bull = [s["label"] for s in leaps_signals if s["bullish"]]
        leaps_bear = [s["label"] for s in leaps_signals if not s["bullish"]]
        puts_bull  = [s["label"] for s in puts_signals  if s["bullish"]]
        puts_bear  = [s["label"] for s in puts_signals  if not s["bullish"]]

        if ind["rsi"] > 75:    alerts.append({"ticker": ticker, "msg": f"RSI overbought at {ind['rsi']} — review LEAPS exits"})
        if ind["bear_div"]:    alerts.append({"ticker": ticker, "msg": "Bearish RSI divergence — caution on longs"})
        if ind["death_cross"]: alerts.append({"ticker": ticker, "msg": "Death cross — avoid new LEAPS entries"})

        leaps_base = {"ticker": ticker, "score": leaps_score, "price": ind["price"], "rsi": ind["rsi"],
                      "iv_rank": ivdata["iv_rank"], "bull_signals": leaps_bull, "bear_signals": leaps_bear,
                      "ma20": ind["ma20"], "ma50": ind["ma50"], "ma200": ind["ma200"], "vol_ratio": ind["vol_ratio"]}
        puts_base  = {"ticker": ticker, "score": puts_score,  "price": ind["price"], "rsi": ind["rsi"],
                      "iv_rank": ivdata["iv_rank"], "bull_signals": puts_bull,  "bear_signals": puts_bear,
                      "ma20": ind["ma20"], "ma50": ind["ma50"], "ma200": ind["ma200"], "vol_ratio": ind["vol_ratio"]}

        opts           = get_options_data(ticker)
        leaps_contract = best_contract(opts, "LEAPS - Bounce Scalp")
        put_contract   = best_contract(opts, "Naked put")
        blank_leaps    = {"type": "LEAPS - Bounce Scalp", "strike": "—", "exp": "—", "dte": "—", "delta": "—", "mid": "—"}
        blank_put      = {"type": "Naked put",            "strike": "—", "exp": "—", "dte": "—", "delta": "—", "mid": "—"}

        ls = leaps_score - (10 if ind["rsi"] > 70 else 0) - (15 if ivdata["iv_rank"] > config.LEAPS_MAX_IV_RANK else 0)
        if ls >= config.MIN_SCORE:
            lc               = leaps_contract or blank_leaps
            earnings_dt      = get_earnings_date(ticker)
            earnings_warning = None
            if earnings_dt and isinstance(lc.get("dte"), int):
                days_to_earn = (earnings_dt - date.today()).days
                if 0 < days_to_earn <= lc["dte"]:
                    earnings_warning = earnings_dt.strftime("%b %d")
            leaps_list.append({**leaps_base, "score": ls, "contract": lc,
                               "trade_type": "LEAPS - Bounce Scalp", "earnings_warning": earnings_warning})

        ps = puts_score - (20 if ivdata["iv_rank"] < config.PUT_MIN_IV_RANK else 0) - (15 if ind["death_cross"] else 0) - (10 if ind["bear_div"] else 0)
        if ps >= config.MIN_SCORE:
            pc      = put_contract or blank_put
            cushion = support_cushion_info(pc.get("strike"), ind) if isinstance(pc.get("strike"), (int, float)) else None
            puts_list.append({**puts_base, "score": ps, "contract": pc,
                              "trade_type": "Naked put", "support_cushion": cushion})

    # Alerts from logged positions
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
        if cur is None:
            continue
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
                    "watchlist_count": len(config.WATCHLIST)}
    print(f"[DONE] {len(leaps_list)} LEAPS | {len(puts_list)} put setups | {len(alerts)} alerts")

# ── Flask routes ──────────────────────────────────────────
@app.route("/")
def index(): return render_template_string(DASHBOARD_HTML)

@app.route("/data")
def data(): return jsonify(scan_results)

@app.route("/rescan", methods=["POST"])
def rescan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"ok": True})

# ── Quote route (for morning briefing) ───────────────────
@app.route("/quote")
def quote():
    ticker = request.args.get("ticker", "SPY")
    try:
        tk   = yf.Ticker(ticker)
        hist = tk.history(period="5d", interval="1d")
        info = tk.info
        if hist.empty:
            return jsonify({"price": 0, "chg": 0, "chg_pct": 0, "above_50ma": None, "rsi": None})

        price    = round(float(hist["Close"].iloc[-1]), 2)
        prev     = round(float(hist["Close"].iloc[-2]), 2) if len(hist) > 1 else price
        chg      = round(price - prev, 2)
        chg_pct  = round((chg / prev) * 100, 2) if prev else 0

        # 50 MA
        hist_long = tk.history(period="1y", interval="1d")
        above_50ma = None
        rsi_val    = None
        if not hist_long.empty and len(hist_long) >= 50:
            ma50       = hist_long["Close"].rolling(50).mean().iloc[-1]
            above_50ma = bool(price > ma50)
            # RSI
            d    = hist_long["Close"].diff()
            gain = d.clip(lower=0).rolling(14).mean()
            loss = (-d.clip(upper=0)).rolling(14).mean()
            rs   = gain / loss.replace(0, float("nan"))
            rsi_val = round(float((100 - 100 / (1 + rs)).iloc[-1]), 1)

        return jsonify({"price": price, "chg": chg, "chg_pct": chg_pct,
                        "above_50ma": above_50ma, "rsi": rsi_val})
    except Exception as e:
        return jsonify({"error": str(e), "price": 0, "chg": 0, "chg_pct": 0})

# ── News route (per ticker) ───────────────────────────────
@app.route("/news")
def news():
    ticker = request.args.get("ticker", "AAPL")
    try:
        tk    = yf.Ticker(ticker)
        items = tk.news or []
        news_list = []
        for n in items[:8]:
            # yfinance >= 0.2.37 nests title/date inside a "content" object
            content  = n.get("content") or {}
            title    = n.get("title") or content.get("title", "")
            if not title:
                continue
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
            lower = title.lower()
            if any(w in lower for w in ["beat", "surge", "jump", "rally", "upgrade", "buy", "growth", "record", "bullish", "up"]):
                sentiment = "bull"
            elif any(w in lower for w in ["miss", "drop", "fall", "cut", "downgrade", "sell", "loss", "warning", "bearish", "down"]):
                sentiment = "bear"
            else:
                sentiment = ""
            news_list.append({"title": title, "time": time_str, "sentiment": sentiment})
        return jsonify({"news": news_list})
    except Exception as e:
        return jsonify({"news": [], "error": str(e)})

# ── Positions log ─────────────────────────────────────────
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
    }
    positions = load_positions()
    positions.append(pos)
    save_positions(positions)
    return jsonify({"ok": True, "id": pos["id"]})

@app.route("/log_position/<pos_id>", methods=["DELETE"])
def delete_position(pos_id):
    save_positions([p for p in load_positions() if p.get("id") != pos_id])
    return jsonify({"ok": True})

# ── Dashboard HTML ────────────────────────────────────────
DASHBOARD_HTML = open("dashboard.html", encoding="utf-8").read() if os.path.exists("dashboard.html") else "<h2>dashboard.html not found</h2>"

def schedule_loop():
    schedule.every(config.REFRESH_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  OPTIONS SCANNER")
    print("=" * 55)
    if login_tastytrade():
        quote_token = get_api_quote_token()
        if quote_token:
            print(f"[OK] Received quote token for DXLink ({quote_token.get('level','unknown')} level)")
        else:
            print("[WARN] Could not retrieve DXLink quote token")
    threading.Thread(target=refresh_token_loop, daemon=True).start()
    print("[INFO] Running first scan — please wait...")
    threading.Thread(target=run_scan, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()
    print(f"[INFO] Dashboard → http://localhost:{config.PORT}")
    print(f"[INFO] Auto-refreshes every {config.REFRESH_MINUTES} minutes")
    print("[INFO] Press Ctrl+C to stop\n")
    webbrowser.open(f"http://localhost:{config.PORT}", new=2)
    app.run(port=config.PORT, debug=False, use_reloader=False)
