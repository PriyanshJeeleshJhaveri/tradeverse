"""MongoDB Atlas storage for TradeVerse portfolios and simulated wallets."""

from __future__ import annotations

import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, ReturnDocument

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


MIN_ORDER_VALUE = 0.01          # smallest order worth recording (avoids free 0.00 trades)
MAX_QUANTITY = 1_000_000_000    # sanity cap on a single order
_QTY_EPSILON = 1e-8             # float dust threshold for "this lot is empty"


def _get_collection():
    return mongo_db.get_collection(MONGODB_PORTFOLIO_COLLECTION)


def init_indexes() -> None:
    collection = _get_collection()
    # One round trip to see what exists, instead of one create_index call each
    # time a cold serverless instance starts.
    if "user_portfolio_unique" not in collection.index_information():
        collection.create_index([("user_id", ASCENDING)], unique=True, name="user_portfolio_unique")


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
        "transactions": [],
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


def ensure_portfolio_exists(user_id: str, username: str, role: str = "USER") -> None:
    """Create the default portfolio only if the account does not have one yet.

    A single cheap upsert, so a half-finished registration can never leave an
    account that can log in but has no wallet.
    """
    _get_collection().update_one(
        {"user_id": user_id},
        {"$setOnInsert": _empty_portfolio_doc(user_id, username, role=role)},
        upsert=True,
    )


def resolve_market_for_symbol(symbol: str, asset_type: str) -> str:
    if asset_type == "crypto":
        return "CCME"
    symbol_u = symbol.strip().upper()
    # NSE (.NS) and BSE (.BO) listings are both Indian-market, INR-priced.
    return "ISE" if symbol_u.endswith((".NS", ".BO")) else "USE"


def _is_crypto_ticker(symbol: str) -> bool:
    return symbol.strip().upper().endswith("-USD")


def _parse_quantity(raw: Any, whole_only: bool):
    """Validate an order quantity. Returns (quantity, error_message)."""
    if isinstance(raw, bool):
        return None, "Quantity must be a number."
    try:
        quantity = float(raw)
    except (TypeError, ValueError):
        return None, "Quantity must be a number."

    # NaN/inf slip past ordinary `<= 0` checks and would corrupt a wallet.
    if not math.isfinite(quantity):
        return None, "Quantity must be a number."
    if quantity <= 0:
        return None, "Quantity must be greater than zero."
    if quantity > MAX_QUANTITY:
        return None, "Quantity is too large."

    if whole_only:
        if quantity != int(quantity):
            return None, "Stock quantity must be a whole number of shares."
        return int(quantity), None

    quantity = round(quantity, 8)
    if quantity <= 0:
        return None, "Quantity is too small."
    return quantity, None


def _clean_money(value: Any) -> float:
    """Round to cents and avoid a displayed '-0.00' from float dust."""
    return round(float(value or 0), 2) or 0.0


def buy_asset(user_id: str, symbol: str, asset_type: str, quantity: Any):
    symbol = (symbol or "").strip().upper()
    asset_type = (asset_type or "").strip().lower()

    if asset_type not in ("stock", "crypto") or not price_service.is_valid_symbol(symbol):
        return False, "Invalid request."
    if (asset_type == "crypto") != _is_crypto_ticker(symbol):
        return False, "That symbol does not match the selected asset type."

    market = resolve_market_for_symbol(symbol, asset_type)

    quantity, error = _parse_quantity(quantity, whole_only=(asset_type == "stock"))
    if error:
        return False, error

    # Fill price: much fresher than the dashboard's display cache.
    price = price_service.get_trade_price(symbol)
    if price is None or price <= 0:
        return False, "Could not fetch the current price right now. Please try again."

    total_cost = round(price * quantity, 2)
    if total_cost < MIN_ORDER_VALUE:
        return False, "Order value is too small. Please increase the quantity."

    field = market + "_portfolio"
    new_lot = {
        "id": _new_lot_id(),
        "name": symbol,
        "bought_date": _utc_date(),
        "bought_price": round(price, 2),
        "price": round(price, 2),
        "quantity": quantity,
        "total_amount": total_cost,
    }

    # ONE atomic update: the balance check, the debit and the new lot happen
    # together, so a double-click or two open tabs can never lose a lot or
    # spend the same money twice.
    updated = _get_collection().find_one_and_update(
        {"user_id": user_id, "wallet." + market: {"$gte": total_cost - 1e-6}},
        {
            "$inc": {"wallet." + market: -total_cost},
            "$push": {field: new_lot},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
        projection={"wallet": 1, "_id": 0},
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        if _get_collection().find_one({"user_id": user_id}, {"_id": 1}) is None:
            return False, "Portfolio not found."
        return False, f"Insufficient balance in your {market} wallet."

    return True, {
        "market": market,
        "wallet": _clean_money(updated.get("wallet", {}).get(market)),
        "lot": new_lot,
    }


def sell_lot(user_id: str, market: str, lot_id: str, quantity: Any):
    market = (market or "").strip().upper()
    lot_id = (lot_id or "").strip()

    if market not in MARKETS:
        return False, "Unknown market."

    collection = _get_collection()
    field = market + "_portfolio"

    doc = collection.find_one({"user_id": user_id}, {field: 1, "_id": 0})
    if not doc:
        return False, "Portfolio not found."

    lot = next((h for h in doc.get(field, []) if h.get("id") == lot_id), None)
    if lot is None:
        return False, "That holding could not be found - it may have already been sold."

    quantity, error = _parse_quantity(quantity, whole_only=not _is_crypto_ticker(lot["name"]))
    if error:
        return False, error

    available = lot.get("quantity", 0)
    if quantity > available + 1e-9:
        return False, f"You can only sell up to {available} from this lot."

    price = price_service.get_trade_price(lot["name"])
    if price is None or price <= 0:
        return False, "Could not fetch the current price right now. Please try again."

    proceeds = round(price * quantity, 2)
    remaining_quantity = round(available - quantity, 8)
    sells_everything = remaining_quantity <= _QTY_EPSILON

    bought_price = lot.get("bought_price", 0)
    buy_amount = round(bought_price * quantity, 2)
    profit_loss_amount = round(proceeds - buy_amount, 2)
    profit_loss_percent = round((profit_loss_amount / buy_amount) * 100, 2) if buy_amount else 0.0
    transaction = {
        "id": uuid.uuid4().hex,
        "market": market,
        "name": lot["name"],
        "quantity": quantity,
        "bought_price": round(bought_price, 2),
        "sell_price": round(price, 2),
        "sell_date": _utc_date(),
        "profit_loss_amount": profit_loss_amount,
        "profit_loss_percent": profit_loss_percent,
        "created_at": datetime.now(timezone.utc),
    }

    # ONE atomic update. The filter only matches while this lot STILL has the
    # quantity being sold, so a second click / second tab finds nothing to
    # match - no duplicate transaction and no double credit.
    updated = collection.find_one_and_update(
        {
            "user_id": user_id,
            field: {"$elemMatch": {"id": lot_id, "quantity": {"$gte": quantity - 1e-9}}},
        },
        {
            "$inc": {field + ".$.quantity": -quantity, "wallet." + market: proceeds},
            "$set": {field + ".$.price": round(price, 2), "updated_at": datetime.now(timezone.utc)},
            "$push": {"transactions": transaction},
        },
        projection={"wallet": 1, "_id": 0},
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        return False, "That holding could not be found, or it no longer has enough quantity - it may have already been sold."

    if sells_everything:
        # Remove the now-empty lot (anything that is just float dust).
        collection.update_one(
            {"user_id": user_id},
            {"$pull": {field: {"id": lot_id, "quantity": {"$lte": _QTY_EPSILON}}}},
        )

    return True, {
        "market": market,
        "wallet": _clean_money(updated.get("wallet", {}).get(market)),
        "proceeds": proceeds,
        "remaining_quantity": None if sells_everything else remaining_quantity,
        "transaction": transaction,
    }


def get_transaction_history(user_id: str, market: str | None = None):
    """Return completed sell transactions, newest first."""
    doc = _get_collection().find_one({"user_id": user_id}, {"transactions": 1, "_id": 0})
    if not doc:
        return []

    transactions = list(doc.get("transactions", []))
    if market:
        market = market.upper()
        transactions = [t for t in transactions if t.get("market") == market]

    def sort_key(t):
        return (str(t.get("sell_date") or ""), str(t.get("created_at") or ""), str(t.get("id") or ""))

    transactions.sort(key=sort_key, reverse=True)
    return transactions


def get_market_snapshot(user_id: str, market: str):
    """Wallet + holdings for one market, valued at live prices.

    This is a pure READ. (It used to write the refreshed holdings back to the
    database on every page view, which could overwrite a purchase made a
    moment earlier in another tab.)
    """
    market = market.upper()
    if market not in MARKETS:
        raise ValueError("Unknown market: " + market)

    field = market + "_portfolio"
    collection = _get_collection()
    doc = collection.find_one({"user_id": user_id}, {"wallet." + market: 1, field: 1, "_id": 0})
    if not doc:
        return {"wallet": 0, "currency": CURRENCY_BY_MARKET[market], "holdings": []}

    raw_holdings = doc.get(field, [])

    # Very old lots created before lot ids existed: give them one, once.
    if any(not h.get("id") for h in raw_holdings):
        for h in raw_holdings:
            h.setdefault("id", _new_lot_id())
        collection.update_one({"user_id": user_id}, {"$set": {field: raw_holdings}})

    holdings = [h for h in raw_holdings if (h.get("quantity") or 0) > _QTY_EPSILON]

    # One batched, cached, parallel price lookup for every holding.
    prices = price_service.get_prices([h["name"] for h in holdings])

    refreshed = []
    for h in holdings:
        live_price = prices.get(h["name"].strip().upper())
        price = live_price if live_price is not None else h.get("price", h.get("bought_price", 0))
        quantity = h.get("quantity", 0)
        bought_price = h.get("bought_price", 0)

        # Total profit/loss on the lot (per-unit gain x quantity), matching how
        # the Transaction History page reports it once the lot is sold.
        profit_loss = round((price - bought_price) * quantity, 2)
        profit_loss_percent = round(((price - bought_price) / bought_price) * 100, 2) if bought_price else 0.0

        refreshed.append({
            "id": h["id"],
            "name": h["name"],
            "bought_date": h.get("bought_date"),
            "bought_price": bought_price,
            "price": round(price, 2),
            "quantity": quantity,
            "total_amount": round(price * quantity, 2),
            "profit_loss": profit_loss,
            "profit_loss_percent": profit_loss_percent,
        })

    return {
        "wallet": _clean_money(doc.get("wallet", {}).get(market)),
        "currency": CURRENCY_BY_MARKET[market],
        "holdings": refreshed,
    }
