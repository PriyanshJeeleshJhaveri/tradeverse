"""MongoDB Atlas storage for TradeVerse portfolios and simulated wallets."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING

import mongo_db
import price_service

MONGODB_PORTFOLIO_COLLECTION = (
    os.getenv("MONGODB_PORTFOLIO_COLLECTION", "portfolios").strip() or "portfolios"
)

MARKETS = ("ISE", "USE", "CCME")
CURRENCY_BY_MARKET = {"ISE": "INR", "USE": "USD", "CCME": "USD"}

DEFAULT_WALLET = {
    "ISE": 1_000_000.0,
    "USE": 10_000.0,
    "CCME": 10_000.0,
}


def _get_collection():
    return mongo_db.get_collection(MONGODB_PORTFOLIO_COLLECTION)


def init_indexes() -> None:
    _get_collection().create_index([("user_id", ASCENDING)], unique=True, name="user_portfolio_unique")


def _utc_date(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _new_lot_id() -> str:
    return uuid.uuid4().hex[:12]


def _holding(name: str, bought_price: float, quantity: float, bought_days_ago: int = 0) -> dict[str, Any]:
    total = round(bought_price * quantity, 2)
    return {
        "id": _new_lot_id(),
        "name": name,
        "bought_date": _utc_date(bought_days_ago),
        "bought_price": round(bought_price, 2),
        "price": round(bought_price, 2),
        "quantity": quantity,
        "total_amount": total,
    }


def _empty_portfolio_doc(
    user_id: str,
    username: str,
    wallet: dict[str, float] | None = None,
    holdings: dict[str, list[dict[str, Any]]] | None = None,
    role: str = "USER",
    portfolio_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    holdings = holdings or {"ISE": [], "USE": [], "CCME": []}
    is_admin = (role or "USER").upper() == "ADMIN"
    return {
        "user_id": user_id,
        "username": username,
        "portfolio_name": portfolio_name or ("Admin Portfolio" if is_admin else f"{username}'s Portfolio"),
        "description": description or (
            "Default portfolio for the administrator."
            if is_admin
            else "Default portfolio created when the account was registered."
        ),
        "is_default": True,
        "is_admin_portfolio": is_admin,
        "wallet": dict(wallet or DEFAULT_WALLET),
        "ISE_portfolio": holdings.get("ISE", []),
        "USE_portfolio": holdings.get("USE", []),
        "CCME_portfolio": holdings.get("CCME", []),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def get_portfolio_doc(user_id: str) -> dict[str, Any] | None:
    return _get_collection().find_one({"user_id": user_id}, {"_id": 0})


def create_default_portfolio(user_id: str, username: str, role: str = "USER") -> dict[str, Any]:
    collection = _get_collection()
    doc = _empty_portfolio_doc(user_id, username, role=role)
    collection.update_one(
        {"user_id": user_id},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return collection.find_one({"user_id": user_id}, {"_id": 0})


def ensure_admin_portfolio(user_id: str, username: str = "admin") -> dict[str, Any]:
    """Create the default admin portfolio with the existing demo positions once."""
    collection = _get_collection()
    existing = collection.find_one({"user_id": user_id}, {"_id": 0})
    if existing:
        updates = {}
        if not existing.get("portfolio_name") or existing.get("portfolio_name") == f"{username}'s Portfolio":
            updates["portfolio_name"] = "Admin Portfolio"
        if not existing.get("description") or existing.get("description") == "Default portfolio created when the account was registered.":
            updates["description"] = "Default portfolio for the administrator."
        if not existing.get("is_default"):
            updates["is_default"] = True
        if not existing.get("is_admin_portfolio"):
            updates["is_admin_portfolio"] = True
        if updates:
            updates["updated_at"] = datetime.now(timezone.utc)
            collection.update_one({"user_id": user_id}, {"$set": updates})
            existing.update(updates)
        return existing

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

    wallet = {
        "ISE": round(DEFAULT_WALLET["ISE"] - sum(h["total_amount"] for h in ise_holdings), 2),
        "USE": round(DEFAULT_WALLET["USE"] - sum(h["total_amount"] for h in use_holdings), 2),
        "CCME": round(DEFAULT_WALLET["CCME"] - sum(h["total_amount"] for h in ccme_holdings), 2),
    }

    doc = _empty_portfolio_doc(
        user_id,
        username,
        wallet=wallet,
        holdings={"ISE": ise_holdings, "USE": use_holdings, "CCME": ccme_holdings},
        role="ADMIN",
        portfolio_name="Admin Portfolio",
        description="Default portfolio for the administrator.",
    )
    collection.insert_one(doc)
    return {k: v for k, v in doc.items() if k != "_id"}


def update_portfolio_metadata(user_id: str, portfolio_name: str, description: str) -> bool:
    portfolio_name = (portfolio_name or "").strip()
    description = (description or "").strip()
    if not portfolio_name:
        raise ValueError("Portfolio name cannot be empty.")
    if len(portfolio_name) > 80:
        raise ValueError("Portfolio name is too long.")
    if len(description) > 500:
        raise ValueError("Description is too long.")

    result = _get_collection().update_one(
        {"user_id": user_id},
        {"$set": {
            "portfolio_name": portfolio_name,
            "description": description,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return result.matched_count == 1


def resolve_market_for_symbol(symbol: str, asset_type: str) -> str:
    if asset_type == "crypto":
        return "CCME"
    return "ISE" if symbol.strip().upper().endswith(".NS") else "USE"


def _is_crypto_ticker(symbol: str) -> bool:
    return symbol.strip().upper().endswith("-USD")


def buy_asset(user_id: str, symbol: str, asset_type: str, quantity: Any):
    symbol = symbol.strip().upper()
    market = resolve_market_for_symbol(symbol, asset_type)

    if market == "ISE":
        return False, "Indian stock market trading isn't available yet."

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return False, "Quantity must be a number."

    if quantity <= 0:
        return False, "Quantity must be greater than zero."
    if asset_type == "stock" and quantity != int(quantity):
        return False, "Stock quantity must be a whole number of shares."

    price = price_service.get_price(symbol)
    if price is None:
        return False, "Could not fetch the current price right now. Please try again."

    total_cost = round(price * quantity, 2)
    collection = _get_collection()
    doc = collection.find_one({"user_id": user_id})

    if not doc:
        return False, "Portfolio not found."

    wallet = doc.get("wallet", {})
    balance = wallet.get(market, 0)
    if total_cost > balance:
        return False, f"Insufficient balance in your {market} wallet."

    field = market + "_portfolio"
    holdings = doc.get(field, [])

    new_lot = {
        "id": _new_lot_id(),
        "name": symbol,
        "bought_date": _utc_date(),
        "bought_price": round(price, 2),
        "price": round(price, 2),
        "quantity": quantity,
        "total_amount": total_cost,
    }
    holdings.append(new_lot)
    new_balance = round(balance - total_cost, 2)

    collection.update_one(
        {"user_id": user_id},
        {"$set": {field: holdings, "wallet." + market: new_balance, "updated_at": datetime.now(timezone.utc)}},
    )

    return True, {"market": market, "wallet": new_balance, "lot": new_lot}


def sell_lot(user_id: str, market: str, lot_id: str, quantity: Any):
    market = (market or "").strip().upper()

    if market == "ISE":
        return False, "Indian stock market trading isn't available yet."
    if market not in MARKETS:
        return False, "Unknown market."

    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return False, "Quantity must be a number."

    if quantity <= 0:
        return False, "Quantity must be greater than zero."

    collection = _get_collection()
    doc = collection.find_one({"user_id": user_id})
    if not doc:
        return False, "Portfolio not found."

    field = market + "_portfolio"
    holdings = doc.get(field, [])

    lot = next((h for h in holdings if h.get("id") == lot_id), None)
    if lot is None:
        return False, "That holding could not be found - it may have already been sold."

    available = lot.get("quantity", 0)
    if not _is_crypto_ticker(lot["name"]) and quantity != int(quantity):
        return False, "Stock quantity must be a whole number of shares."
    if quantity > available + 1e-9:
        return False, f"You can only sell up to {available} from this lot."

    price = price_service.get_price(lot["name"])
    if price is None:
        return False, "Could not fetch the current price right now. Please try again."

    proceeds = round(price * quantity, 2)
    remaining_quantity = round(available - quantity, 8)

    if remaining_quantity <= 1e-9:
        holdings = [h for h in holdings if h.get("id") != lot_id]
        remaining_quantity = None
    else:
        for h in holdings:
            if h.get("id") == lot_id:
                h["quantity"] = remaining_quantity
                h["price"] = round(price, 2)
                h["total_amount"] = round(remaining_quantity * price, 2)
                break

    wallet = doc.get("wallet", {})
    new_balance = round(wallet.get(market, 0) + proceeds, 2)

    collection.update_one(
        {"user_id": user_id},
        {"$set": {field: holdings, "wallet." + market: new_balance, "updated_at": datetime.now(timezone.utc)}},
    )

    return True, {
        "market": market,
        "wallet": new_balance,
        "proceeds": proceeds,
        "remaining_quantity": remaining_quantity,
    }


def get_market_snapshot(user_id: str, market: str):
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
        lot_id = h.get("id") or _new_lot_id()

        live_price = price_service.get_price(h["name"])
        price = live_price if live_price is not None else h.get("price", h.get("bought_price", 0))
        quantity = h.get("quantity", 0)
        bought_price = h.get("bought_price", 0)

        profit_loss = round(price - bought_price, 2)
        profit_loss_percent = round((profit_loss / bought_price) * 100, 2) if bought_price else 0.0

        refreshed.append({
            "id": lot_id,
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
        stored = [
            {k: r[k] for k in ("id", "name", "bought_date", "bought_price", "price", "quantity", "total_amount")}
            for r in refreshed
        ]
        _get_collection().update_one(
            {"user_id": user_id},
            {"$set": {field: stored, "updated_at": datetime.now(timezone.utc)}},
        )

    return {
        "wallet": doc.get("wallet", {}).get(market, 0),
        "currency": CURRENCY_BY_MARKET[market],
        "holdings": refreshed,
    }
