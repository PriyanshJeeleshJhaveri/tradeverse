"""
price_service.py
------------------
Live price lookups for TradeVerse, talking directly to:
  - Yahoo Finance (Indian NSE/BSE stocks)
  - Twelve Data (US stocks & indices)
  - CoinGecko's public REST API (crypto)

Also provides:
  - search_symbols()   live "search as you type" across both stocks and crypto
  - get_chart_data()   OHLC-style time series for the asset detail page's
                        graph, for the 24 Hours / 1 Week / 1 Month / 1 Year
                        ranges shown on that page.

Results are cached in-memory (quotes & charts for PRICE_CACHE_TTL_SECONDS,
search results for a much shorter SEARCH_CACHE_TTL_SECONDS) so we don't
hammer either API on every page view / keystroke.

Config is read from a `.env` file (see `.env.example`) via python-dotenv:
    COINGECKO_API_KEY          optional CoinGecko demo/pro API key
    TWELVEDATA_API_KEY         Twelve Data API key for US stock/index prices
    PRICE_CACHE_TTL_SECONDS    quote/chart cache lifetime in seconds (default 600 = 10 min)
    TRADE_PRICE_MAX_AGE_SECONDS  max age of a price used to fill a buy/sell (default 60)
    REQUEST_TIMEOUT_SECONDS    HTTP timeout in seconds (default 6)

Caching is two-layered: a per-instance memory cache (L1) and a shared MongoDB
`api_cache` collection (L2). On Vercel every cold start / new instance starts
with an empty memory cache, so the shared L2 cache is what keeps us inside the
free-tier limits of Twelve Data (8 calls/min) and CoinGecko.
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
CACHE_TTL_SECONDS = int(os.getenv("PRICE_CACHE_TTL_SECONDS", "600"))
TRADE_PRICE_MAX_AGE = int(os.getenv("TRADE_PRICE_MAX_AGE_SECONDS", "60"))
# Vercel Hobby functions have a short execution limit, so fail fast on slow APIs.
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "6"))
SEARCH_CACHE_TTL_SECONDS = 300
CACHE_COLLECTION = os.getenv("MONGODB_CACHE_COLLECTION", "api_cache").strip() or "api_cache"
MAX_PARALLEL_FETCHES = 6
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=&^_]{0,24}$")

YAHOO_FINANCE_BASE_URL = "https://query1.finance.yahoo.com"
YAHOO_SEARCH_URL = YAHOO_FINANCE_BASE_URL + "/v1/finance/search"
YAHOO_CHART_URL = YAHOO_FINANCE_BASE_URL + "/v8/finance/chart"
INDIA_TIMEZONE = ZoneInfo("Asia/Kolkata")

TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# Index boxes on the dashboard. Yahoo Finance serves these for free (no key),
# unlike Twelve Data where index tickers need a paid plan.
INDEX_SYMBOLS = {
    "S&P 500": "^GSPC",
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
}

# Last-resort placeholder values, only used if Yahoo can't be reached at all.
INDEX_FALLBACK = {
    "S&P 500": {"price": 6250.00, "change": 28.13, "change_percent": 0.45},
    "NIFTY 50": {"price": 25000.00, "change": 95.00, "change_percent": 0.38},
    "SENSEX": {"price": 82000.00, "change": 336.20, "change_percent": 0.41},
}

# Common crypto ticker -> CoinGecko coin id. Anything not listed here gets
# resolved on the fly via CoinGecko's /search endpoint (and then cached).
CRYPTO_ID_MAP = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "BNB-USD": "binancecoin",
    "XRP-USD": "ripple",
    "ADA-USD": "cardano",
    "DOGE-USD": "dogecoin",
    "MATIC-USD": "matic-network",
    "DOT-USD": "polkadot",
    "LTC-USD": "litecoin",
    "AVAX-USD": "avalanche-2",
    "TRX-USD": "tron",
    "LINK-USD": "chainlink",
    "SHIB-USD": "shiba-inu",
}

# Time-range options shown as tabs on the asset detail page, and the
# interval/lookback each one maps to for Twelve Data (stocks) & CoinGecko (crypto).
# CoinGecko picks 5-min / hourly granularity automatically when no `interval`
# is passed (the explicit "hourly" option is not available on free plans), so
# only the 1-year range asks for "daily".
# yahoo_range is the history window requested from Yahoo; points are then
# trimmed to `lookback` measured back from the LAST available candle, so the
# 24H chart still works on weekends / holidays when the market is closed.
CHART_RANGES = {
    "24H": {"label": "24 Hours", "td_interval": "15min", "lookback": timedelta(days=1), "cg_interval": None,
            "yahoo_interval": "15m", "yahoo_range": "5d", "td_outputsize": 100},
    "1W": {"label": "1 Week", "td_interval": "1h", "lookback": timedelta(days=7), "cg_interval": None,
           "yahoo_interval": "1h", "yahoo_range": "1mo", "td_outputsize": 100},
    "1M": {"label": "1 Month", "td_interval": "4h", "lookback": timedelta(days=30), "cg_interval": None,
           "yahoo_interval": "1d", "yahoo_range": "3mo", "td_outputsize": 100},
    "1Y": {"label": "1 Year", "td_interval": "1week", "lookback": timedelta(days=365), "cg_interval": "daily",
           "yahoo_interval": "1wk", "yahoo_range": "2y", "td_outputsize": 60},
}

_mem_cache = {}            # L1: key -> {"data": ..., "ts": float}   (per instance)
_coingecko_id_cache = {}   # symbol -> coingecko coin id
_MEM_CACHE_MAX = 600
_l2_disabled_until = 0.0   # circuit breaker for the shared Mongo cache


def _now():
    return time.time()


def is_valid_symbol(symbol):
    """Cheap sanity check so arbitrary text never reaches the upstream APIs."""
    return bool(_SYMBOL_RE.match((symbol or "").strip().upper()))


def _is_crypto_symbol(symbol):
    symbol_u = symbol.strip().upper()
    return symbol_u in CRYPTO_ID_MAP or symbol_u.endswith("-USD")


# ---------------------------------------------------------------------------
# Two-layer cache: memory (L1) + shared MongoDB collection (L2)
# ---------------------------------------------------------------------------

def _l2_collection():
    global _l2_disabled_until
    if _now() < _l2_disabled_until:
        return None
    if not os.getenv("MONGODB_URI", "").strip():
        return None
    try:
        import mongo_db
        return mongo_db.get_collection(CACHE_COLLECTION)
    except Exception:
        _l2_disabled_until = _now() + 60
        return None


def _l2_trip(exc):
    """If Mongo misbehaves, stop using the shared cache for a minute."""
    global _l2_disabled_until
    _l2_disabled_until = _now() + 60
    print(f"[price_service] shared cache disabled for 60s: {exc}")


def init_cache_indexes():
    """TTL index so old cache documents are purged automatically."""
    col = _l2_collection()
    if col is None:
        return
    try:
        existing = col.index_information()
        if "purge_at_ttl" not in existing:
            col.create_index("purge_at", expireAfterSeconds=0, name="purge_at_ttl")
    except Exception as exc:
        _l2_trip(exc)


def _cache_read_many(keys):
    """Return {key: {"data":..., "ts":...}} for every key found (fresh OR stale)."""
    found = {}
    missing = []
    for key in keys:
        hit = _mem_cache.get(key)
        if hit:
            found[key] = hit
        else:
            missing.append(key)

    if missing:
        col = _l2_collection()
        if col is not None:
            try:
                for doc in col.find({"_id": {"$in": missing}}):
                    entry = {"data": doc.get("data"), "ts": float(doc.get("ts", 0))}
                    if entry["data"] is not None:
                        found[doc["_id"]] = entry
                        _mem_cache[doc["_id"]] = entry
            except Exception as exc:
                _l2_trip(exc)
    return found


def _cache_write(key, data):
    ts = _now()
    if len(_mem_cache) > _MEM_CACHE_MAX:
        _mem_cache.clear()
    _mem_cache[key] = {"data": data, "ts": ts}

    col = _l2_collection()
    if col is None:
        return
    try:
        col.update_one(
            {"_id": key},
            {"$set": {"data": data, "ts": ts,
                      "purge_at": datetime.now(timezone.utc) + timedelta(days=2)}},
            upsert=True,
        )
    except Exception as exc:
        _l2_trip(exc)


def _is_fresh(entry, ttl):
    return bool(entry) and (_now() - entry["ts"] < ttl)


def _parse_point_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _trim_to_lookback(points, lookback):
    """Keep only candles within `lookback` of the LAST candle.

    Measuring from the last candle (not from "now") means a 24H chart still
    shows the latest trading session on weekends and market holidays.
    """
    if not points:
        return points
    last = _parse_point_time(points[-1]["t"])
    if last is None:
        return points
    cutoff = last - lookback
    kept = []
    for p in points:
        t = _parse_point_time(p["t"])
        if t is None or t >= cutoff:
            kept.append(p)
    return kept or points


# ---------------------------------------------------------------------------
# Yahoo Finance (Indian NSE/BSE stocks)
# ---------------------------------------------------------------------------

def _is_indian_stock_symbol(symbol):
    """True for Yahoo Finance NSE (.NS) and BSE (.BO) equity symbols."""
    symbol_u = (symbol or "").strip().upper()
    return symbol_u.endswith(".NS") or symbol_u.endswith(".BO")


def _is_yahoo_symbol(symbol):
    """Symbols served by Yahoo Finance: NSE/BSE equities and ^INDEX tickers."""
    symbol_u = (symbol or "").strip().upper()
    return _is_indian_stock_symbol(symbol_u) or symbol_u.startswith("^")


def _yahoo_headers():
    # A normal browser-like User-Agent makes the server-side request more
    # reliable while keeping Yahoo calls entirely on the Flask backend.
    return {
        "User-Agent": "Mozilla/5.0 (compatible; TradeVerse/1.0; +https://vercel.com/)"
    }


def _yahoo_chart_json(symbol, interval="1d", range_value="1d",
                      period1=None, period2=None):
    """Fetch Yahoo Finance chart JSON for an Indian stock."""
    params = {"interval": interval}
    if period1 is not None and period2 is not None:
        params["period1"] = int(period1)
        params["period2"] = int(period2)
    else:
        params["range"] = range_value

    try:
        resp = requests.get(
            f"{YAHOO_CHART_URL}/{symbol}",
            params=params,
            headers=_yahoo_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()

        chart = payload.get("chart", {})
        if chart.get("error"):
            print(f"[price_service] Yahoo Finance error for {symbol}: {chart['error']}")
            return None

        results = chart.get("result") or []
        return results[0] if results else None
    except Exception as e:
        print(f"[price_service] Yahoo Finance request failed for {symbol}: {e}")
        return None


def _yahoo_india_quote(symbol):
    """Latest INR quote for an NSE/BSE Indian stock."""
    symbol = symbol.strip().upper()
    result = _yahoo_chart_json(symbol, interval="1d", range_value="1d")
    if not result:
        return None

    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        return None

    previous_close = (
        meta.get("chartPreviousClose")
        or meta.get("previousClose")
    )

    # Yahoo's Indian equity quotes are already denominated in INR.
    # Calculate the change ourselves so the existing TradeVerse logic
    # remains unchanged.
    try:
        price = float(price)
        previous_close = float(previous_close) if previous_close is not None else None
    except (TypeError, ValueError):
        return None

    change = (price - previous_close) if previous_close is not None else None
    change_percent = (
        (change / previous_close * 100)
        if change is not None and previous_close
        else None
    )

    symbol_name = (
        meta.get("longName")
        or meta.get("shortName")
        or symbol
    )
    currency = meta.get("currency") or ("INR" if _is_indian_stock_symbol(symbol) else "USD")
    if symbol.startswith("^"):
        exchange = "INDEX"
    else:
        exchange = "NSE" if symbol.endswith(".NS") else "BSE"

    return {
        "symbol": symbol,
        "name": symbol_name,
        "price": round(price, 4),
        "previous_close": round(previous_close, 4) if previous_close is not None else None,
        "change": round(change, 4) if change is not None else None,
        "change_percent": round(change_percent, 2) if change_percent is not None else None,
        "currency": currency,
        "exchange": exchange,
    }


def _yahoo_india_search(query, limit=6):
    """
    Live Indian-stock autocomplete using Yahoo Finance's search endpoint.
    Results are restricted to NSE (.NS) and BSE (.BO) equity symbols so the
    existing US-stock and crypto search behaviour remains unchanged.
    """
    try:
        resp = requests.get(
            YAHOO_SEARCH_URL,
            params={
                "q": query,
                "quotesCount": max(10, limit * 4),
                "newsCount": 0,
                "enableFuzzyQuery": "true",
            },
            headers=_yahoo_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        quotes = payload.get("quotes") or []

        results = []
        seen = set()

        for row in quotes:
            symbol = (row.get("symbol") or "").upper()
            quote_type = (row.get("quoteType") or "").upper()

            if quote_type not in ("EQUITY", "ETF"):
                continue
            if not _is_indian_stock_symbol(symbol):
                continue
            if symbol in seen:
                continue

            seen.add(symbol)
            exchange_code = (row.get("exchange") or "").upper()
            exchange_name = (
                "NSE" if symbol.endswith(".NS")
                else "BSE" if symbol.endswith(".BO")
                else exchange_code or "India"
            )

            results.append({
                "symbol": symbol,
                "name": row.get("longname")
                        or row.get("shortname")
                        or symbol,
                "type": "stock",
                "exchange": exchange_name,
                "currency": "INR",
            })

            if len(results) >= limit:
                break

        return results
    except Exception as e:
        print(f"[price_service] Yahoo India search failed for '{query}': {e}")
        return []


def _yahoo_india_time_series(symbol, range_cfg):
    """Chronological close-price series from Yahoo Finance for Indian stocks."""
    result = _yahoo_chart_json(
        symbol,
        interval=range_cfg["yahoo_interval"],
        range_value=range_cfg["yahoo_range"],
    )
    if not result:
        return None

    timestamps = result.get("timestamp") or []
    quote_rows = (result.get("indicators") or {}).get("quote") or []
    if not quote_rows:
        return None

    closes = quote_rows[0].get("close") or []
    points = []

    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            # Keep labels in Indian Standard Time because these are Indian
            # market prices.
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(INDIA_TIMEZONE)
            points.append({
                "t": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "price": float(close),
            })
        except (TypeError, ValueError, OSError):
            continue

    points = _trim_to_lookback(points, range_cfg["lookback"])
    return points or None


# ---------------------------------------------------------------------------
# Twelve Data (US & Indian stocks + indices, where the plan allows)
# ---------------------------------------------------------------------------

def _twelvedata_quote(symbol):
    if not TWELVEDATA_API_KEY:
        print(f"[price_service] TWELVEDATA_API_KEY is not set - skipping lookup for {symbol}")
        return None

    try:
        resp = requests.get(
            TWELVEDATA_BASE_URL + "/quote",
            params={"symbol": symbol, "apikey": TWELVEDATA_API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        payload = resp.json()

        if isinstance(payload, dict) and payload.get("status") == "error":
            print(f"[price_service] Twelve Data error for {symbol}: {payload.get('message')}")
            return None

        price = payload.get("close")
        if price in (None, ""):
            print(f"[price_service] Twelve Data returned no data for {symbol}: {payload}")
            return None

        prev_close = payload.get("previous_close")
        change = payload.get("change")
        pct = payload.get("percent_change")
        name = payload.get("name") or symbol

        return {
            "symbol": symbol,
            "name": name,
            "price": round(float(price), 4),
            "previous_close": round(float(prev_close), 4) if prev_close not in (None, "") else None,
            "change": round(float(change), 4) if change not in (None, "") else None,
            "change_percent": round(float(pct), 2) if pct not in (None, "") else None,
        }
    except Exception as e:
        print(f"[price_service] Twelve Data quote request failed for {symbol}: {e}")
        return None


# Live search is scoped to the US stock market only. Twelve Data's
# symbol_search endpoint returns matches from every exchange it covers
# (Indian, European, crypto-adjacent tickers, etc.), and with a small
# outputsize the actual US-listed match can get crowded out entirely -
# which is what was causing US tickers to go missing from results. So we
# pull a larger raw batch and filter down to US exchanges ourselves.
US_EXCHANGES = {"NASDAQ", "NYSE", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS", "OTC", "OTC MARKETS", "CBOE", "IEX"}


def _twelvedata_search(query, limit=8):
    """US-stock-only symbol search (autocomplete) via Twelve Data."""
    if not TWELVEDATA_API_KEY:
        print("[price_service] TWELVEDATA_API_KEY is not set - skipping stock search")
        return []

    try:
        resp = requests.get(
            TWELVEDATA_BASE_URL + "/symbol_search",
            params={
                "symbol": query,
                "outputsize": 30,           # pull a wider raw batch...
                "country": "United States",  # ...ask the API to scope to the US where it supports it...
                "apikey": TWELVEDATA_API_KEY,
            },
            timeout=REQUEST_TIMEOUT,
        )
        payload = resp.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []

        results = []
        for row in rows:
            symbol = row.get("symbol")
            if not symbol:
                continue

            # ...and always double-check client-side, since not every plan
            # honours the "country" filter server-side.
            country = (row.get("country") or "").strip().lower()
            exchange = (row.get("exchange") or "").strip().upper()
            is_us = country == "united states" or exchange in US_EXCHANGES
            if not is_us:
                continue

            results.append({
                "symbol": symbol,
                "name": row.get("instrument_name") or symbol,
                "type": "stock",
                "exchange": row.get("exchange") or "US",
            })
            if len(results) >= limit:
                break

        return results
    except Exception as e:
        print(f"[price_service] Twelve Data search failed for '{query}': {e}")
        return []


def _twelvedata_time_series(symbol, range_cfg):
    """Returns a chronological list of {"t": datetime_str, "price": float} or None."""
    if not TWELVEDATA_API_KEY:
        print(
            f"[price_service] TWELVEDATA_API_KEY is not set - "
            f"skipping chart lookup for {symbol}"
        )
        return None

    try:
        resp = requests.get(
            TWELVEDATA_BASE_URL + "/time_series",
            params={
                "symbol": symbol,
                "interval": range_cfg["td_interval"],
                "outputsize": range_cfg["td_outputsize"],
                "timezone": "America/New_York",
                "apikey": TWELVEDATA_API_KEY,
            },
            timeout=REQUEST_TIMEOUT,
        )

        payload = resp.json()

        if isinstance(payload, dict) and payload.get("status") == "error":
            print(
                f"[price_service] Twelve Data time_series error for "
                f"{symbol}: {payload.get('message')}"
            )
            return None

        values = payload.get("values") or []

        if not values:
            print(
                f"[price_service] Twelve Data returned no chart data "
                f"for {symbol}: {payload}"
            )
            return None

        # Twelve Data returns newest first.
        values = list(reversed(values))

        points = []
        for v in values:
            try:
                points.append({
                    "t": v["datetime"],
                    "price": float(v["close"]),
                })
            except (KeyError, TypeError, ValueError):
                continue

        # Candles only exist while the market is open, so a fixed candle count
        # covers more calendar time than the tab label says. Trim to the window.
        points = _trim_to_lookback(points, range_cfg["lookback"])
        return points or None

    except Exception as e:
        print(
            f"[price_service] Twelve Data time_series request "
            f"failed for {symbol}: {e}"
        )
        return None


# ---------------------------------------------------------------------------
# CoinGecko (crypto)
# ---------------------------------------------------------------------------

def _coingecko_headers():
    if COINGECKO_API_KEY:
        return {"x-cg-demo-api-key": COINGECKO_API_KEY}
    return {}


def _resolve_coingecko_id(symbol):
    symbol_u = symbol.strip().upper()
    if symbol_u in CRYPTO_ID_MAP:
        return CRYPTO_ID_MAP[symbol_u]
    if symbol_u in _coingecko_id_cache:
        return _coingecko_id_cache[symbol_u]

    # Shared cache: another instance may already have resolved this ticker.
    id_key = "cgid:" + symbol_u
    shared = _cache_read_many([id_key]).get(id_key)
    if shared and isinstance(shared["data"], dict) and shared["data"].get("id"):
        _coingecko_id_cache[symbol_u] = shared["data"]["id"]
        return shared["data"]["id"]

    base = symbol_u.split("-")[0]
    try:
        resp = requests.get(
            COINGECKO_BASE_URL + "/search",
            params={"query": base},
            headers=_coingecko_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
        match = next((c for c in coins if c.get("symbol", "").upper() == base), None)
        chosen = match or (coins[0] if coins else None)
        if chosen:
            _coingecko_id_cache[symbol_u] = chosen["id"]
            _cache_write(id_key, {"id": chosen["id"]})
            return chosen["id"]
    except Exception:
        pass
    return None


def _coingecko_search(query, limit=6):
    """Live search for crypto coins via CoinGecko's /search endpoint."""
    try:
        resp = requests.get(
            COINGECKO_BASE_URL + "/search",
            params={"query": query},
            headers=_coingecko_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])

        results = []
        for c in coins[:limit]:
            raw_symbol = (c.get("symbol") or "").upper()
            if not raw_symbol or not c.get("id"):
                continue
            ticker = raw_symbol + "-USD"
            _coingecko_id_cache[ticker] = c["id"]  # cache the resolution now, saves a lookup later
            results.append({
                "symbol": ticker,
                "name": c.get("name") or ticker,
                "type": "crypto",
                "exchange": "Crypto",
            })
        return results
    except Exception as e:
        print(f"[price_service] CoinGecko search failed for '{query}': {e}")
        return []


def _coin_row_to_quote(symbol, coin):
    price = coin.get("current_price")
    if price is None:
        return None
    change = coin.get("price_change_24h")
    change_pct = coin.get("price_change_percentage_24h")
    prev_close = (price - change) if change is not None else None

    return {
        "symbol": symbol,
        "name": coin.get("name") or symbol,
        "price": round(float(price), 4),
        "previous_close": round(float(prev_close), 4) if prev_close is not None else None,
        "change": round(float(change), 4) if change is not None else None,
        "change_percent": round(float(change_pct), 2) if change_pct is not None else None,
    }


def _coingecko_quotes(symbols):
    """Quotes for many crypto tickers in ONE CoinGecko request -> {symbol: quote}."""
    id_by_symbol = {}
    for symbol in symbols:
        coin_id = _resolve_coingecko_id(symbol)
        if coin_id:
            id_by_symbol[symbol] = coin_id
    if not id_by_symbol:
        return {}

    try:
        resp = requests.get(
            COINGECKO_BASE_URL + "/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(sorted(set(id_by_symbol.values()))),
                "per_page": 250,
            },
            headers=_coingecko_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        print(f"[price_service] CoinGecko quote request failed: {e}")
        return {}

    if not isinstance(rows, list):
        return {}

    by_id = {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}
    out = {}
    for symbol, coin_id in id_by_symbol.items():
        coin = by_id.get(coin_id)
        quote = _coin_row_to_quote(symbol, coin) if coin else None
        if quote:
            out[symbol] = quote
    return out


def _coingecko_market_chart_range(coin_id, from_ts, to_ts, interval=None):
    """Returns a chronological list of {"t": datetime_str, "price": float} or None."""
    try:
        params = {"vs_currency": "usd", "from": int(from_ts), "to": int(to_ts)}
        if interval:
            params["interval"] = interval

        resp = requests.get(
            COINGECKO_BASE_URL + f"/coins/{coin_id}/market_chart/range",
            params=params,
            headers=_coingecko_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        prices = resp.json().get("prices") or []
        if not prices:
            return None

        points = []
        for ts_ms, price in prices:
            iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            points.append({"t": iso, "price": float(price)})
        return points
    except Exception as e:
        print(f"[price_service] CoinGecko market_chart/range failed for {coin_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API - quotes (used by app.py / portfolio_db.py)
# ---------------------------------------------------------------------------

def _fetch_single_quote(symbol):
    if _is_yahoo_symbol(symbol):
        return _yahoo_india_quote(symbol)
    return _twelvedata_quote(symbol)


def _fetch_quotes(symbols):
    """Fetch many uncached symbols at once (crypto in one batch call, the rest in parallel)."""
    crypto = [s for s in symbols if _is_crypto_symbol(s)]
    others = [s for s in symbols if s not in crypto]
    out = {}

    tasks = len(others) + (1 if crypto else 0)
    if tasks == 0:
        return out

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_FETCHES, tasks)) as pool:
        crypto_future = pool.submit(_coingecko_quotes, crypto) if crypto else None
        other_futures = {s: pool.submit(_fetch_single_quote, s) for s in others}

        if crypto_future is not None:
            try:
                out.update(crypto_future.result())
            except Exception as e:
                print(f"[price_service] crypto batch failed: {e}")
        for symbol, future in other_futures.items():
            try:
                data = future.result()
            except Exception as e:
                print(f"[price_service] quote failed for {symbol}: {e}")
                data = None
            if data is not None:
                out[symbol] = data
    return out


def get_quotes(symbols, max_age=None, stale_limit=None):
    """
    Quotes for many symbols -> {SYMBOL: quote_dict_or_None}.

    - Yahoo Finance for NSE/BSE stocks (.NS/.BO) and ^INDEX tickers
    - CoinGecko for crypto (one request for all coins)
    - Twelve Data for US stocks
    Cached results younger than `max_age` seconds (default PRICE_CACHE_TTL_SECONDS)
    are reused. If a refresh fails, the previous value is returned instead, unless
    it is older than `stale_limit` seconds (None = any age is acceptable).
    """
    ttl = CACHE_TTL_SECONDS if max_age is None else max_age

    keys = {}
    for symbol in symbols:
        symbol_key = (symbol or "").strip().upper()
        if symbol_key and symbol_key not in keys:
            keys[symbol_key] = "q:" + symbol_key

    cached = _cache_read_many(list(keys.values()))
    result = {}
    to_fetch = []
    for symbol_key, cache_key in keys.items():
        entry = cached.get(cache_key)
        if _is_fresh(entry, ttl):
            result[symbol_key] = entry["data"]
        else:
            to_fetch.append(symbol_key)

    if to_fetch:
        fetched = _fetch_quotes(to_fetch)
        for symbol_key in to_fetch:
            data = fetched.get(symbol_key)
            if data is not None:
                _cache_write(keys[symbol_key], data)
                result[symbol_key] = data
                continue
            # fetch failed - fall back to whatever we had before
            stale = cached.get(keys[symbol_key])
            if stale and (stale_limit is None or _now() - stale["ts"] < stale_limit):
                result[symbol_key] = stale["data"]
            else:
                result[symbol_key] = None
    return result


def get_quote(symbol, max_age=None, stale_limit=None):
    """Latest quote for one symbol (or None). See get_quotes()."""
    symbol_key = (symbol or "").strip().upper()
    if not symbol_key:
        return None
    return get_quotes([symbol_key], max_age=max_age, stale_limit=stale_limit).get(symbol_key)


def get_price(symbol):
    """Returns just the latest (cache-friendly) price for `symbol`, or None."""
    quote = get_quote(symbol)
    return quote["price"] if quote else None


def get_prices(symbols):
    """Latest cache-friendly prices for many symbols -> {SYMBOL: price_or_None}."""
    quotes = get_quotes(symbols)
    return {s: (q["price"] if q else None) for s, q in quotes.items()}


def get_trade_price(symbol):
    """
    Price used to fill a buy/sell order. Much fresher than the display cache
    (TRADE_PRICE_MAX_AGE_SECONDS, default 60s) and never older than the normal
    cache lifetime, even when the live lookup fails.
    """
    quote = get_quote(symbol, max_age=TRADE_PRICE_MAX_AGE, stale_limit=CACHE_TTL_SECONDS)
    return quote["price"] if quote else None


def get_index_quotes():
    """The three dashboard index boxes, live from Yahoo Finance when possible."""
    quotes = get_quotes(list(INDEX_SYMBOLS.values()))
    rows = []
    for label, symbol in INDEX_SYMBOLS.items():
        quote = quotes.get(symbol)
        if quote:
            rows.append({
                "symbol": symbol,
                "name": label,
                "price": quote["price"],
                "change": quote.get("change"),
                "change_percent": quote.get("change_percent"),
                "proxy": False,
            })
        else:
            rows.append({"symbol": symbol, "name": label, "proxy": True, **INDEX_FALLBACK[label]})
    return rows


# ---------------------------------------------------------------------------
# Public API - live search (dashboard search bar)
# ---------------------------------------------------------------------------

def search_symbols(query, limit=12):
    """
    Live "search as you type" for the dashboard search bar.

    Search sources (queried in parallel):
      - Indian NSE/BSE stocks -> Yahoo Finance
      - US stocks -> Twelve Data
      - Crypto -> CoinGecko

    Indian results are placed before US/crypto results so an input such as
    "RELIANCE" resolves naturally to the NSE/BSE equity. Each source is capped
    at 4 rows and the default limit is 12, so no source is ever crowded out.
    """
    query = (query or "").strip()[:40]
    if not query:
        return []

    cache_key = "s:" + query.lower()
    entry = _cache_read_many([cache_key]).get(cache_key)
    if _is_fresh(entry, SEARCH_CACHE_TTL_SECONDS):
        return entry["data"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        india_f = pool.submit(_yahoo_india_search, query, 4)
        us_f = pool.submit(_twelvedata_search, query, 4)
        crypto_f = pool.submit(_coingecko_search, query, 4)
        parts = []
        for future in (india_f, us_f, crypto_f):
            try:
                parts.append(future.result())
            except Exception as e:
                print(f"[price_service] search source failed: {e}")
                parts.append([])

    results = (parts[0] + parts[1] + parts[2])[:limit]

    # Don't cache an empty answer - it usually means an upstream API hiccup.
    if results:
        _cache_write(cache_key, results)
    elif entry:
        return entry["data"]
    return results


# ---------------------------------------------------------------------------
# Public API - chart data (asset detail page)
# ---------------------------------------------------------------------------

def get_chart_data(symbol, asset_type, range_key):
    """
    Returns chart data + period stats for the asset detail page:
        {
            "symbol": ..., "range": "24H",
            "labels": [...], "prices": [...],
            "current_price": ..., "period_high": ..., "period_low": ...,
            "change_value": ..., "change_percent": ...
        }
    or None if no data could be fetched (and nothing usable was cached).
    `asset_type` is "stock" or "crypto". `range_key` is one of CHART_RANGES.
    """
    symbol = (symbol or "").strip().upper()
    range_key = (range_key or "").strip().upper()
    range_cfg = CHART_RANGES.get(range_key)
    if not range_cfg or not is_valid_symbol(symbol):
        return None

    cache_key = f"c:{asset_type}:{symbol}:{range_key}"
    cached = _cache_read_many([cache_key]).get(cache_key)
    if _is_fresh(cached, CACHE_TTL_SECONDS):
        return cached["data"]

    now = datetime.now(timezone.utc)
    points = None

    if asset_type == "crypto":
        coin_id = _resolve_coingecko_id(symbol)
        if coin_id:
            # CoinGecko's free API only serves the past 365 days; stay just
            # inside that so the 1-year request is never rejected.
            lookback = min(range_cfg["lookback"], timedelta(days=364))
            from_dt = now - lookback
            points = _coingecko_market_chart_range(
                coin_id, from_dt.timestamp(), now.timestamp(), range_cfg["cg_interval"]
            )
    elif _is_indian_stock_symbol(symbol):
        points = _yahoo_india_time_series(symbol, range_cfg)
    else:
        points = _twelvedata_time_series(symbol, range_cfg)

    if not points:
        # fetch failed - fall back to whatever we had before, even if stale
        if cached:
            return cached["data"]
        return None

    prices_only = [p["price"] for p in points]
    period_start_price = prices_only[0]
    current_price = prices_only[-1]
    period_high = max(prices_only)
    period_low = min(prices_only)
    change_value = current_price - period_start_price
    change_percent = (change_value / period_start_price * 100) if period_start_price else 0.0

    data = {
        "symbol": symbol,
        "range": range_key,
        "labels": [p["t"] for p in points],
        "prices": prices_only,
        "current_price": round(current_price, 4),
        "period_high": round(period_high, 4),
        "period_low": round(period_low, 4),
        "change_value": round(change_value, 4),
        "change_percent": round(change_percent, 2),
    }

    _cache_write(cache_key, data)
    return data
