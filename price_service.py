"""
price_service.py
------------------
Live price lookups for TradeVerse, talking directly to:
  - Twelve Data (stocks & indices)
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
    TWELVEDATA_API_KEY         Twelve Data API key (required for stock/index prices)
    PRICE_CACHE_TTL_SECONDS    quote/chart cache lifetime in seconds (default 600 = 10 min)
    REQUEST_TIMEOUT_SECONDS    HTTP timeout in seconds (default 10)
"""

import os
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
CACHE_TTL_SECONDS = int(os.getenv("PRICE_CACHE_TTL_SECONDS", "600"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
SEARCH_CACHE_TTL_SECONDS = 60

TWELVEDATA_BASE_URL = "https://api.twelvedata.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# NOTE: Twelve Data's exact index tickers can vary - double check these
# against Twelve Data's own symbol search if the index boxes come back
# "Unavailable" for your API plan.
INDEX_SYMBOLS = {
    "S&P 500": "SPX",
    "NIFTY 50": "NIFTY",
    "SENSEX": "SENSEX",
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
CHART_RANGES = {
    "24H": {"label": "24 Hours", "td_interval": "15min", "lookback": timedelta(days=1), "cg_interval": None},
    "1W": {"label": "1 Week", "td_interval": "1h", "lookback": timedelta(days=7), "cg_interval": "hourly"},
    "1M": {"label": "1 Month", "td_interval": "4h", "lookback": timedelta(days=30), "cg_interval": "hourly"},
    "1Y": {"label": "1 Year", "td_interval": "1week", "lookback": timedelta(days=365), "cg_interval": "daily"},
}

_quote_cache = {}          # symbol -> {"data": dict, "ts": float}
_chart_cache = {}          # "type:symbol:range" -> {"data": dict, "ts": float}
_search_cache = {}         # query (lowercase) -> {"data": list, "ts": float}
_coingecko_id_cache = {}   # symbol -> coingecko coin id


def _now():
    return time.time()


def _is_crypto_symbol(symbol):
    symbol_u = symbol.strip().upper()
    return symbol_u in CRYPTO_ID_MAP or symbol_u.endswith("-USD")


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


def _twelvedata_time_series(symbol, interval, start_date=None, end_date=None):
    """Returns a chronological list of {"t": datetime_str, "price": float} or None."""
    if not TWELVEDATA_API_KEY:
        print(
            f"[price_service] TWELVEDATA_API_KEY is not set - "
            f"skipping chart lookup for {symbol}"
        )
        return None

    # Number of candles needed for each chart interval.
    # We intentionally request a little extra because stocks don't trade
    # 24/7 and weekends/holidays create gaps.
    outputsize_map = {
        "15min": 100,
        "1h": 100,
        "4h": 100,
        "1week": 60,
    }

    outputsize = outputsize_map.get(interval, 100)

    try:
        resp = requests.get(
            TWELVEDATA_BASE_URL + "/time_series",
            params={
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
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


def _coingecko_quote(symbol):
    coin_id = _resolve_coingecko_id(symbol)
    if not coin_id:
        return None

    try:
        resp = requests.get(
            COINGECKO_BASE_URL + "/coins/markets",
            params={"vs_currency": "usd", "ids": coin_id},
            headers=_coingecko_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        coin = rows[0]

        price = coin.get("current_price")
        change = coin.get("price_change_24h")
        change_pct = coin.get("price_change_percentage_24h")
        prev_close = (price - change) if (price is not None and change is not None) else None

        if price is None:
            return None

        return {
            "symbol": symbol,
            "name": coin.get("name") or symbol,
            "price": round(float(price), 4),
            "previous_close": round(float(prev_close), 4) if prev_close is not None else None,
            "change": round(float(change), 4) if change is not None else None,
            "change_percent": round(float(change_pct), 2) if change_pct is not None else None,
        }
    except Exception:
        return None


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
            iso = datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            points.append({"t": iso, "price": float(price)})
        return points
    except Exception as e:
        print(f"[price_service] CoinGecko market_chart/range failed for {coin_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API - quotes (used by app.py / portfolio_db.py, signatures unchanged)
# ---------------------------------------------------------------------------

def get_quote(symbol):
    """
    Returns a dict with symbol, name, price, previous_close, change and
    change_percent for `symbol`, using CoinGecko for crypto tickers (e.g.
    BTC-USD) and Twelve Data for everything else (stocks & indices).
    Cached for `PRICE_CACHE_TTL_SECONDS` (default 10 minutes).
    """
    symbol_key = symbol.strip().upper()
    cached = _quote_cache.get(symbol_key)
    if cached and (_now() - cached["ts"] < CACHE_TTL_SECONDS):
        return cached["data"]

    if _is_crypto_symbol(symbol_key):
        data = _coingecko_quote(symbol_key)
    else:
        data = _twelvedata_quote(symbol_key)

    if data is None:
        # fetch failed - fall back to whatever we had before, even if stale
        if cached:
            return cached["data"]
        return None

    _quote_cache[symbol_key] = {"data": data, "ts": _now()}
    return data


def get_price(symbol):
    """Returns just the latest price for `symbol`, or None if unavailable."""
    quote = get_quote(symbol)
    return quote["price"] if quote else None


def get_index_quote(label):
    """label is one of the keys in INDEX_SYMBOLS (e.g. 'S&P 500')."""
    symbol = INDEX_SYMBOLS.get(label)
    if not symbol:
        return None
    quote = get_quote(symbol)
    if quote is None:
        return {"symbol": symbol, "name": label, "price": None,
                "change": None, "change_percent": None}
    quote = dict(quote)
    quote["name"] = label
    return quote


# ---------------------------------------------------------------------------
# Public API - live search (dashboard search bar)
# ---------------------------------------------------------------------------

def search_symbols(query, limit=8):
    """
    Live "search as you type" for the dashboard search bar. Searches both
    US stocks (Twelve Data) and crypto (CoinGecko) - stock matches are
    always listed before crypto matches, regardless of how many of each
    come back, since the concatenation order below is preserved by the
    slice at the end.
    Cached per-query for SEARCH_CACHE_TTL_SECONDS to keep fast typing cheap.
    """
    query = (query or "").strip()
    if not query:
        return []

    query_key = query.lower()
    cached = _search_cache.get(query_key)
    if cached and (_now() - cached["ts"] < SEARCH_CACHE_TTL_SECONDS):
        return cached["data"]

    stock_results = _twelvedata_search(query, limit=6)
    crypto_results = _coingecko_search(query, limit=6)

    # Stocks first, always - crypto only fills whatever slots are left.
    results = (stock_results + crypto_results)[:limit]

    _search_cache[query_key] = {"data": results, "ts": _now()}
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
    symbol = symbol.strip().upper()
    range_key = (range_key or "").strip().upper()
    range_cfg = CHART_RANGES.get(range_key)
    if not range_cfg:
        return None

    cache_key = f"{asset_type}:{symbol}:{range_key}"
    cached = _chart_cache.get(cache_key)
    if cached and (_now() - cached["ts"] < CACHE_TTL_SECONDS):
        return cached["data"]

    now = datetime.utcnow()
    points = None

    if asset_type == "crypto":
        coin_id = _resolve_coingecko_id(symbol)
        if coin_id:
            from_dt = now - range_cfg["lookback"]
            points = _coingecko_market_chart_range(
                coin_id, from_dt.timestamp(), now.timestamp(), range_cfg["cg_interval"]
            )
    else:
        start_dt = now - range_cfg["lookback"]
        points = _twelvedata_time_series(
            symbol,
            range_cfg["td_interval"],
            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )

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

    _chart_cache[cache_key] = {"data": data, "ts": _now()}
    return data
