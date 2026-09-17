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
from datetime import datetime, timedelta

from tinymongo import TinyMongoClient

import price_service

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_FOLDER = os.path.join(BASE_DIR, "tinydb_storage")

_client = TinyMongoClient(STORAGE_FOLDER)
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

    field = market + "_portfolio"
    holdings = doc.get(field, [])

    refreshed = []
    for h in holdings:
        live_price = price_service.get_price(h["name"])
        price = live_price if live_price is not None else h.get("price", h.get("bought_price", 0))
        quantity = h.get("quantity", 0)
        bought_price = h.get("bought_price", 0)

        profit_loss = round(price - bought_price, 2)
        profit_loss_percent = round((profit_loss / bought_price) * 100, 2) if bought_price else 0.0

        refreshed.append({
            "name": h["name"],
            "bought_date": h.get("bought_date"),
            "bought_price": bought_price,
            "price": round(price, 2),
            "quantity": quantity,
            "total_amount": round(price * quantity, 2),
            "profit_loss": profit_loss,
            "profit_loss_percent": profit_loss_percent,
        })

    if refreshed:
        # Persist only the fields that are actually stored (not the derived
        # profit/loss numbers, which are recomputed fresh on every read).
        stored = [
            {k: r[k] for k in ("name", "bought_date", "bought_price", "price", "quantity", "total_amount")}
            for r in refreshed
        ]
        portfolios.update_one({"user_id": user_id}, {"$set": {field: stored}})

    wallet_amount = doc.get("wallet", {}).get(market, 0)

    return {
        "wallet": wallet_amount,
        "currency": CURRENCY_BY_MARKET[market],
        "holdings": refreshed,
    }
