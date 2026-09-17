"""
portfolio_db.py
----------------
NoSQL storage layer for TradeVerse user wallets & portfolios, using tinymongo
(a MongoDB-like wrapper around TinyDB, stored as local JSON files).

One document per user, shaped like:

{
    "user_id": 1,                # matches the SQLite users.id
    "username": "admin",
    "wallet": {
        "ISE": 1000000.0,        # INR
        "USE": 10000.0,          # USD
        "CCME": 10000.0          # USD
    },
    "ISE_portfolio": [
        {"name": "RELIANCE.NS", "bought_price": 2400.0, "price": 2400.0,
         "quantity": 20, "total_amount": 48000.0},
        ...
    ],
    "USE_portfolio": [...],
    "CCME_portfolio": [...]
}

ISE  = Indian Stock Market      (currency: INR)
USE  = US Stock Market          (currency: USD)
CCME = Crypto Currency Market Exchange (currency: USD)
"""

import os
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from tinymongo import TinyMongoClient

from storage_paths import prepare_storage

import price_service

_, STORAGE_FOLDER = prepare_storage()

_client = TinyMongoClient(str(STORAGE_FOLDER))
_db = _client.tradeverse
portfolios = _db.portfolios

MARKETS = ("ISE", "USE", "CCME")
CURRENCY_BY_MARKET = {"ISE": "INR", "USE": "USD", "CCME": "USD"}

DEFAULT_WALLET = {
    "ISE": 1000000.0,   # 10,00,000 INR
    "USE": 10000.0,     # 10,000 USD
    "CCME": 10000.0,    # 10,000 USD
}


def _empty_portfolio_doc(user_id, username, wallet=None, holdings=None):
    holdings = holdings or {"ISE": [], "USE": [], "CCME": []}
    return {
        "user_id": user_id,
        "username": username,
        "wallet": dict(wallet or DEFAULT_WALLET),
        "ISE_portfolio": holdings.get("ISE", []),
        "USE_portfolio": holdings.get("USE", []),
        "CCME_portfolio": holdings.get("CCME", []),
    }


def get_portfolio_doc(user_id):
    return portfolios.find_one({"user_id": user_id})


def create_default_portfolio(user_id, username):
    """Called whenever a brand new user registers. All portfolios start empty."""
    if get_portfolio_doc(user_id):
        return
    doc = _empty_portfolio_doc(user_id, username)
    portfolios.insert_one(doc)


def _holding(name, bought_price, quantity, bought_days_ago=0):
    total = round(bought_price * quantity, 2)
    bought_date = (datetime.utcnow() - timedelta(days=bought_days_ago)).strftime("%Y-%m-%d")
    return {
        "name": name,
        "bought_date": bought_date,
        "bought_price": round(bought_price, 2),
        "price": round(bought_price, 2),
        "quantity": quantity,
        "total_amount": total,
    }


def ensure_admin_portfolio(user_id, username="admin"):
    """Creates a demo portfolio for the built-in admin account, if it doesn't exist yet."""
    if get_portfolio_doc(user_id):
        return

    ise_holdings = [
        _holding("RELIANCE.NS", 2400.0, 20, bought_days_ago=45),
        _holding("TCS.NS", 3500.0, 10, bought_days_ago=120),
        _holding("INFY.NS", 1450.0, 15, bought_days_ago=200),
    ]
    use_holdings = [
        _holding("AAPL", 180.0, 15, bought_days_ago=60),
        _holding("MSFT", 320.0, 10, bought_days_ago=150),
        _holding("TSLA", 250.0, 8, bought_days_ago=30),
    ]
    ccme_holdings = [
        _holding("BTC-USD", 45000.0, 0.10, bought_days_ago=90),
        _holding("ETH-USD", 2500.0, 1.5, bought_days_ago=20),
    ]

    ise_spent = sum(h["total_amount"] for h in ise_holdings)
    use_spent = sum(h["total_amount"] for h in use_holdings)
    ccme_spent = sum(h["total_amount"] for h in ccme_holdings)

    wallet = {
        "ISE": round(DEFAULT_WALLET["ISE"] - ise_spent, 2),
        "USE": round(DEFAULT_WALLET["USE"] - use_spent, 2),
        "CCME": round(DEFAULT_WALLET["CCME"] - ccme_spent, 2),
    }

    doc = _empty_portfolio_doc(
        user_id,
        username,
        wallet=wallet,
        holdings={"ISE": ise_holdings, "USE": use_holdings, "CCME": ccme_holdings},
    )
    portfolios.insert_one(doc)



def _new_lot_id():
    """Create a unique identifier for every purchase lot."""
    return uuid.uuid4().hex


def _normalise_quantity(value):
    """Validate and normalise a positive quantity without allowing 0/negative values."""
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Quantity must be a valid number.")
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("Quantity must be greater than 0.")
    # Keep fractional crypto quantities while avoiding excessive precision.
    quantity = quantity.quantize(Decimal("0.00000001"))
    if quantity <= 0:
        raise ValueError("Quantity must be greater than 0.")
    return float(quantity)


def _money(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid price.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Current price is unavailable.")
    return float(amount)


def _ensure_lot_ids(user_id, market, doc=None):
    """Migrate old holdings in-place by assigning lot IDs without changing their quantities."""
    if market not in MARKETS:
        raise ValueError("Unknown market: " + market)
    if doc is None:
        doc = get_portfolio_doc(user_id)
    if not doc:
        return None

    field = market + "_portfolio"
    holdings = list(doc.get(field, []))
    changed = False
    migrated = []
    for holding in holdings:
        item = dict(holding)
        if not item.get("lot_id"):
            item["lot_id"] = _new_lot_id()
            # Existing lots have a date but no timestamp. Keep the original date
            # and use a migration timestamp only for uniqueness/audit purposes.
            item.setdefault("bought_at", item.get("bought_date"))
            changed = True
        migrated.append(item)

    if changed:
        portfolios.update_one({"user_id": user_id}, {"$set": {field: migrated}})
        doc[field] = migrated
    return doc


def buy_asset(user_id, market, symbol, quantity, current_price=None):
    """Buy a new independent lot and deduct its cost from the market wallet."""
    market = market.upper()
    if market not in ("USE", "CCME"):
        raise ValueError("Trading is not available for the Indian Stock Market yet.")

    quantity = _normalise_quantity(quantity)
    if market == "USE" and not quantity.is_integer():
        raise ValueError("US stock quantity must be a whole number.")
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Asset symbol is required.")
    if market == "USE" and (symbol.endswith(".NS") or symbol.endswith(".BO")):
        raise ValueError("Trading Indian stocks is not available yet.")

    # Always re-fetch/revalidate the price on the server. The browser's displayed
    # price is only an estimate and must never be trusted for the actual debit.
    live_price = price_service.get_price(symbol)
    if live_price is None:
        raise ValueError("Current market price is unavailable. Please try again.")
    price = _money(live_price)
    total = round(price * quantity, 2)

    doc = get_portfolio_doc(user_id)
    if not doc:
        raise ValueError("Portfolio not found.")

    wallet = dict(doc.get("wallet", {}))
    balance = round(float(wallet.get(market, 0)), 2)
    if total > balance + 1e-9:
        raise ValueError(
            f"Insufficient balance. Required {total:.2f}, available {balance:.2f}."
        )

    lot = {
        "lot_id": _new_lot_id(),
        "name": symbol,
        "bought_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "bought_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "bought_price": price,
        "price": price,
        "quantity": quantity,
        "total_amount": total,
    }

    field = market + "_portfolio"
    holdings = list(doc.get(field, []))
    holdings.append(lot)  # Never merge lots, even when symbol/price/time are identical.
    wallet[market] = round(balance - total, 2)

    portfolios.update_one(
        {"user_id": user_id},
        {"$set": {field: holdings, "wallet": wallet}},
    )

    return {
        "market": market,
        "lot": lot,
        "wallet": wallet[market],
        "total_amount": total,
        "currency": CURRENCY_BY_MARKET[market],
    }


def sell_lot(user_id, market, lot_id, quantity, current_price=None):
    """Sell part or all of one exact lot and credit proceeds to its market wallet."""
    market = market.upper()
    if market not in ("USE", "CCME"):
        raise ValueError("Trading is not available for the Indian Stock Market yet.")

    quantity_to_sell = _normalise_quantity(quantity)
    if market == "USE" and not quantity_to_sell.is_integer():
        raise ValueError("US stock quantity must be a whole number.")
    lot_id = str(lot_id).strip()
    if not lot_id:
        raise ValueError("Lot ID is required.")

    doc = _ensure_lot_ids(user_id, market)
    if not doc:
        raise ValueError("Portfolio not found.")

    field = market + "_portfolio"
    holdings = list(doc.get(field, []))
    index = next((i for i, h in enumerate(holdings) if str(h.get("lot_id")) == lot_id), None)
    if index is None:
        raise ValueError("That holding lot no longer exists. Refresh the portfolio and try again.")

    lot = dict(holdings[index])
    available = _normalise_quantity(lot.get("quantity", 0))
    if quantity_to_sell > available + 1e-9:
        raise ValueError(f"You can sell at most {available:g} from this lot.")

    symbol = str(lot.get("name", "")).upper()
    live_price = price_service.get_price(symbol)
    if live_price is None:
        raise ValueError("Current market price is unavailable. Please try again.")
    price = _money(live_price)
    proceeds = round(price * quantity_to_sell, 2)

    wallet = dict(doc.get("wallet", {}))
    balance = round(float(wallet.get(market, 0)), 2)
    new_quantity = round(available - quantity_to_sell, 8)

    if new_quantity <= 0:
        holdings.pop(index)
    else:
        lot["quantity"] = new_quantity
        # Keep the lot's original bought price/date/id. Only the remaining
        # quantity and current valuation are changed.
        lot["price"] = price
        lot["total_amount"] = round(price * new_quantity, 2)
        holdings[index] = lot

    wallet[market] = round(balance + proceeds, 2)
    portfolios.update_one(
        {"user_id": user_id},
        {"$set": {field: holdings, "wallet": wallet}},
    )

    return {
        "market": market,
        "lot_id": lot_id,
        "symbol": symbol,
        "quantity_sold": quantity_to_sell,
        "proceeds": proceeds,
        "remaining_quantity": new_quantity,
        "wallet": wallet[market],
        "currency": CURRENCY_BY_MARKET[market],
    }

def get_market_snapshot(user_id, market):
    """
    Returns the wallet amount + holdings for one market (ISE/USE/CCME), with
    live prices refreshed through price_service (10-minute cache under the hood).
    Also persists the refreshed prices back into the document.
    """
    market = market.upper()
    if market not in MARKETS:
        raise ValueError("Unknown market: " + market)

    doc = get_portfolio_doc(user_id)
    if not doc:
        return {"wallet": 0, "currency": CURRENCY_BY_MARKET[market], "holdings": []}

    doc = _ensure_lot_ids(user_id, market, doc)
    field = market + "_portfolio"
    holdings = doc.get(field, [])

    def _refresh_holding(h):
        live_price = price_service.get_price(h["name"])
        price = live_price if live_price is not None else h.get("price", h.get("bought_price", 0))
        quantity = h.get("quantity", 0)
        bought_price = h.get("bought_price", 0)

        profit_loss = round(price - bought_price, 2)
        profit_loss_percent = round((profit_loss / bought_price) * 100, 2) if bought_price else 0.0

        return {
            "lot_id": h.get("lot_id"),
            "name": h["name"],
            "bought_date": h.get("bought_date"),
            "bought_at": h.get("bought_at"),
            "bought_price": bought_price,
            "price": round(price, 2),
            "quantity": quantity,
            "total_amount": round(price * quantity, 2),
            "profit_loss": profit_loss,
            "profit_loss_percent": profit_loss_percent,
        }

    # Fetch independent quotes concurrently. This matters on Vercel because
    # a market page can contain several holdings and each external API call
    # has network latency. Keeping them parallel avoids serial timeout buildup.
    if holdings:
        worker_count = min(6, len(holdings))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            refreshed = list(executor.map(_refresh_holding, holdings))
    else:
        refreshed = []

    if refreshed:
        # Persist only the fields that are actually stored (not the derived
        # profit/loss numbers, which are recomputed fresh on every read).
        stored = [
            {k: r[k] for k in ("lot_id", "name", "bought_date", "bought_at", "bought_price", "price", "quantity", "total_amount")}
            for r in refreshed
        ]
        portfolios.update_one({"user_id": user_id}, {"$set": {field: stored}})

    wallet_amount = doc.get("wallet", {}).get(market, 0)

    return {
        "wallet": wallet_amount,
        "currency": CURRENCY_BY_MARKET[market],
        "holdings": refreshed,
    }
